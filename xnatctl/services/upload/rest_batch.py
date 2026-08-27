"""Parallel REST batch upload transport.

Splits a DICOM directory into N batches, archives each batch (tar/zip), and
uploads the archives in parallel through the XNAT import service. Each batch
goes through :func:`~xnatctl.services.upload.rest_archive.upload_single_archive`.
"""

from __future__ import annotations

import logging
import shutil
import ssl
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken, cancellable_pool
from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    OperationCancelledError,
)
from xnatctl.core.server_version import MIN_VERSION_DIRECT_ARCHIVE, require_server_version
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.models.progress import OperationPhase, UploadProgress, UploadSummary

from .archives import _create_archive
from .rest_archive import upload_single_archive
from .shared import (
    DEFAULT_UPLOAD_WORKERS,
    SessionRefresher,
    collect_dicom_files,
    split_into_n_batches,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500
DEFAULT_ARCHIVE_WORKERS = 4
DEFAULT_ARCHIVE_FORMAT = "tar"
DEFAULT_TIMEOUT = DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_IMPORT_HANDLER = "DICOM-zip"
DEFAULT_OVERWRITE = "delete"


@dataclass
class _UploadResult:
    """Result of a single batch upload (internal)."""

    batch_id: int
    success: bool
    duration: float
    file_count: int
    archive_size: int
    error: str = ""
    # A batch dropped because the user interrupted the run. Distinct from a
    # failure: nothing went wrong with it, so it must not be reported as an
    # error the server rejected.
    cancelled: bool = False


@dataclass(frozen=True)
class _BatchUploadConfig:
    """Static per-batch upload configuration, identical for every batch in a run."""

    client: XNATClient
    username: str | None
    password: str | None
    session_token: str | None
    verify_ssl: bool | ssl.SSLContext
    timeout: int
    project: str
    subject: str
    session: str
    import_handler: str
    ignore_unparsable: bool
    overwrite: str
    direct_archive: bool


def _cancelled_result(
    batch_id: int,
    batch: list[Path],
    duration: float = 0.0,
    archive_size: int = 0,
) -> _UploadResult:
    """Build the result for a batch dropped by user cancellation.

    Distinct from a failure: nothing went wrong with it, so it must not be
    reported as an error the server rejected.

    Args:
        batch_id: 1-based batch number.
        batch: The batch's files, for the file count.
        duration: Elapsed time, 0.0 when no work started.
        archive_size: Bytes archived before the cancellation, if any.

    Returns:
        A cancelled (not failed) _UploadResult.
    """
    return _UploadResult(
        batch_id=batch_id,
        success=False,
        duration=duration,
        file_count=len(batch),
        archive_size=archive_size,
        error="cancelled",
        cancelled=True,
    )


def _create_and_upload_batch(
    *,
    batch: list[Path],
    archive_path: Path,
    source_path: Path,
    archive_format: str,
    batch_id: int,
    config: _BatchUploadConfig,
    cancel_token: CancellationToken = NULL_TOKEN,
    session_refresher: SessionRefresher | None = None,
) -> _UploadResult:
    """Create archive, upload it, then delete the archive immediately.

    Combines archive creation and upload into a single task to reduce peak
    disk and memory usage. The archive is deleted as soon as the upload
    completes (or fails), preventing all archives from existing on disk
    simultaneously.

    Checks for cancellation before doing any work: ``cancel_futures`` drops the
    queue, but a worker that has already picked up a batch would otherwise go
    on to build and upload an archive the user asked it to abandon.
    """
    start_time = time.time()
    archive_size = 0

    if cancel_token.cancelled:
        return _cancelled_result(batch_id, batch)

    try:
        archive_size = _create_archive(batch, archive_path, source_path, archive_format)

        upload_result = upload_single_archive(
            xnat_client=config.client,
            username=config.username,
            password=config.password,
            session_token=config.session_token,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
            archive_path=archive_path,
            project=config.project,
            subject=config.subject,
            session=config.session,
            import_handler=config.import_handler,
            ignore_unparsable=config.ignore_unparsable,
            overwrite=config.overwrite,
            direct_archive=config.direct_archive,
            cancel_token=cancel_token,
            session_refresher=session_refresher,
        )

        return _UploadResult(
            batch_id=batch_id,
            success=upload_result.success,
            duration=time.time() - start_time,
            file_count=len(batch),
            archive_size=archive_size,
            error=upload_result.error,
        )
    except OperationCancelledError:
        return _cancelled_result(
            batch_id, batch, duration=time.time() - start_time, archive_size=archive_size
        )
    except Exception as e:  # noqa: BLE001  # per-batch worker-pool isolation: one archive-worker's failure must not abort the parallel batch
        return _UploadResult(
            batch_id=batch_id,
            success=False,
            duration=time.time() - start_time,
            file_count=len(batch),
            archive_size=archive_size,
            error=str(e),
        )
    finally:
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001  # best-effort cleanup: temp archive removal must not fail the upload
            pass


def upload_dicom_parallel(
    client: XNATClient,
    source_dir: Path,
    project: str,
    subject: str,
    session: str,
    *,
    username: str | None = None,
    password: str | None = None,
    upload_workers: int = DEFAULT_UPLOAD_WORKERS,
    archive_workers: int = DEFAULT_ARCHIVE_WORKERS,
    archive_format: str = DEFAULT_ARCHIVE_FORMAT,
    import_handler: str = DEFAULT_IMPORT_HANDLER,
    ignore_unparsable: bool = True,
    overwrite: str = DEFAULT_OVERWRITE,
    direct_archive: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    progress_callback: Callable[[UploadProgress], None] | None = None,
) -> UploadSummary:
    """Upload DICOM files using parallel batched archives via REST import.

    High-throughput upload that:
    1. Collects DICOM files from the source directory
    2. Splits files into N batches (N = upload_workers)
    3. Creates archives in parallel
    4. Uploads archives in parallel with per-thread HTTP clients

    Args:
        client: Bound XNAT client (source of base URL, token, credentials).
        source_dir: Directory containing DICOM files.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.
        username: XNAT username (override for per-thread auth).
        password: XNAT password (override for per-thread auth).
        upload_workers: Parallel upload workers (default: 4).
        archive_workers: Parallel archive workers (default: 4).
        archive_format: Archive format, "tar" or "zip" (default: tar).
        import_handler: XNAT import handler (default: DICOM-zip).
        ignore_unparsable: Skip unparsable DICOM files (default: True).
        overwrite: Overwrite mode: none, append, delete (default: delete).
        direct_archive: Use direct archive vs prearchive (default: True).
        timeout: HTTP timeout in seconds.
        progress_callback: Optional callback for progress updates.

    Returns:
        UploadSummary with results.

    Raises:
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
    """
    total_start = time.time()
    errors: list[str] = []

    def report(phase: OperationPhase, **kwargs: Any) -> None:
        if progress_callback:
            progress_callback(UploadProgress(phase=phase, **kwargs))

    # Phase 1: Collect files. Local validation (an empty/unscannable
    # directory) runs before the version gate below, so a source with no
    # DICOM files gets the "No DICOM files found" summary regardless of the
    # server's version, rather than an UnsupportedServerVersionError that
    # has nothing to do with why the upload can't proceed.
    report(OperationPhase.PREPARING, message="Scanning for DICOM files...")

    files_or_summary = _collect_files_or_summary(source_dir, total_start)
    if isinstance(files_or_summary, UploadSummary):
        return files_or_summary
    files = files_or_summary

    # The gate still precedes any archive creation or network upload -- it
    # just runs after the local file-collection check above.
    if direct_archive:
        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    config = _BatchUploadConfig(
        client=client,
        username=username or client.username,
        password=password or client.password,
        session_token=client.session_token,
        verify_ssl=client.httpx_verify(),
        timeout=timeout,
        project=project,
        subject=subject,
        session=session,
        import_handler=import_handler,
        ignore_unparsable=ignore_unparsable,
        overwrite=overwrite,
        direct_archive=direct_archive,
    )

    # Phase 2: Split into batches
    batch_count = max(1, min(upload_workers, len(files)))
    batches = split_into_n_batches(files, batch_count)
    report(
        OperationPhase.PREPARING,
        message=f"Split {len(files)} files into {len(batches)} batches",
    )

    # Phase 3+4: Create archives and upload (merged to reduce peak memory)
    #
    # Each worker creates its archive, uploads it, then deletes it
    # immediately. This avoids having all archives on disk at once,
    # which would double the disk/page-cache footprint.
    ext = ".tar" if archive_format == "tar" else ".zip"
    temp_dir = Path(tempfile.mkdtemp(prefix="xnatctl_upload_"))

    try:
        archive_paths = [temp_dir / f"batch_{i + 1}{ext}" for i in range(len(batches))]

        source_path = source_dir.expanduser().resolve()
        effective_workers = max(1, min(upload_workers, len(batches)))

        report(
            OperationPhase.UPLOADING,
            total=len(batches),
            message="Starting batch processing...",
        )

        batch_refresher = _build_batch_refresher(client, config)

        results, total_archive_size = _run_upload_batches(
            batches,
            archive_paths,
            source_path,
            archive_format,
            config,
            batch_refresher,
            effective_workers,
            report,
            errors,
        )

        # Phase 5: Complete
        return _build_final_summary(
            results, batches, files, errors, total_archive_size, total_start, session, report
        )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _collect_files_or_summary(source_dir: Path, total_start: float) -> list[Path] | UploadSummary:
    """Collect the DICOM files, or the failure summary when none can be had.

    Args:
        source_dir: Directory containing DICOM files.
        total_start: The run's start timestamp, for the failure duration.

    Returns:
        The files to upload, or a failed UploadSummary the caller returns
        as-is (unscannable directory, or nothing found).
    """
    try:
        files = collect_dicom_files(source_dir)
    except (ValueError, OSError) as e:
        return UploadSummary(
            success=False,
            total=0,
            succeeded=0,
            failed=0,
            duration=time.time() - total_start,
            errors=[f"Failed to scan directory: {e}"],
        )

    if not files:
        return UploadSummary(
            success=False,
            total=0,
            succeeded=0,
            failed=0,
            duration=time.time() - total_start,
            errors=["No DICOM files found"],
        )
    return files


def _build_batch_refresher(
    client: XNATClient, config: _BatchUploadConfig
) -> SessionRefresher | None:
    """Build the run's shared session refresher, when a token is in play.

    One refresher for the run, so a session evicted mid-upload is
    re-established once rather than once per worker. Only built when
    a token is in play: workers given bare credentials already log in
    per batch and have nothing to refresh.

    Args:
        client: Bound XNAT client that owns the session.
        config: The run's static batch configuration.

    Returns:
        The shared refresher, or None when no token is in play.
    """
    if not config.session_token:
        return None
    return SessionRefresher(
        base_url=config.client.base_url,
        verify_ssl=config.verify_ssl,
        token=config.session_token,
        username=config.username,
        password=config.password,
        owner=client,
    )


def _run_upload_batches(
    batches: list[list[Path]],
    archive_paths: list[Path],
    source_path: Path,
    archive_format: str,
    config: _BatchUploadConfig,
    batch_refresher: SessionRefresher | None,
    effective_workers: int,
    report: Callable[..., None],
    errors: list[str],
) -> tuple[list[_UploadResult], int]:
    """Run every batch through the worker pool, reporting as each completes.

    Progress reporting and error collection happen inside the completion
    loop, at the same point in the timeline as each batch finishes -- not
    recomputed afterward.

    Args:
        batches: Per-worker file batches.
        archive_paths: One archive path per batch, same order.
        source_path: Resolved source directory (archive member roots).
        archive_format: Archive format, "tar" or "zip".
        config: The run's static batch configuration.
        batch_refresher: Shared session refresher, or None.
        effective_workers: Worker-pool size.
        report: Progress-event emitter.
        errors: Mutable run-wide error list, appended per failed batch.

    Returns:
        Tuple of (per-batch results in completion order, total archived bytes).
    """
    results: list[_UploadResult] = []
    total_archive_size = 0

    with cancellable_pool(effective_workers) as (executor, cancel_token):
        futures: dict[Future[_UploadResult], int] = {}
        for i, batch in enumerate(batches):
            fut: Future[_UploadResult] = executor.submit(
                _create_and_upload_batch,
                batch=batch,
                archive_path=archive_paths[i],
                source_path=source_path,
                archive_format=archive_format,
                batch_id=i + 1,
                config=config,
                cancel_token=cancel_token,
                session_refresher=batch_refresher,
            )
            futures[fut] = i + 1

        for done in as_completed(futures):
            result: _UploadResult = done.result()
            results.append(result)
            total_archive_size += result.archive_size

            if not result.success:
                errors.append(f"Batch {result.batch_id}: {result.error}")

            succeeded = sum(1 for r in results if r.success)
            report(
                OperationPhase.UPLOADING,
                current=len(results),
                total=len(batches),
                batch_id=result.batch_id,
                success=result.success,
                message=f"Completed {len(results)}/{len(batches)} ({succeeded} succeeded)",
            )

    return results, total_archive_size


def _build_final_summary(
    results: list[_UploadResult],
    batches: list[list[Path]],
    files: list[Path],
    errors: list[str],
    total_archive_size: int,
    total_start: float,
    session: str,
    report: Callable[..., None],
) -> UploadSummary:
    """Tally the batch results into the run's UploadSummary and final report.

    Args:
        results: Per-batch results.
        batches: The batches that ran.
        files: All collected files.
        errors: Run-wide error list.
        total_archive_size: Total archived bytes.
        total_start: The run's start timestamp.
        session: Target session label.
        report: Progress-event emitter.

    Returns:
        The run's UploadSummary.
    """
    total_duration = time.time() - total_start
    batches_succeeded = sum(1 for r in results if r.success)
    batches_failed = len(results) - batches_succeeded
    success = batches_failed == 0

    report(
        OperationPhase.COMPLETE if success else OperationPhase.ERROR,
        current=len(results),
        total=len(batches),
        message=(
            "Upload complete!" if success else f"Upload completed with {batches_failed} failures"
        ),
        success=success,
        errors=errors,
    )

    if not success:
        logger.warning("Upload completed with %s failures", batches_failed)

    return UploadSummary(
        success=success,
        total=len(batches),
        succeeded=batches_succeeded,
        failed=batches_failed,
        duration=total_duration,
        errors=errors,
        total_files=len(files),
        total_size_mb=total_archive_size / 1024 / 1024,
        batches_total=len(batches),
        batches_succeeded=batches_succeeded,
        batches_failed=batches_failed,
        session_id=session,
    )
