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
from xnatctl.core.logging import FILE_ONLY_ATTR
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
    batch_id: int
    success: bool
    duration: float
    file_count: int
    archive_size: int
    error: str = ""
    cancelled: bool = False


@dataclass(frozen=True)
class _BatchUploadConfig:
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
    except Exception as e:
        logger.exception("Upload of batch %d failed", batch_id, extra={FILE_ONLY_ATTR: True})
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
        except OSError:
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

    report(OperationPhase.PREPARING, message="Scanning for DICOM files...")

    files_or_summary = _collect_files_or_summary(source_dir, total_start)
    if isinstance(files_or_summary, UploadSummary):
        return files_or_summary
    files = files_or_summary

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

    batch_count = max(1, min(upload_workers, len(files)))
    batches = split_into_n_batches(files, batch_count)
    report(
        OperationPhase.PREPARING,
        message=f"Split {len(files)} files into {len(batches)} batches",
    )

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

        return _build_final_summary(
            results, batches, files, errors, total_archive_size, total_start, session, report
        )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _collect_files_or_summary(source_dir: Path, total_start: float) -> list[Path] | UploadSummary:
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
