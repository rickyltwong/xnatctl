"""Parallel session-scoped download engine.

Owns the per-scan parallel download/extract path (:meth:`download_session_fast`)
and the two sequential whole-session paths: one archive
(:meth:`download_session_archive`) and per-resource ZIPs
(:meth:`download_session_level_resources`).
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from concurrent.futures import as_completed
from pathlib import Path
from typing import NamedTuple

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.core.validation import (
    check_no_casefold_collision,
    quote_path_segment,
    validate_local_path_component,
    verify_directory_contained_in,
)

from ..base import BaseService
from ..sessions import SessionService
from ..zip_extract import _extract_scan_zip
from .shared import _reject_empty_resource_filter_values
from .transport import stream_to_file

logger = logging.getLogger(__name__)


class ScanResult(NamedTuple):
    """One scan download attempt."""

    scan_id: str
    ok: bool
    files: int
    message: str
    skipped_unsafe_entries: int = 0


class DownloadOutcome(NamedTuple):
    """What a parallel session download actually achieved.

    Returned rather than discarded because the caller has to decide the exit
    code: a download that lost scans is not a success, and for a long time it
    was reported as one.
    """

    succeeded: int
    failed: list[tuple[str, str]]
    files: int
    skipped_unsafe_entries: int = 0


class _SessionDownloadMixin(BaseService):
    """Mixin providing session-scoped download methods."""

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

        # Created once here, single-threaded, before any worker starts --
        # not left for each worker's own `_extract_scan_zip` to create on
        # first use. Every scan's `scan_base` (`session_dir/scans/{id}`)
        # shares this one parent; on Windows, `Path.resolve()` only
        # canonicalizes (corrects case, expands any 8.3 short name, follows
        # a substituted drive/junction) the deepest ALREADY-EXISTING
        # ancestor of a path, appending anything below that literally
        # (`ntpath._getfinalpathname_nonstrict`). If this call raced two
        # workers both creating `scans/` for the first time, one worker's
        # `verify_directory_contained_in` below could resolve `scan_base`
        # against a `scans/` that only *partially* existed at that instant,
        # then disagree with `session_dir`'s own (fully resolved) form and
        # fail containment on a perfectly safe directory -- exactly the
        # class of bug `_extract_scan_zip`'s own early `scan_base.mkdir()`
        # fixes for ITS internal resolve, but that fix runs too late to help
        # this earlier check. Creating the shared parent up front, before
        # any thread touches it, removes the race outright.
        (session_dir / "scans").mkdir(parents=True, exist_ok=True)

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
                    extracted, renamed, skipped = _extract_scan_zip(
                        tmp_path,
                        scan_base,
                        resource_label=resource_label,
                        exclude_resources=exclude_set,
                    )
                    if skipped:
                        logger.warning(
                            "Skipped %d unsafe ZIP entries during extraction: %s",
                            len(skipped),
                            skipped[:5],
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
                return ScanResult(scan_id, True, extracted, status, len(skipped))
            except ResourceNotFoundError:
                # A scan with no files of the requested type is normal under -r, so
                # this is not an error -- but it downloaded nothing, and the zero is
                # what stops an all-404 session (XNAT answers a mis-routed URL with
                # a silent 404/empty 200) reading as a complete download.
                label_desc = f" ({resource_label})" if resource_label else ""
                return ScanResult(scan_id, True, 0, f"no files{label_desc}")
            except Exception as e:  # noqa: BLE001 -- per-scan worker-pool isolation
                return ScanResult(scan_id, False, 0, str(e))

        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        total_files = 0
        total_skipped = 0

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
                total_skipped += result.skipped_unsafe_entries
                if on_scan_result is not None:
                    on_scan_result(result)

        return DownloadOutcome(
            succeeded=len(succeeded),
            failed=failed,
            files=total_files,
            skipped_unsafe_entries=total_skipped,
        )

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
