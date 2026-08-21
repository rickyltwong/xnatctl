"""Download service for XNAT download operations."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    AuthenticationError,
    DownloadError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.models.hierarchy import ExperimentRef, ResourceRef, ScanRef
from xnatctl.models.progress import (
    DownloadProgress,
    DownloadSummary,
    OperationPhase,
    VerificationReport,
)

from . import verify
from .base import BaseService
from .hierarchy import HierarchyService
from .resources import ResourceService
from .sessions import SessionService

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class StreamedFile(NamedTuple):
    """Result of :func:`stream_to_file`.

    Attributes:
        bytes_written: Bytes written to the destination.
        content_length: The response Content-Length, or None when the server
            did not send one.
    """

    bytes_written: int
    content_length: int | None


def _declared_content_length(response: httpx.Response) -> int | None:
    """The Content-Length usable for byte-count verification, or None.

    None when the header is absent, malformed, or negative -- and when a
    non-identity Content-Encoding means httpx's decoded byte count would not
    match the wire length anyway.
    """
    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        return None
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return None
    return length if length >= 0 else None


def stream_to_file(
    client: XNATClient,
    path: str,
    dest: Path,
    *,
    params: dict[str, Any] | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    chunk_size: int = _DOWNLOAD_CHUNK_SIZE,
) -> StreamedFile:
    """Stream a GET to ``dest`` atomically, through the client's retry/auth path.

    Writes to a sibling ``.part`` file and renames on success, so a network
    drop never leaves a truncated file that looks complete. If the response
    carries a nonzero Content-Length that disagrees with the bytes written, the
    download is rejected. On any failure the ``.part`` is removed and no
    ``dest`` is produced.

    Args:
        client: Client to stream through (retry ladder, typed errors, auth).
        path: API path.
        dest: Final destination path.
        params: Query parameters.
        progress_cb: Called after each chunk with
            ``(bytes_written, content_length)``.
        chunk_size: Read chunk size in bytes.

    Returns:
        The bytes written and the response Content-Length.

    Raises:
        DownloadError: On a Content-Length mismatch.
    """
    # Unique per process and thread, so parallel workers (or two commands)
    # aiming at the same destination cannot truncate or unlink each other's
    # in-flight temporary.
    part = dest.with_name(f"{dest.name}.{os.getpid()}-{threading.get_ident()}.part")
    bytes_written = 0
    content_length: int | None = None
    try:
        with client.stream("GET", path, params=params) as response:
            content_length = _declared_content_length(response)

            with open(part, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if progress_cb is not None:
                        progress_cb(bytes_written, content_length)

        if content_length is not None and content_length != 0 and bytes_written != content_length:
            raise DownloadError(
                f"Incomplete download of {path}: wrote {bytes_written} bytes but the "
                f"server declared Content-Length {content_length}",
                path,
            )

        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    return StreamedFile(bytes_written, content_length)


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """Extract ZIP contents safely, guarding against path traversal."""
    resolved_root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (extract_dir / member.filename).resolve()
            if not target.is_relative_to(resolved_root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_scan_zip(  # noqa: C901  # pre-existing; see pyproject
    zip_path: Path,
    scan_base: Path,
    *,
    resource_label: str | None = None,
    exclude_resources: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """Extract a scan ZIP into standard XNAT layout.

    Handles both filtered ZIPs (single resource) and unfiltered ZIPs
    (multiple resources).  XNAT ZIP structure:
        {exp}/scans/{id}/resources/{label}/files/{filename...}

    Args:
        zip_path: Path to the downloaded ZIP file.
        scan_base: Target directory (e.g. session_dir/scans/{scan_id}).
        resource_label: If set, all files go under resources/{label}/files/.
            When None, resource labels are inferred from ZIP paths.
        exclude_resources: Resource labels to skip during extraction.

    Returns:
        Tuple of (files_extracted, duplicates_renamed).
    """
    files_extracted = 0
    duplicates_renamed = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            member_path = Path(member.filename)
            if any(part.startswith(".") for part in member_path.parts):
                continue

            parts = member_path.parts

            # Detect resource label and relative file path from ZIP entry.
            detected_label: str | None = None
            rel: Path | None = None
            if "resources" in parts and "files" in parts:
                res_idx = parts.index("resources")
                files_idx = parts.index("files")
                if files_idx > res_idx + 1:
                    detected_label = parts[res_idx + 1]
                    rel_parts = parts[files_idx + 1 :]
                    if rel_parts:
                        rel = Path(*rel_parts)

            # Fallback: strip up to "files/" if present, else strip top folder.
            if rel is None:
                if "files" in parts:
                    idx = parts.index("files")
                    rel_parts = parts[idx + 1 :]
                    if not rel_parts:
                        continue
                    rel = Path(*rel_parts)
                elif len(parts) > 1:
                    rel = Path(*parts[1:])
                else:
                    rel = member_path

            if not rel.name or rel.name.startswith("."):
                continue

            effective_label = resource_label or detected_label or "UNKNOWN"

            if effective_label in exclude_resources:
                continue

            target_dir = scan_base / "resources" / effective_label / "files"
            target_dir.mkdir(parents=True, exist_ok=True)
            resolved_root = target_dir.resolve()

            dest = (target_dir / rel).resolve()
            if not dest.is_relative_to(resolved_root):
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)

            final_dest = dest
            if final_dest.exists():
                duplicates_renamed += 1
                stem = final_dest.stem
                suffix = final_dest.suffix
                i = 1
                while True:
                    candidate = final_dest.with_name(f"{stem}__dup{i}{suffix}")
                    if not candidate.exists():
                        final_dest = candidate
                        break
                    i += 1

            with zf.open(member) as src, open(final_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files_extracted += 1

    return files_extracted, duplicates_renamed


def extract_session_zips(
    session_dir: Path,
    *,
    cleanup: bool = True,
    on_message: Callable[[str], None] | None = None,
    zip_paths: Sequence[Path] | None = None,
) -> None:
    """Extract ZIP files in a session directory.

    Args:
        session_dir: Path to session directory containing ZIPs.
        cleanup: Remove ZIPs after successful extraction.
        on_message: Called with each user-facing progress line (rendering is
            the caller's concern; the service prints nothing).
        zip_paths: Exactly which ZIPs to extract; None globs every ``*.zip``
            in *session_dir* (the default). A caller that wants to defer some
            ZIPs -- e.g. session-resource ZIPs that ``--verify`` still needs
            to read intact -- passes the rest explicitly instead.

    Raises:
        DownloadError: If any archive is corrupt; extraction of a truncated
            download must fail the command rather than report a partial success.
    """
    zip_files = list(zip_paths) if zip_paths is not None else list(session_dir.glob("*.zip"))
    if not zip_files:
        return

    failed_zips: list[str] = []
    for zip_path in zip_files:
        if on_message is not None:
            on_message(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # CRC-check every member before writing anything: testzip()
                # returns the first corrupt name, or None when the archive is
                # whole. Without it a truncated download extracted partially and
                # the command still reported success.
                if zf.testzip() is not None:
                    failed_zips.append(zip_path.name)
                    continue
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue

                    member_path = Path(member)
                    if any(part.startswith(".") for part in member_path.parts):
                        continue

                    parts = member_path.parts
                    if len(parts) > 1:
                        stripped_path = Path(*parts[1:])
                    else:
                        stripped_path = member_path

                    target_path = session_dir / stripped_path
                    # Guard against ZipSlip path traversal
                    if not target_path.resolve().is_relative_to(session_dir.resolve()):
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)

            if cleanup:
                zip_path.unlink()
                if on_message is not None:
                    on_message(f"  Removed {zip_path.name}")
        except zipfile.BadZipFile:
            failed_zips.append(zip_path.name)

    if failed_zips:
        # A corrupt archive must fail the command, not print and exit 0:
        # @handle_errors turns this into a nonzero exit so a broken download is
        # never mistaken for a complete one.
        raise DownloadError(
            "Corrupt ZIP archive(s) in the session download: "
            + ", ".join(failed_zips)
            + ". Extraction did not complete.",
            details={"corrupt_zips": failed_zips},
        )


class ScanResult(NamedTuple):
    """One scan download attempt."""

    scan_id: str
    ok: bool
    files: int
    message: str


class DownloadOutcome(NamedTuple):
    """What a parallel session download actually achieved.

    Returned rather than discarded because the caller has to decide the exit
    code: a download that lost scans is not a success, and for a long time it
    was reported as one.
    """

    succeeded: int
    failed: list[tuple[str, str]]
    files: int


class VerificationManifest(NamedTuple):
    """Server-reported checksums for a downloaded scope, keyed by path.

    See :meth:`DownloadService.build_verification_manifest`.
    """

    digests: dict[str, str | None]
    collisions: list[str]


class _ManifestCollector:
    """Accumulates ``key -> digest`` entries for :meth:`DownloadService.build_verification_manifest`.

    A second row landing on an already-seen key is only a harmless repeat of
    the exact same file (identical identity and digest) or a real ambiguity
    between two different files. Silently keeping whichever was seen last
    would hide the latter, so a genuine collision is pulled out of the
    manifest entirely and reported instead of resolved arbitrarily.
    """

    def __init__(self) -> None:
        self.manifest: dict[str, str | None] = {}
        self.collisions: set[str] = set()
        self._fingerprints: dict[str, tuple[str, str | None]] = {}

    def ingest(self, rows: list[dict[str, object]], *, label: str, scan_id: str | None) -> None:
        """Record every file row's key -> digest, deriving the key from its URI.

        Args:
            rows: Raw file rows from :meth:`ResourceService.list_file_rows`.
            label: The resource label these rows belong to (unencoded).
            scan_id: The scan these rows belong to, or None for a
                session-level resource. Used only for the fallback key (a
                row with no URI to parse) -- built with the exact same
                ``scans/{scan_id}/resources/{label}/...`` /
                ``resources/{label}/...`` shape :func:`verify.key_from_uri`
                itself returns, so the two forms can never disagree for the
                same scan/label/name.
        """
        for row in rows:
            uri = str(row.get("URI") or row.get("uri") or "")
            key = verify.key_from_uri(uri.strip("/").split("/")) if uri else None
            name = str(row.get("Name") or row.get("name") or "")
            if key is None:
                if not name:
                    continue
                key = (
                    f"scans/{scan_id}/resources/{label}/{name}"
                    if scan_id is not None
                    else f"resources/{label}/{name}"
                )
            self._record(key, row.get("digest"), uri or key)

    def _record(self, key: str, digest: object, identity: str) -> None:
        # A key that has already collided stays collided permanently: without
        # this, a later row whose fingerprint happens to match one of the
        # (already-evicted) prior entries would look like a first insertion
        # and quietly repopulate the manifest.
        if key in self.collisions:
            return
        digest_value = digest if isinstance(digest, str) and digest else None
        # Lowercased before comparing so two rows reporting the identical
        # file with differently-cased hex digests are recognized as the same
        # file, not flagged as a collision.
        fingerprint = (identity, digest_value.lower() if digest_value else None)
        if key in self._fingerprints and self._fingerprints[key] != fingerprint:
            self.collisions.add(key)
            self.manifest.pop(key, None)
            self._fingerprints.pop(key, None)
            return
        self._fingerprints[key] = fingerprint
        self.manifest[key] = digest_value


class DownloadService(BaseService):
    """Service for XNAT download operations."""

    def _resolve_zip_experiment_ref(
        self,
        session_id: str,
        *,
        project: str | None = None,
        subject: str | None = None,
    ) -> ExperimentRef:
        """Resolve label-based experiment references to a canonical experiment ID."""
        if project and not session_id.startswith("XNAT_E"):
            source_ref = ExperimentRef(
                experiment=session_id,
                project_id=project,
                subject=subject,
                experiment_is_label=True,
                subject_is_label=subject is not None,
            )
            resolved = HierarchyService.parse_resolved_experiment(
                source_ref,
                self._get(
                    HierarchyService.build_experiment_path(source_ref),
                    params={"format": "json"},
                ),
            )
            return ExperimentRef(experiment=resolved.experiment_id)

        return ExperimentRef(experiment=session_id)

    def download_session_fast(  # noqa: C901  # pre-existing; see pyproject
        self,
        *,
        session_project: str,
        subject: str,
        resolved_session_id: str,
        session_dir: Path,
        workers: int = 8,
        include_resources: tuple[str, ...] = (),
        exclude_resources: tuple[str, ...] = (),
        on_start: Callable[[int], None] | None = None,
        on_scan_result: Callable[[ScanResult], None] | None = None,
    ) -> DownloadOutcome:
        """Download session scans in parallel and extract to standard structure.

        Uses a two-tier strategy:
        - No filter / exclude filter: one unfiltered request per scan
          (``/scans/{id}/files``), exclude applied during extraction.
        - Include filter: one request per (scan, resource) pair
          (``/scans/{id}/resources/{label}/files``).

        Args:
            session_project: Project ID.
            subject: Subject ID.
            resolved_session_id: Resolved XNAT experiment ID.
            session_dir: Output directory for session data.
            workers: Maximum parallel download workers.
            include_resources: Resource types to include (empty = all).
            exclude_resources: Resource types to exclude.
            on_start: Called once with the number of scans discovered, before
                downloading begins (including zero). Rendering is the caller's
                concern; the service prints nothing.
            on_scan_result: Called with each scan's result as it completes.

        Produces the XNAT compressed-uploader layout:
            {session_dir}/scans/{scan_id}/resources/{label}/files/{files...}
        """
        results = SessionService(self.client).scan_rows(resolved_session_id)
        scan_ids = [r["ID"] for r in results if r.get("ID")]

        if on_start is not None:
            on_start(len(scan_ids))

        if not scan_ids:
            return DownloadOutcome(succeeded=0, failed=[], files=0)

        exclude_set = frozenset(exclude_resources)

        # Two-tier task list: (scan_id, resource_label_or_None)
        download_tasks: list[tuple[str, str | None]] = []
        if include_resources:
            for sid in scan_ids:
                for res in include_resources:
                    download_tasks.append((sid, res))
        else:
            for sid in scan_ids:
                download_tasks.append((sid, None))

        def download_and_extract(
            scan_id: str,
            resource_label: str | None,
        ) -> ScanResult:
            """Download a scan ZIP and extract into standard layout."""
            base = (
                f"/data/projects/{session_project}/subjects/{subject}"
                f"/experiments/{resolved_session_id}/scans/{scan_id}"
            )
            if resource_label:
                scan_url = f"{base}/resources/{resource_label}/files"
            else:
                scan_url = f"{base}/files"

            # One shared XNATClient across worker threads: httpx.Client is
            # thread-safe and XNATClient.stream sends the session cookie per call
            # instead of mutating shared state, so the retry/auth/typed-error path
            # is reused here without a per-thread raw client.
            try:
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                try:
                    stream_to_file(self.client, scan_url, tmp_path, params={"format": "zip"})
                    scan_base = session_dir / "scans" / scan_id
                    extracted, renamed = _extract_scan_zip(
                        tmp_path,
                        scan_base,
                        resource_label=resource_label,
                        exclude_resources=exclude_set,
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

                parts = []
                if resource_label:
                    parts.append(resource_label)
                if extracted == 0:
                    parts.append("empty")
                if renamed:
                    parts.append(f"renamed {renamed} duplicates")
                status = ", ".join(parts) if parts else ""
                return ScanResult(scan_id, True, extracted, status)
            except ResourceNotFoundError:
                # A scan with no files of the requested type is normal under -r, so
                # this is not an error -- but it downloaded nothing, and the zero is
                # what stops an all-404 session (the failure mode ADR-0010
                # describes) reading as a complete download.
                label_desc = f" ({resource_label})" if resource_label else ""
                return ScanResult(scan_id, True, 0, f"no files{label_desc}")
            except Exception as e:
                return ScanResult(scan_id, False, 0, str(e))

        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        total_files = 0

        pool_size = min(len(download_tasks), workers)
        with cancellable_pool(pool_size) as (executor, _token):
            futures = {
                executor.submit(download_and_extract, sid, res): (sid, res)
                for sid, res in download_tasks
            }
            for future in as_completed(futures):
                result = future.result()
                if result.ok:
                    succeeded.append(result.scan_id)
                    total_files += result.files
                else:
                    failed.append((result.scan_id, result.message))
                if on_scan_result is not None:
                    on_scan_result(result)

        return DownloadOutcome(succeeded=len(succeeded), failed=failed, files=total_files)

    def download_session_archive(
        self,
        *,
        session_project: str,
        subject: str,
        resolved_session_id: str,
        session_dir: Path,
        progress_cb: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Stream the whole session as a single ``scans.zip`` (the sequential path).

        Args:
            session_project: Project ID.
            subject: Subject ID.
            resolved_session_id: Resolved XNAT experiment ID.
            session_dir: Output directory; the ZIP lands at ``scans.zip`` inside it.
            progress_cb: Forwarded to the streamer as ``(written, content_length)``.

        Returns:
            The path to the written ``scans.zip``.

        Raises:
            XNATCtlError: Any typed client-layer failure (authentication,
                permission, not-found) from streaming the archive.
            DownloadError: On a short read (Content-Length mismatch).
        """
        scans_url = (
            f"/data/projects/{session_project}/subjects/{subject}"
            f"/experiments/{resolved_session_id}/scans/ALL/files"
        )
        scans_zip = session_dir / "scans.zip"
        stream_to_file(
            self.client, scans_url, scans_zip, params={"format": "zip"}, progress_cb=progress_cb
        )
        return scans_zip

    def download_session_level_resources(
        self,
        *,
        session_project: str,
        subject: str,
        resolved_session_id: str,
        session_dir: Path,
        downloaded: list[tuple[str, Path]] | None = None,
    ) -> list[tuple[str, Path]]:
        """Download each session-level (outside-scans) resource as its own ZIP.

        Args:
            session_project: Project ID.
            subject: Subject ID.
            resolved_session_id: Resolved XNAT experiment ID.
            session_dir: Output directory; each resource lands at
                ``resources_{label}.zip``.
            downloaded: Appended to in place as each resource's ZIP finishes
                writing, rather than only assembled into a list returned at
                the very end. Pass a list a caller already holds a reference
                to, and if a later resource's download raises, everything
                appended before that failure is still visible there --
                instead of vanishing with the exception the way a
                return-only value would, losing provenance for resources
                that genuinely landed. With no need to rediscover them
                afterward by globbing the directory (which could just as
                easily find a stale ZIP left over from an earlier run).

        Returns:
            The same list *downloaded* points to (or a fresh one if it was
            not given) -- the ``(label, path)`` pair for each resource ZIP
            successfully written so far, in download order.

        Raises:
            XNATCtlError: Any typed client-layer failure while listing or
                streaming a resource.
            DownloadError: On a short read (Content-Length mismatch).
        """
        res_url = (
            f"/data/projects/{session_project}/subjects/{subject}"
            f"/experiments/{resolved_session_id}/resources"
        )
        sess_resources = SessionService(self.client).experiment_resource_rows(
            resolved_session_id, project=session_project, subject=subject
        )
        result = downloaded if downloaded is not None else []
        for res in sess_resources:
            label = res.get("label", "resource")
            zip_path = session_dir / f"resources_{label}.zip"
            stream_to_file(
                self.client,
                f"{res_url}/{label}/files",
                zip_path,
                params={"format": "zip"},
            )
            result.append((label, zip_path))
        return result

    def build_verification_manifest(
        self,
        *,
        session_id: str,
        project: str | None,
        subject: str | None = None,
        scan_ids: list[str] | None = None,
        include_resources: tuple[str, ...] = (),
        exclude_resources: tuple[str, ...] = (),
        resource_filter: str | None = None,
        include_session_resources: bool = False,
    ) -> VerificationManifest:
        """Fetch server-side checksums for a downloaded scan scope, keyed by path.

        Mirrors the scope :meth:`download_session_fast`/:meth:`download_scans`
        used to fetch the files in the first place: the same experiment
        resolution, and -- with *scan_ids* omitted -- the same flat, unscoped
        scan enumeration :meth:`download_session_fast` uses.

        Args:
            session_id: Session ID or label.
            project: Project ID (enables label resolution).
            subject: Subject ID/label, when known.
            scan_ids: Scans to cover; None covers every scan in the session.
            include_resources: Resource labels to include (empty = all).
            exclude_resources: Resource labels to exclude.
            resource_filter: A single resource label to scope every scan to,
                skipping the per-scan resource listing call. Mutually
                exclusive in practice with include/exclude, which only make
                sense when the resource set is discovered per scan.
            include_session_resources: Also cover session-level (outside-scans)
                resources, i.e. the ``--session-resources`` download scope.

        Returns:
            The digest map (see :func:`xnatctl.services.verify.key_from_uri`;
            a None digest means the server listed the file with no checksum)
            plus any key two different server-reported files both mapped to.
        """
        resolved = self._resolve_zip_experiment_ref(session_id, project=project, subject=subject)
        resolved_session_id = resolved.experiment
        experiment_ref = ExperimentRef(
            experiment=resolved_session_id, project_id=project, subject=subject
        )

        resource_svc = ResourceService(self.client)
        collector = _ManifestCollector()

        if scan_ids is None:
            rows = SessionService(self.client).scan_rows(resolved_session_id)
            scan_ids = [r["ID"] for r in rows if r.get("ID")]

        include_set = frozenset(include_resources)
        exclude_set = frozenset(exclude_resources)

        for scan_id in scan_ids:
            scan_ref = ScanRef(experiment=experiment_ref, scan_id=scan_id)
            if resource_filter:
                labels = [resource_filter]
            else:
                labels = [
                    str(r["label"]) for r in resource_svc.list_rows(scan_ref) if r.get("label")
                ]
                if include_set:
                    labels = [label for label in labels if label in include_set]
                elif exclude_set:
                    labels = [label for label in labels if label not in exclude_set]

            for label in labels:
                # `quote` matches ResourceService's other file-listing callers
                # (see cli/resource.py): a label may contain characters
                # (spaces, `#`) invalid unencoded in a URL path segment.
                rows = resource_svc.list_file_rows(scan_ref, quote(label))
                collector.ingest(rows, label=label, scan_id=scan_id)

        if include_session_resources:
            session_resource_rows = SessionService(self.client).experiment_resource_rows(
                resolved_session_id, project=project, subject=subject
            )
            for res in session_resource_rows:
                label = str(res.get("label") or "")
                if not label:
                    continue
                rows = resource_svc.list_file_rows(experiment_ref, quote(label))
                collector.ingest(rows, label=label, scan_id=None)

        return VerificationManifest(
            digests=collector.manifest, collisions=sorted(collector.collisions)
        )

    def verify_scan_downloads(
        self,
        *,
        session_id: str,
        project: str | None,
        subject: str | None = None,
        scan_ids: list[str] | None = None,
        include_resources: tuple[str, ...] = (),
        exclude_resources: tuple[str, ...] = (),
        resource_filter: str | None = None,
        include_session_resources: bool = False,
        local_root: Path | None = None,
        local_root_wrapped: bool = False,
        zip_paths: Sequence[verify.ZipSource] = (),
    ) -> VerificationReport:
        """Verify a completed download against server-reported MD5 checksums.

        Fetches the server-side file manifest for the same scope the download
        used (see :meth:`build_verification_manifest`), then compares it
        against the files on disk: an extracted tree (*local_root*) and/or one
        or more unextracted archives (*zip_paths*, not mutually exclusive with
        *local_root* -- session-level resources can remain as separate,
        un-extracted ZIPs alongside an extracted scan tree), streamed rather
        than loaded whole into memory either way.

        Args:
            session_id: Session ID or label.
            project: Project ID (enables label resolution).
            subject: Subject ID/label, when known.
            scan_ids: Scans to verify; None covers every scan in the session.
            include_resources: Resource labels to include (empty = all).
            exclude_resources: Resource labels to exclude.
            resource_filter: A single resource label the download was scoped to.
            include_session_resources: Also cover session-level resources.
            local_root: Root of an extracted download tree.
            local_root_wrapped: Whether *local_root*'s tree carries a
                session/experiment-label wrapper -- see
                :func:`xnatctl.services.verify.scan_source_key`. The caller
                already knows this from how it produced *local_root*.
            zip_paths: Unextracted archive(s) to verify against too. Each
                entry is a bare path or a ``(path, label)`` pair overriding
                *resource_filter* for that one archive.

        Returns:
            The comparison report, its ``collisions`` including both
            server-side ambiguities from the manifest and local-side ones
            found while indexing *local_root*/*zip_paths*.
        """
        manifest = self.build_verification_manifest(
            session_id=session_id,
            project=project,
            subject=subject,
            scan_ids=scan_ids,
            include_resources=include_resources,
            exclude_resources=exclude_resources,
            resource_filter=resource_filter,
            include_session_resources=include_session_resources,
        )
        report = verify.verify_manifest(
            manifest.digests,
            local_root=local_root,
            local_root_wrapped=local_root_wrapped,
            zip_paths=zip_paths,
            resource_label=resource_filter,
        )
        if manifest.collisions:
            report.collisions = sorted(set(report.collisions) | set(manifest.collisions))
        return report

    def download_resource(
        self,
        session_id: str,
        resource_label: str,
        output_dir: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        zip_filename: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            output_dir: Output directory
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP files (default: False)
            zip_filename: Custom ZIP filename (default: {resource_label}.zip)
            progress_callback: Progress callback

        Returns:
            DownloadSummary describing the completed download (always a success;
            failures raise).

        Raises:
            XNATCtlError: A typed failure from the client layer passes through
                untouched -- authentication, permission, not-found, or a
                short-read DownloadError. The one carve-out: when ``session_id``
                is a label needing resolution to an experiment ID, a non-404,
                non-auth typed failure during that resolution step (a network
                hiccup, a 5xx) is swallowed into a best-effort fallback that
                treats ``session_id`` as the experiment ID directly, rather
                than raised here.
            DownloadError: Any other failure (OSError, corrupt ZIP, unexpected
                exception) wrapped with the resource label and ``__cause__`` set.
        """
        start_time = time.time()
        output_dir = Path(output_dir)

        def notify_error(exc: Exception) -> None:
            # The notification must never mask the failure it reports, so a
            # raising callback is suppressed.
            if progress_callback is None:
                return
            with contextlib.suppress(Exception):
                progress_callback(
                    DownloadProgress(
                        phase=OperationPhase.ERROR,
                        message=str(exc),
                        success=False,
                        errors=[str(exc)],
                    )
                )

        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                resolved_experiment_ref = self._resolve_zip_experiment_ref(
                    session_id,
                    project=project,
                )
            except AuthenticationError:
                # Covers SessionExpiredError and PermissionDeniedError too --
                # an auth failure here will just fail again on the fallback
                # path, so surfacing it directly is more honest than masking
                # it with a doomed retry.
                raise
            except (ResourceNotFoundError, ValueError):
                # A definitive 404 or a malformed response means the
                # identifier itself is bad, not that resolution merely
                # hiccuped -- it must not be swallowed by the fallback below.
                raise
            except XNATCtlError:
                # Any other typed failure (network, server, retry-exhausted)
                # is deliberately discarded: resolution is best-effort
                # normalization, and a transient hiccup here must not doom an
                # otherwise-valid accession ID. The fallback retries via the
                # direct /data/experiments/{id} path -- if that also fails,
                # ITS error is the one that propagates under this method's
                # contract.
                resolved_experiment_ref = ExperimentRef(experiment=session_id)

            # Build path - always use /data/experiments/{id}/... for reliable ZIP downloads
            if scan_id:
                path = HierarchyService.build_resource_path(
                    ResourceRef(
                        parent=ScanRef(experiment=resolved_experiment_ref, scan_id=scan_id),
                        resource_label=resource_label,
                    ),
                    "files",
                )
            else:
                path = HierarchyService.build_resource_path(
                    ResourceRef(parent=resolved_experiment_ref, resource_label=resource_label),
                    "files",
                )

            params = {"format": "zip"}

            zip_path = output_dir / (zip_filename or f"{resource_label}.zip")

            progress_cb: Callable[[int, int | None], None] | None = None
            if progress_callback is not None:
                emit = progress_callback

                def progress_cb(written: int, total: int | None) -> None:
                    emit(
                        DownloadProgress(
                            phase=OperationPhase.DOWNLOADING,
                            bytes_received=written,
                            total_bytes=total or 0,
                            file_path=str(zip_path),
                        )
                    )

            total_bytes = stream_to_file(
                self.client, path, zip_path, params=params, progress_cb=progress_cb
            ).bytes_written

            file_count = 1
            if extract:
                extract_dir = output_dir / resource_label
                _safe_extract_zip(zip_path, extract_dir)
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                zip_path.unlink()

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=1,
                succeeded=1,
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=str(output_dir),
                session_id=session_id,
            )

        except XNATCtlError as e:
            # Typed failures already carry the right class and exit code -- an
            # expired session, a permission denial, a 404, or the DownloadError
            # stream_to_file raises on a short read. Passing them through is the
            # whole point: a caller can distinguish them instead of reading a
            # stringified summary.
            notify_error(e)
            raise
        except Exception as e:
            notify_error(e)
            raise DownloadError(str(e), resource=resource_label) from e

    def download_scan(
        self,
        session_id: str,
        scan_id: str,
        output_dir: Path,
        project: str | None = None,
        resource: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            output_dir: Output directory
            project: Project ID
            resource: Resource type to download (None = all resources)
            progress_callback: Progress callback

        Returns:
            DownloadSummary describing the download. With ``resource=None`` this
            is the multi-scan batch summary from :meth:`download_scans` (call its
            ``raise_for_status`` to fail on a partial result); with a resource it
            is the always-success summary from :meth:`download_resource`.

        Raises:
            XNATCtlError: A typed client-layer failure from the single-resource
                path (:meth:`download_resource`) passes through untouched.
            DownloadError: Any other single-resource failure, wrapped.
        """
        if resource is None:
            return self.download_scans(
                session_id=session_id,
                scan_ids=[scan_id],
                output_dir=output_dir,
                project=project,
                resource=None,
                progress_callback=progress_callback,
            )
        return self.download_resource(
            session_id=session_id,
            resource_label=resource,
            output_dir=output_dir,
            scan_id=scan_id,
            project=project,
            progress_callback=progress_callback,
        )

    def download_scans(
        self,
        session_id: str,
        scan_ids: list[str],
        output_dir: Path,
        project: str | None = None,
        subject: str | None = None,
        resource: str | None = None,
        zip_filename: str | None = None,
        extract: bool = False,
        cleanup: bool = True,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download multiple scans in a single request.

        Uses XNAT's comma-separated scan ID feature for efficient batch downloads.
        When resource is None, downloads ALL files (DICOM + SNAPSHOTS).

        Args:
            session_id: Session ID or label
            scan_ids: List of scan IDs (or ["ALL"] for all scans)
            output_dir: Output directory
            project: Project ID (required when using session label)
            subject: Subject ID/label (optional, narrows experiment lookup)
            resource: Resource type (None = all resources, "DICOM" = DICOM only)
            zip_filename: Output ZIP filename (default: scans.zip)
            extract: Extract ZIP after download
            cleanup: Remove ZIP after successful extraction (with extract=True)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results. This is a batch operation: a failed
            fetch is reported as ``success=False`` with the reason in ``errors``
            rather than raised. Call ``raise_for_status()`` on the summary to
            turn a failed batch into a ``BatchOperationError``.
        """
        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            resolved_experiment_ref = self._resolve_zip_experiment_ref(
                session_id,
                project=project,
                subject=subject,
            )
        except AuthenticationError:
            raise
        except (ResourceNotFoundError, ValueError):
            # A definitive 404 or a malformed response means the identifier
            # itself is bad; do not paper over it with the fallback below.
            raise
        except XNATCtlError:
            # Best-effort normalization -- see the sibling try/except in
            # download_resource for the full rationale.
            resolved_experiment_ref = ExperimentRef(experiment=session_id)

        scan_spec = ",".join(scan_ids) if len(scan_ids) > 1 else scan_ids[0]

        if resource:
            path = HierarchyService.build_resource_path(
                ResourceRef(
                    parent=ScanRef(experiment=resolved_experiment_ref, scan_id=scan_spec),
                    resource_label=resource,
                ),
                "files",
            )
        else:
            path = HierarchyService.build_scan_path(
                ScanRef(experiment=resolved_experiment_ref, scan_id=scan_spec),
                "files",
            )

        params = {"format": "zip"}
        zip_path = output_dir / (zip_filename or "scans.zip")

        try:
            progress_cb: Callable[[int, int | None], None] | None = None
            if progress_callback is not None:
                emit = progress_callback

                def progress_cb(written: int, total: int | None) -> None:
                    emit(
                        DownloadProgress(
                            phase=OperationPhase.DOWNLOADING,
                            bytes_received=written,
                            total_bytes=total or 0,
                            file_path=str(zip_path),
                        )
                    )

            total_bytes = stream_to_file(
                self.client, path, zip_path, params=params, progress_cb=progress_cb
            ).bytes_written

            file_count = 1
            output_path = str(zip_path)
            if extract:
                extract_dir = output_dir / "scans"
                _safe_extract_zip(zip_path, extract_dir)
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                if cleanup:
                    zip_path.unlink()
                output_path = str(extract_dir)

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=len(scan_ids),
                succeeded=len(scan_ids),
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=output_path,
                session_id=session_id,
            )

        except Exception as e:
            duration = time.time() - start_time
            return DownloadSummary(
                success=False,
                total=len(scan_ids),
                succeeded=0,
                failed=len(scan_ids),
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )
