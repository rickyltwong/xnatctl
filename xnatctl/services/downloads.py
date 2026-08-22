"""Download service for XNAT download operations."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    AuthenticationError,
    DownloadError,
    InputValidationError,
    PathValidationError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.core.validation import (
    check_no_casefold_collision,
    quote_path_segment,
    validate_local_path_component,
    validate_xnat_resource_label,
    verify_directory_contained_in,
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
from .zip_extract import _extract_scan_zip, _safe_extract_zip

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


def _safe_output_path(output_dir: Path, filename: str | None, default: str) -> Path:
    """Resolve a caller-supplied output filename, rejecting path traversal.

    ``zip_filename`` on :meth:`DownloadService.download_resource` and
    :meth:`DownloadService.download_scans` is an optional override -- the CLI
    never exposes it as a flag, but the library surface does, so nothing
    upstream has validated it. ``None`` means "use the default name" and is
    the real omitted case; an explicitly-supplied empty/whitespace-only
    STRING is a caller mistake and must not be silently substituted with
    ``default`` the same way.

    A subdirectory-bearing value (``"sub/dir.zip"``) is legal -- XNAT and
    the CLI both use ``/`` for this regardless of host OS -- but each ``/``-
    separated component is validated on its own via
    :func:`~xnatctl.core.validation.validate_local_path_component`, the same
    check every other identifier-derived local path piece in this module
    goes through. That also means a leading or trailing ``/`` (an absolute
    path, or a stray empty component) is rejected: it splits out an empty
    string component, which the component validator already refuses.
    Joining the whole thing onto ``output_dir`` unchecked would otherwise let
    a caller (or anything that echoes attacker-controlled text into it)
    escape the output directory with a name like ``../../etc/cron.d/x``, or
    -- component-by-component checking aside -- reach Windows path handling
    unvalidated the way a single-component value already does elsewhere.

    Raises:
        PathValidationError: If ``filename`` is supplied but empty or
            whitespace-only, if any ``/``-separated component fails
            :func:`~xnatctl.core.validation.validate_local_path_component`,
            or if the resolved result escapes ``output_dir``.
    """
    if filename is not None:
        if filename.strip() == "":
            raise PathValidationError(
                filename, "output filename cannot be empty or whitespace-only"
            )
        for part in filename.split("/"):
            validate_local_path_component(part, "output filename component")
        effective_name = filename
    else:
        effective_name = default

    candidate = output_dir / effective_name
    resolved_dir = output_dir.resolve()
    if not candidate.resolve().is_relative_to(resolved_dir):
        raise PathValidationError(
            str(filename), "output filename must not escape the output directory"
        )
    return candidate


def _reject_empty_resource_filter_values(
    include_resources: tuple[str, ...], exclude_resources: tuple[str, ...]
) -> None:
    """Reject an empty/whitespace-only element in a resource include/exclude filter.

    An EMPTY tuple (the default) legitimately means "no filter" -- but an
    empty STRING inside a non-empty tuple is a different thing, and every
    caller downstream checks the tuple's truthiness (``if include_resources:``)
    or a resource label's truthiness (``if resource_label:``) to decide
    in/unfiltered scope. A stray ``""`` element would satisfy the tuple
    truthiness check (turning ON the include-filter branch) while never
    matching a real resource label -- either silently narrowing to zero
    results (nothing is ever labelled ``""``) or, worse, falling through a
    later per-item ``if resource_label:`` check into the UNFILTERED request
    for that one item. Neither is what the caller asked for, so this fails
    loudly instead.
    """
    for value in (*include_resources, *exclude_resources):
        if value.strip() == "":
            raise InputValidationError(
                "include_resources/exclude_resources cannot contain an empty or "
                "whitespace-only value",
                field="resource filter",
                value=value,
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
        # `is not None`, not truthy: `project=""` used to skip this branch
        # entirely (treated the same as "no project"), silently using
        # session_id AS an accession ID without resolving it -- wrong if
        # it's actually a label. `is not None` routes "" into
        # ExperimentRef(project_id=""), which raises via the ref's own
        # validation instead.
        if project is not None and not session_id.startswith("XNAT_E"):
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

        Raises:
            InputValidationError: If ``include_resources`` or
                ``exclude_resources`` contains an empty/whitespace-only value.
            PathValidationError: If two scans' IDs collide case-insensitively
                (they would extract into the same local directory on a
                case-insensitive filesystem -- Windows, or macOS/HFS+ by
                default).
        """
        _reject_empty_resource_filter_values(include_resources, exclude_resources)

        results = SessionService(self.client).scan_rows(resolved_session_id)
        scan_ids = [r["ID"] for r in results if r.get("ID")]

        # Checked once, sequentially, before any download starts (and before
        # the parallel pool below, which is why this doesn't need a lock) --
        # a case collision between two scan IDs is a structural problem with
        # the whole batch, not a single scan's failure.
        seen_scan_dirs: set[str] = set()
        for sid in scan_ids:
            check_no_casefold_collision(sid, seen_scan_dirs, "scan_id")

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
                f"/data/projects/{quote_path_segment(session_project)}"
                f"/subjects/{quote_path_segment(subject)}"
                f"/experiments/{quote_path_segment(resolved_session_id)}"
                f"/scans/{quote_path_segment(scan_id)}"
            )
            if resource_label is not None:
                scan_url = f"{base}/resources/{quote_path_segment(resource_label)}/files"
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
                    # `scan_id` is server-reported (scan_rows), not caller
                    # input, but a misconfigured/malicious server could still
                    # hand back something traversal-shaped -- a hostile ID
                    # fails this one scan (caught below) rather than being
                    # silently aliased onto a generic local folder.
                    scan_base = (
                        session_dir / "scans" / validate_local_path_component(scan_id, "scan_id")
                    )
                    # A pre-existing symlink at exactly this path (a prior
                    # run, a race, deliberate planting) would resolve
                    # OUTSIDE session_dir -- _extract_scan_zip's own
                    # containment check then anchors to that escaped
                    # location and passes trivially. Verified one level up,
                    # against the true caller-supplied root.
                    verify_directory_contained_in(scan_base, session_dir, "scan directory")
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
            f"/data/projects/{quote_path_segment(session_project)}"
            f"/subjects/{quote_path_segment(subject)}"
            f"/experiments/{quote_path_segment(resolved_session_id)}/scans/ALL/files"
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
            PathValidationError: If a server-reported resource label is not
                safe to use as a local filename component, or two resource
                labels collide case-insensitively (they would produce the
                same local ZIP filename on a case-insensitive filesystem --
                Windows, or macOS/HFS+ by default).
        """
        res_url = (
            f"/data/projects/{quote_path_segment(session_project)}"
            f"/subjects/{quote_path_segment(subject)}"
            f"/experiments/{quote_path_segment(resolved_session_id)}/resources"
        )
        sess_resources = SessionService(self.client).experiment_resource_rows(
            resolved_session_id, project=session_project, subject=subject
        )
        result = downloaded if downloaded is not None else []
        seen_resource_names: set[str] = set()
        for res in sess_resources:
            label = res.get("label", "resource")
            # `label` is server-reported, not caller input, but a resource
            # label is still attacker-influenceable in principle (whoever
            # created it on the server) -- a hostile label fails this
            # resource's download (see the method's Raises) rather than being
            # silently aliased onto a generic local filename, while the raw
            # label is still what the URL and the returned tuple use (the
            # verification manifest keys on the literal label).
            safe_name = validate_local_path_component(label, "resource label")
            check_no_casefold_collision(safe_name, seen_resource_names, "resource label")
            zip_path = session_dir / f"resources_{safe_name}.zip"
            stream_to_file(
                self.client,
                f"{res_url}/{quote_path_segment(label)}/files",
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
    ) -> verify.VerificationManifest:
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

        Raises:
            InputValidationError: If ``include_resources`` or
                ``exclude_resources`` contains an empty/whitespace-only value.
        """
        _reject_empty_resource_filter_values(include_resources, exclude_resources)

        resolved = self._resolve_zip_experiment_ref(session_id, project=project, subject=subject)
        resolved_session_id = resolved.experiment
        experiment_ref = ExperimentRef(
            experiment=resolved_session_id, project_id=project, subject=subject
        )

        resource_svc = ResourceService(self.client)
        collector = verify.ManifestCollector()

        if scan_ids is None:
            rows = SessionService(self.client).scan_rows(resolved_session_id)
            scan_ids = [r["ID"] for r in rows if r.get("ID")]

        include_set = frozenset(include_resources)
        exclude_set = frozenset(exclude_resources)

        for scan_id in scan_ids:
            scan_ref = ScanRef(experiment=experiment_ref, scan_id=scan_id)
            # `is not None`, not truthy: `resource_filter=""` is a caller
            # mistake, not "no single-resource scope" -- it must not silently
            # widen verification to every resource on the scan.
            # list_file_rows below rejects the empty string via ResourceRef.
            if resource_filter is not None:
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
                rows = resource_svc.list_file_rows(scan_ref, label)
                collector.ingest(rows, label=label, scan_id=scan_id)

        if include_session_resources:
            session_resource_rows = SessionService(self.client).experiment_resource_rows(
                resolved_session_id, project=project, subject=subject
            )
            for res in session_resource_rows:
                label = str(res.get("label") or "")
                if not label:
                    continue
                rows = resource_svc.list_file_rows(experiment_ref, label)
                collector.ingest(rows, label=label, scan_id=None)

        return verify.VerificationManifest(
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

            # Build path - always use /data/experiments/{id}/... for reliable ZIP downloads.
            # `is not None`, not truthy: `scan_id=""` is a caller mistake, not
            # "no scan scope" -- it must not silently widen to a session-level
            # resource. ScanRef's own validation rejects the empty string.
            if scan_id is not None:
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

            # `resource_label` reaches Windows path handling raw wherever it
            # is joined onto a local path below (e.g. "C:escape" is
            # drive-relative, discarding the base entirely) unless validated
            # first -- validate_xnat_resource_label above only covers URL
            # safety, not local-filesystem safety.
            safe_resource_label = validate_local_path_component(resource_label, "resource_label")
            zip_path = _safe_output_path(output_dir, zip_filename, f"{safe_resource_label}.zip")

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
                extract_dir = output_dir / safe_resource_label
                # A pre-existing symlink at exactly this path would resolve
                # OUTSIDE output_dir -- _safe_extract_zip's own containment
                # check then anchors to that escaped location. Verified one
                # level up, against the true caller-supplied root.
                verify_directory_contained_in(extract_dir, output_dir, "extraction directory")
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

        Raises:
            InputValidationError: If ``scan_ids`` is empty, or contains an
                empty/whitespace-only ID. An empty list would otherwise join
                to an empty batch spec (``/scans//files`` -- a malformed,
                different route), and this is caught before any HTTP call or
                filesystem write, not folded into the batch-failure summary
                the way a stream failure is.
        """
        if not scan_ids:
            raise InputValidationError("scan_ids cannot be empty", field="scan_ids", value=scan_ids)
        if any(not scan_id.strip() for scan_id in scan_ids):
            raise InputValidationError(
                "scan_ids cannot contain an empty or whitespace-only ID",
                field="scan_ids",
                value=scan_ids,
            )

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

        # XNAT's own comma-delimited multi-scan syntax (`/scans/1,2,3/files`) --
        # NOT a single opaque path segment, so it cannot go through ScanRef +
        # the normal builder chain the way one scan ID does: that would quote
        # the whole joined string as one segment, percent-encoding the very
        # commas that make it a batch spec (and double-encoding any ID that
        # itself needed escaping). Each ID is quoted on its own instead, then
        # rejoined with a literal comma.
        scans_suffix = ",".join(quote_path_segment(scan_id) for scan_id in scan_ids)
        scans_base_path = HierarchyService.build_experiment_path(
            HierarchyService.routable_scan_parent(resolved_experiment_ref), "scans"
        )

        # `is not None`, not truthy: `resource=""` is a caller mistake, not
        # "no resource filter" -- it must not silently widen to all
        # resources. validate_xnat_resource_label rejects it explicitly.
        if resource is not None:
            resource = validate_xnat_resource_label(resource)
            path = (
                f"{scans_base_path}/{scans_suffix}/resources/{quote_path_segment(resource)}/files"
            )
        else:
            path = f"{scans_base_path}/{scans_suffix}/files"

        params = {"format": "zip"}
        zip_path = _safe_output_path(output_dir, zip_filename, "scans.zip")

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
                # "scans" is a fixed literal, not identifier-derived, but the
                # same symlink risk applies to any pre-existing entry at this
                # path -- verified against the true caller-supplied root.
                verify_directory_contained_in(extract_dir, output_dir, "extraction directory")
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
