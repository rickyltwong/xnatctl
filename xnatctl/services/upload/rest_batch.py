"""Parallel REST batch upload transport.

Splits a DICOM directory into N batches, archives each batch (tar/zip), and
uploads the archives in parallel through the XNAT import service. Also home
to the single-archive upload path shared with ``session upload``.
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
from typing import Any, NamedTuple

import httpx

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken, cancellable_pool
from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    NetworkError,
    OperationCancelledError,
    PermissionDeniedError,
    ResourceNotFoundError,
    RetryExhaustedError,
    SessionExpiredError,
    UploadError,
)
from xnatctl.core.exceptions import RequestTimeoutError as XNATTimeoutError
from xnatctl.core.retry import RETRYABLE_STATUS_CODES, UPLOAD_MAX_RETRIES, upload_with_retry
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS, build_httpx_timeout
from xnatctl.models.progress import OperationPhase, UploadProgress, UploadSummary
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params

from .archives import _create_archive, _maybe_zip_to_tar
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


# =============================================================================
# Archive upload (shared by the parallel batch path and `session upload`)
# =============================================================================


class ArchiveUploadResult(NamedTuple):
    """Outcome of one archive upload, with enough context to classify it.

    ``status_code`` and ``exception`` exist so the CLI can map a failure to the
    documented exit-code taxonomy (auth 3, network 4, permission 6) instead of
    flattening everything to a string and exiting 1.
    """

    success: bool
    error: str
    status_code: int | None = None
    exception: BaseException | None = None


def _classify_import_response(resp: httpx.Response) -> ArchiveUploadResult:
    """Turn the import service's final response into an ArchiveUploadResult."""
    if resp.status_code == 200:
        return ArchiveUploadResult(True, "")
    if resp.status_code in (401, 403):
        # No body snippet here on purpose: XNAT answers both with an HTML
        # login page, which is noise in a one-line error.
        return ArchiveUploadResult(
            False,
            "Authentication failed: invalid or expired session",
            status_code=resp.status_code,
        )
    return ArchiveUploadResult(
        False,
        f"HTTP {resp.status_code}: {resp.text[:500]}",
        status_code=resp.status_code,
    )


def upload_single_archive(
    *,
    base_url: str,
    username: str | None,
    password: str | None,
    session_token: str | None,
    verify_ssl: bool | ssl.SSLContext,
    timeout: int,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
    cancel_token: CancellationToken = NULL_TOKEN,
    session_refresher: SessionRefresher | None = None,
) -> ArchiveUploadResult:
    """Upload a single archive file to XNAT.

    Creates a fresh httpx client for thread-safety in parallel execution.

    A 401 mid-upload used to end the batch. That is the wrong answer whenever
    credentials are available: XNAT evicts sessions when an account exceeds its
    concurrent-session limit -- routine when several workers share a service
    account -- so a long parallel upload would fail batch by batch against a
    server that was working perfectly. The gradual path already reauthenticated;
    this one did not. Refreshing through the shared *session_refresher* rather
    than logging in per batch matters: it serialises the reauth, so N workers
    hitting the same eviction do not answer it with N more logins.

    Returns:
        ArchiveUploadResult; on failure it carries the final HTTP status or the
        transport exception so callers can classify the error.
    """
    name = archive_path.name.lower()
    # Anything that is not a ZIP defaults to tar: the CLI accepts arbitrary
    # archive names (data.tar.bz2, extensionless), and tar was the historical
    # default for those. The batch path only ever generates .tar/.zip names.
    content_type = "application/zip" if name.endswith(".zip") else "application/x-tar"

    params = build_import_params(
        import_handler=import_handler,
        project=project,
        subject=subject,
        session=session,
        overwrite=overwrite,
        overwrite_files=True,
        quarantine=False,
        trigger_pipelines=True,
        rename=False,
        inbody=True,
        ignore_unparsable=ignore_unparsable,
        direct_archive=direct_archive,
    )

    with httpx.Client(
        base_url=base_url,
        timeout=build_httpx_timeout(timeout),  # connect fails fast
        verify=verify_ssl,
    ) as client:
        try:
            cookies: dict[str, str] = {}
            created_session = False

            if session_token:
                cookies = {"JSESSIONID": session_token}
            else:
                if not username or not password:
                    return ArchiveUploadResult(False, "Authentication failed: missing credentials")

                auth_resp = client.post(
                    "/data/JSESSION",
                    auth=(str(username), str(password)),
                )
                if auth_resp.status_code != 200:
                    return ArchiveUploadResult(
                        False,
                        f"Authentication failed: HTTP {auth_resp.status_code}",
                        status_code=auth_resp.status_code,
                    )

                if "<html" in auth_resp.text.lower():
                    return ArchiveUploadResult(False, "Authentication failed: invalid credentials")

                session_token = auth_resp.text.strip()
                cookies = {"JSESSIONID": session_token}
                created_session = True

            def _attempt_with(jar: dict[str, str]) -> httpx.Response:
                with archive_path.open("rb") as data:
                    return client.post(
                        IMPORT_ENDPOINT,
                        params=params,
                        headers={"Content-Type": content_type},
                        content=data,
                        cookies=jar,
                    )

            try:
                resp = upload_with_retry(
                    lambda: _attempt_with(cookies),
                    label=f"batch {archive_path.name}",
                    cancel_token=cancel_token,
                )

                if resp.status_code == 401 and session_refresher is not None:
                    fresh = session_refresher.refresh(session_token)
                    if fresh and fresh != session_token:
                        logger.info(
                            "Session expired mid-upload; retrying batch %s with a refreshed token",
                            archive_path.name,
                        )
                        session_token = fresh
                        cookies = {"JSESSIONID": fresh}
                        resp = upload_with_retry(
                            lambda: _attempt_with(cookies),
                            label=f"batch {archive_path.name} (after reauth)",
                            cancel_token=cancel_token,
                        )
            finally:
                if created_session:
                    try:
                        client.delete("/data/JSESSION", cookies=cookies)
                    except Exception:
                        pass

            return _classify_import_response(resp)

        except httpx.ConnectTimeout as e:
            # upload_with_retry re-raises this without retrying (fail fast on
            # an unreachable host), so do not claim retries happened.
            return ArchiveUploadResult(False, "Connection timed out; not retried", exception=e)
        except httpx.TimeoutException as e:
            return ArchiveUploadResult(False, "Upload timed out (after retries)", exception=e)
        except httpx.ConnectError as e:
            return ArchiveUploadResult(
                False, f"Connection failed (after retries): {e}", exception=e
            )
        except Exception as e:
            return ArchiveUploadResult(False, str(e), exception=e)


def upload_archive_or_raise(
    client: XNATClient,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
    zip_to_tar: bool,
) -> None:
    """Upload one archive through :func:`upload_single_archive`, raising on failure.

    A thin classifier over :func:`upload_single_archive`: that function returns an
    :class:`ArchiveUploadResult` so the parallel batch path can tally failures,
    whereas the single-archive CLI path wants the failure mapped to a typed
    exception so ``@handle_errors`` keeps the documented exit-code taxonomy
    (auth 3, network 4, permission 6). The two are kept separate deliberately --
    one returns a result to be counted, one raises to stop a command.

    Deliberately not ``client.post`` wrapped in ``upload_with_retry``: that
    stacked two retry ladders, and because ``_request`` raises typed errors on
    4xx, ``upload_with_retry`` never saw a raw 400 response -- so the
    transient-vs-permanent 400 discrimination the import service needs was dead
    on this path.
    """
    refresher = SessionRefresher(
        base_url=client.base_url,
        verify_ssl=client.httpx_verify(),
        token=client.session_token,
        username=client.username,
        password=client.password,
        owner=client,
    )

    with _maybe_zip_to_tar(archive_path, zip_to_tar) as upload_path:
        result = upload_single_archive(
            base_url=client.base_url,
            username=client.username,
            password=client.password,
            session_token=client.session_token,
            verify_ssl=client.httpx_verify(),
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            archive_path=upload_path,
            project=project,
            subject=subject,
            session=session,
            import_handler="DICOM-zip",
            ignore_unparsable=ignore_unparsable,
            overwrite=overwrite,
            direct_archive=direct_archive,
            session_refresher=refresher,
        )

    if result.success:
        return
    if result.status_code == 401:
        raise SessionExpiredError(client.base_url)
    if result.status_code == 403:
        raise PermissionDeniedError(f"project {project}", "upload to", url=client.base_url)
    if result.status_code == 404:
        raise ResourceNotFoundError("Import destination", f"{project}/{subject}/{session}")
    if result.status_code in RETRYABLE_STATUS_CODES:
        # The core set (429/5xx), NOT the upload set: an exhausted transient
        # 400 falls through to UploadError below, where the body -- which names
        # the conflicting session -- survives in the message.
        raise RetryExhaustedError(
            f"upload {archive_path.name}",
            UPLOAD_MAX_RETRIES + 1,
            UploadError(result.error),
        )
    if isinstance(result.exception, httpx.TimeoutException):
        raise XNATTimeoutError(
            client.base_url,
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            message=f"Upload of {archive_path.name} failed: {result.error}",
        )
    if isinstance(result.exception, httpx.TransportError):
        raise NetworkError(client.base_url, cause=result.error)
    raise UploadError(
        f"Upload of {archive_path.name} failed: {result.error}",
        file_path=str(archive_path),
    )


def _create_and_upload_batch(
    *,
    batch: list[Path],
    archive_path: Path,
    source_path: Path,
    archive_format: str,
    base_url: str,
    username: str | None,
    password: str | None,
    session_token: str | None,
    verify_ssl: bool | ssl.SSLContext,
    timeout: int,
    batch_id: int,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
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
        return _UploadResult(
            batch_id=batch_id,
            success=False,
            duration=0.0,
            file_count=len(batch),
            archive_size=0,
            error="cancelled",
            cancelled=True,
        )

    try:
        archive_size = _create_archive(batch, archive_path, source_path, archive_format)

        upload_result = upload_single_archive(
            base_url=base_url,
            username=username,
            password=password,
            session_token=session_token,
            verify_ssl=verify_ssl,
            timeout=timeout,
            archive_path=archive_path,
            project=project,
            subject=subject,
            session=session,
            import_handler=import_handler,
            ignore_unparsable=ignore_unparsable,
            overwrite=overwrite,
            direct_archive=direct_archive,
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
        return _UploadResult(
            batch_id=batch_id,
            success=False,
            duration=time.time() - start_time,
            file_count=len(batch),
            archive_size=archive_size,
            error="cancelled",
            cancelled=True,
        )
    except Exception as e:
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
        except Exception:
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
    """
    total_start = time.time()
    errors: list[str] = []

    base_url = client.base_url
    session_token = client.session_token
    verify_ssl = client.httpx_verify()
    effective_username = username or client.username
    effective_password = password or client.password

    def report(phase: OperationPhase, **kwargs: Any) -> None:
        if progress_callback:
            progress_callback(UploadProgress(phase=phase, **kwargs))

    # Phase 1: Collect files
    report(OperationPhase.PREPARING, message="Scanning for DICOM files...")

    try:
        files = collect_dicom_files(source_dir)
    except Exception as e:
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
    # which previously doubled the disk/page-cache footprint.
    ext = ".tar" if archive_format == "tar" else ".zip"
    temp_dir = Path(tempfile.mkdtemp(prefix="xnatctl_upload_"))
    archive_paths: list[Path] = []
    total_archive_size = 0

    try:
        for i in range(len(batches)):
            archive_paths.append(temp_dir / f"batch_{i + 1}{ext}")

        source_path = source_dir.expanduser().resolve()
        effective_workers = max(1, min(upload_workers, len(batches)))

        report(
            OperationPhase.UPLOADING,
            total=len(batches),
            message="Starting batch processing...",
        )

        results: list[_UploadResult] = []

        # One refresher for the run, so a session evicted mid-upload is
        # re-established once rather than once per worker. Only built when
        # a token is in play: workers given bare credentials already log in
        # per batch and have nothing to refresh.
        batch_refresher = (
            SessionRefresher(
                base_url=base_url,
                verify_ssl=verify_ssl,
                token=session_token,
                username=effective_username,
                password=effective_password,
                owner=client,
            )
            if session_token
            else None
        )

        with cancellable_pool(effective_workers) as (executor, cancel_token):
            futures: dict[Future[_UploadResult], int] = {}
            for i, batch in enumerate(batches):
                fut: Future[_UploadResult] = executor.submit(
                    _create_and_upload_batch,
                    batch=batch,
                    archive_path=archive_paths[i],
                    source_path=source_path,
                    archive_format=archive_format,
                    base_url=base_url,
                    username=effective_username,
                    password=effective_password,
                    session_token=session_token,
                    verify_ssl=verify_ssl,
                    timeout=timeout,
                    batch_id=i + 1,
                    project=project,
                    subject=subject,
                    session=session,
                    import_handler=import_handler,
                    ignore_unparsable=ignore_unparsable,
                    overwrite=overwrite,
                    direct_archive=direct_archive,
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

        # Phase 5: Complete
        total_duration = time.time() - total_start
        batches_succeeded = sum(1 for r in results if r.success)
        batches_failed = len(results) - batches_succeeded
        success = batches_failed == 0

        report(
            OperationPhase.COMPLETE if success else OperationPhase.ERROR,
            current=len(results),
            total=len(batches),
            message=(
                "Upload complete!"
                if success
                else f"Upload completed with {batches_failed} failures"
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

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
