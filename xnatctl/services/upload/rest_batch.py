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
from xnatctl.core.server_version import MIN_VERSION_DIRECT_ARCHIVE, require_server_version
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


@dataclass
class _AuthAttempt:
    """Outcome of resolving batch-upload credentials to a JSESSION cookie.

    Exactly one of ``failure`` / ``session_token`` is set: a failure short-
    circuits the upload, otherwise the token and cookie jar are ready to use.
    """

    failure: ArchiveUploadResult | None = None
    session_token: str | None = None
    cookies: dict[str, str] | None = None
    created_session: bool = False


def _resolve_batch_credentials(
    client: httpx.Client,
    username: str | None,
    password: str | None,
    session_token: str | None,
) -> _AuthAttempt:
    """Turn a token or username/password into a JSESSION cookie jar.

    Args:
        client: The batch's own httpx client.
        username: XNAT username, used only when no token is given.
        password: Its password.
        session_token: Existing JSESSION token, preferred when present.

    Returns:
        _AuthAttempt; ``created_session`` is True only when this call logged
        in itself, in which case the caller owns the session's deletion.
    """
    if session_token:
        return _AuthAttempt(session_token=session_token, cookies={"JSESSIONID": session_token})

    if not username or not password:
        return _AuthAttempt(
            failure=ArchiveUploadResult(False, "Authentication failed: missing credentials")
        )

    auth_resp = client.post(
        "/data/JSESSION",
        auth=(str(username), str(password)),
    )
    if auth_resp.status_code != 200:
        return _AuthAttempt(
            failure=ArchiveUploadResult(
                False,
                f"Authentication failed: HTTP {auth_resp.status_code}",
                status_code=auth_resp.status_code,
            )
        )

    if "<html" in auth_resp.text.lower():
        return _AuthAttempt(
            failure=ArchiveUploadResult(False, "Authentication failed: invalid credentials")
        )

    token = auth_resp.text.strip()
    return _AuthAttempt(session_token=token, cookies={"JSESSIONID": token}, created_session=True)


def _upload_with_reauth(
    attempt_with: Callable[[dict[str, str]], httpx.Response],
    *,
    archive_name: str,
    session_token: str | None,
    cookies: dict[str, str],
    session_refresher: SessionRefresher | None,
    cancel_token: CancellationToken,
) -> httpx.Response:
    """Run the upload retry ladder, retrying once with a refreshed token on 401.

    Mutates *cookies* in place when the token is refreshed, so the caller's
    JSESSION cleanup always targets the newest token -- even when the retried
    upload raises.

    Args:
        attempt_with: Uploads the archive using the given cookie jar.
        archive_name: Archive filename, for log/retry labels.
        session_token: Token the first attempt runs with.
        cookies: Cookie jar shared with the caller's cleanup path.
        session_refresher: Thread-safe token manager; None disables reauth.
        cancel_token: Checked by the retry ladder.

    Returns:
        The final response (possibly still 401 when reauth is unavailable).
    """
    resp = upload_with_retry(
        lambda: attempt_with(cookies),
        label=f"batch {archive_name}",
        cancel_token=cancel_token,
    )

    if resp.status_code == 401 and session_refresher is not None:
        fresh = session_refresher.refresh(session_token)
        if fresh and fresh != session_token:
            logger.info(
                "Session expired mid-upload; retrying batch %s with a refreshed token",
                archive_name,
            )
            cookies["JSESSIONID"] = fresh
            resp = upload_with_retry(
                lambda: attempt_with(cookies),
                label=f"batch {archive_name} (after reauth)",
                cancel_token=cancel_token,
            )
    return resp


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
    xnat_client: XNATClient,
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
    ``xnat_client`` is used only for the version-gate check below (its
    cached ``server_version``, safe under concurrent access) -- the actual
    upload always goes over a fresh per-call ``httpx.Client``, never
    ``xnat_client``'s own.

    That upload client's base URL is taken from ``xnat_client``, not passed
    in separately. A separate ``base_url`` argument would let the gate and
    the upload address different servers: a caller could hand in a 1.9
    client and a 1.7 target URL, satisfy the version check, and still send
    ``Direct-Archive=true`` to a server that does not support it. Deriving
    the URL from the same object the gate inspects makes that divergence
    unrepresentable rather than merely unlikely.

    This is one of the two canonical public entry points that can set
    ``Direct-Archive=true`` on the wire (the other is
    :class:`~xnatctl.services.upload.gradual.GradualUploadRun`); both gate
    here rather than relying solely on a higher wrapper, because both are
    re-exported for direct library use and a caller going straight to either
    must not be able to send a direct-archive import to an unsupported
    server ungated. ``upload_archive_or_raise`` and the parallel batch path
    also gate before this point, purely to fail fast before doing any local
    archive work -- by the time either reaches here, the version is already
    cached, so the check below costs no extra network round trip.

    A 401 mid-upload must not end the batch when credentials are available:
    XNAT evicts sessions when an account exceeds its concurrent-session
    limit -- routine when several workers share a service account -- so a
    long parallel upload would otherwise fail batch by batch against a
    server that was working perfectly. Refreshing through the shared
    *session_refresher* rather than logging in per batch matters: it
    serialises the reauth, so N workers hitting the same eviction do not
    answer it with N more logins.

    Returns:
        ArchiveUploadResult; on failure it carries the final HTTP status or the
        transport exception so callers can classify the error.

    Raises:
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
    """
    if direct_archive:
        require_server_version(xnat_client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

    # Same object the gate just inspected -- see the docstring.
    base_url = xnat_client.base_url

    name = archive_path.name.lower()
    # Anything that is not a ZIP defaults to tar: the CLI accepts arbitrary
    # archive names (data.tar.bz2, extensionless), and tar is the default
    # for those. The batch path only ever generates .tar/.zip names.
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
            auth = _resolve_batch_credentials(client, username, password, session_token)
            if auth.failure is not None:
                return auth.failure
            session_token = auth.session_token
            cookies = auth.cookies or {}
            created_session = auth.created_session

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
                resp = _upload_with_reauth(
                    _attempt_with,
                    archive_name=archive_path.name,
                    session_token=session_token,
                    cookies=cookies,
                    session_refresher=session_refresher,
                    cancel_token=cancel_token,
                )
            finally:
                if created_session:
                    try:
                        client.delete("/data/JSESSION", cookies=cookies)
                    except Exception:  # noqa: BLE001  # best-effort cleanup: deleting a created JSESSION must not fail the upload
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
        except Exception as e:  # noqa: BLE001  # worker-pool isolation: batch result returned for tallying, not raised (see module docstring)
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

    Raises:
        UnsupportedServerVersionError: If ``direct_archive`` is set and the
            server is known to be older than
            :data:`~xnatctl.core.server_version.MIN_VERSION_DIRECT_ARCHIVE`.
    """
    if direct_archive:
        require_server_version(client, MIN_VERSION_DIRECT_ARCHIVE, "direct-archive")

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
            xnat_client=client,
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

    _raise_for_archive_result(
        result,
        base_url=client.base_url,
        archive_path=archive_path,
        project=project,
        subject=subject,
        session=session,
    )


def _raise_for_archive_result(
    result: ArchiveUploadResult,
    *,
    base_url: str,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
) -> None:
    """Map a failed ArchiveUploadResult to the typed exception taxonomy.

    Args:
        result: Outcome of one archive upload.
        base_url: XNAT server base URL, for error context.
        archive_path: Uploaded archive, for error context.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.

    Raises:
        SessionExpiredError: On a final 401.
        PermissionDeniedError: On a final 403.
        ResourceNotFoundError: On a final 404.
        RetryExhaustedError: On an exhausted retryable status.
        RequestTimeoutError: On an httpx timeout.
        NetworkError: On an httpx transport failure.
        UploadError: On any other failure.
    """
    if result.success:
        return
    if result.status_code == 401:
        raise SessionExpiredError(base_url)
    if result.status_code == 403:
        raise PermissionDeniedError(f"project {project}", "upload to", url=base_url)
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
            base_url,
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            message=f"Upload of {archive_path.name} failed: {result.error}",
        )
    if isinstance(result.exception, httpx.TransportError):
        raise NetworkError(base_url, cause=result.error)
    raise UploadError(
        f"Upload of {archive_path.name} failed: {result.error}",
        file_path=str(archive_path),
    )


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
