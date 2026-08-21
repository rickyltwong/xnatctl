"""Upload service for XNAT upload operations.

Provides UploadService with methods for all upload transports:
- REST batch upload (simple ZIP batches via import service)
- Parallel REST upload (batched archives with parallel workers)
- DICOM C-STORE upload (pynetdicom-based network transfer)
- Resource upload (file/directory upload to session resources)

Public utility functions (collect_dicom_files, split_into_batches, etc.)
are available for direct import and testing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import ssl
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, as_completed, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from zipfile import ZIP_DEFLATED, ZipFile

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
    XNATCtlError,
)
from xnatctl.core.exceptions import RequestTimeoutError as XNATTimeoutError
from xnatctl.core.retry import RETRYABLE_STATUS_CODES, UPLOAD_MAX_RETRIES, upload_with_retry
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS, build_httpx_timeout
from xnatctl.models.progress import (
    OperationPhase,
    UploadProgress,
    UploadSummary,
)
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params

from .base import BaseService

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

DEFAULT_BATCH_SIZE = 500
DEFAULT_UPLOAD_WORKERS = 4
DEFAULT_ARCHIVE_WORKERS = 4
DEFAULT_ARCHIVE_FORMAT = "tar"
DEFAULT_TIMEOUT = DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_IMPORT_HANDLER = "DICOM-zip"
DEFAULT_OVERWRITE = "delete"
DEFAULT_DICOM_STORE_WORKERS = 4
DEFAULT_DICOM_CALLING_AET = "XNATCTL"
DEFAULT_DICOM_PORT = 104

DICOM_EXTENSIONS = {".dcm", ".ima", ".img", ".dicom"}

# Gradual-DICOM uses one HTTP request per DICOM file; creating a new httpx.Client
# per file is expensive and can trigger transient ConnectError bursts under high
# concurrency. Reuse a persistent client per worker thread (keep-alive).
_GRADUAL_HTTP_TIMEOUT_SECONDS = 120.0
_gradual_client_local = threading.local()
_gradual_client_registry_lock = threading.Lock()
_gradual_client_registry: list[httpx.Client] = []
_gradual_scope_lock = threading.Lock()
_gradual_scope_refcount = 0


class SessionRefresher:
    """Thread-safe XNAT session token manager.

    When any worker thread encounters a 401 (expired session), it calls
    :meth:`refresh`.  Only the first thread to detect a stale token actually
    re-authenticates; concurrent callers wait on the lock and receive the
    already-refreshed token.

    A refresh here has to reach the *owning client* too. Workers hold their own
    copy of the token, so without ``owner`` the shared client kept the token it
    started with: after a multi-hour upload during which workers re-authenticated
    several times, the phases that run afterwards -- hierarchy resolution and
    resource attach in ``session upload-exam`` -- went out with a token that had
    been dead for hours, turning a completed upload into a failure at the last
    step.

    Args:
        base_url: XNAT server URL.
        verify_ssl: TLS verification: a bool, or an SSLContext carrying a
            profile's ``ca_bundle`` (``XNATClient.httpx_verify()``).
        token: Initial JSESSIONID token.
        username: Credentials for re-authentication.
        password: Credentials for re-authentication.
        owner: Client whose ``session_token`` should track refreshes.
    """

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool | ssl.SSLContext,
        token: str | None,
        username: str | None,
        password: str | None,
        owner: XNATClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._verify_ssl = verify_ssl
        self._token = token
        self._username = username
        self._password = password
        self._owner = owner
        self._lock = threading.Lock()

    def _publish(self, token: str) -> None:
        """Push a fresh token to the owning client and the on-disk cache.

        Called with the lock held. Both writes are best-effort: a worker that
        just re-authenticated has a usable token in hand, and failing the
        upload because a *cache* could not be updated would be absurd.
        """
        if self._owner is not None:
            self._owner.session_token = token

        try:
            # Imported here rather than at module scope: this is the only place
            # the upload service touches the credential cache, and a top-level
            # import would tie every upload to the auth module's import cost.
            from xnatctl.core.auth import AuthManager

            if self._username:
                AuthManager().save_session(token, self._base_url, self._username)
        except Exception as e:
            logger.debug("Could not update the session cache after refresh: %s", e)

    @property
    def token(self) -> str | None:
        """Current session token (may be updated by any thread)."""
        with self._lock:
            return self._token

    def refresh(self, stale_token: str | None) -> str | None:
        """Re-authenticate and return a fresh token.

        Thread-safe: if another thread already refreshed past *stale_token*,
        the cached fresh token is returned without hitting the server again.

        Args:
            stale_token: The token that triggered the 401.

        Returns:
            Fresh session token, or the unchanged token if credentials are
            unavailable.
        """
        with self._lock:
            if self._token != stale_token:
                return self._token

            if not self._username or not self._password:
                logger.warning("Session expired but no credentials available for reauth")
                return self._token

            try:
                with httpx.Client(
                    base_url=self._base_url,
                    verify=self._verify_ssl,
                    timeout=build_httpx_timeout(30.0),  # connect fails fast
                ) as client:
                    resp = client.post(
                        "/data/JSESSION",
                        auth=(self._username, self._password),
                    )
                    if resp.status_code == 200 and "<html" not in resp.text.lower():
                        self._token = resp.text.strip()
                        self._publish(self._token)
                        logger.info("Session refreshed successfully")
                    else:
                        logger.error("Session refresh failed: HTTP %d", resp.status_code)
            except Exception:
                logger.exception("Session refresh failed")

            return self._token


def _get_gradual_http_client(*, base_url: str, verify_ssl: bool | ssl.SSLContext) -> httpx.Client:
    """Get a thread-local httpx.Client for gradual-DICOM uploads."""
    key = (base_url, verify_ssl)
    client: httpx.Client | None = getattr(_gradual_client_local, "client", None)
    client_key: tuple[str, bool | ssl.SSLContext] | None = getattr(
        _gradual_client_local, "key", None
    )

    if client is None or client_key != key or client.is_closed:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        client = httpx.Client(
            base_url=base_url,
            timeout=build_httpx_timeout(_GRADUAL_HTTP_TIMEOUT_SECONDS),  # connect fails fast
            verify=verify_ssl,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        _gradual_client_local.client = client
        _gradual_client_local.key = key
        with _gradual_client_registry_lock:
            _gradual_client_registry.append(client)

    return client


def _close_gradual_http_clients() -> None:
    """Close any thread-local clients created for gradual uploads."""
    # Best-effort clear for the current thread so sequential operations don't
    # accidentally reuse a closed client.
    try:
        _gradual_client_local.client = None
        _gradual_client_local.key = None
    except Exception:
        pass

    with _gradual_client_registry_lock:
        clients = list(_gradual_client_registry)
        _gradual_client_registry.clear()
    for c in clients:
        try:
            c.close()
        except Exception:
            pass


@contextlib.contextmanager
def _gradual_http_clients_scope() -> Iterator[None]:
    """Scope gradual httpx client lifecycle to an upload operation.

    Gradual uploads use a per-thread httpx.Client. Since the registry is global,
    concurrent gradual upload operations must not close each other's clients.

    This context manager refcounts active gradual operations and only performs a
    global close when the last active operation completes.
    """
    global _gradual_scope_refcount
    with _gradual_scope_lock:
        _gradual_scope_refcount += 1

    try:
        yield
    finally:
        with _gradual_scope_lock:
            _gradual_scope_refcount -= 1
            if _gradual_scope_refcount <= 0:
                _gradual_scope_refcount = 0
                _close_gradual_http_clients()


# =============================================================================
# DICOM C-STORE Result (separate from REST models)
# =============================================================================


@dataclass
class DICOMStoreSummary:
    """Summary of a DICOM C-STORE operation."""

    total_files: int
    sent: int
    failed: int
    log_dir: Path
    workspace: Path
    success: bool


# =============================================================================
# Internal Batch Result
# =============================================================================


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
# Public Utility Functions
# =============================================================================


def collect_dicom_files(
    root: Path,
    *,
    include_extensionless: bool = True,
) -> list[Path]:
    """Recursively collect DICOM-like files under a root directory.

    Args:
        root: Root directory to search.
        include_extensionless: If True, include files without extensions
            (common for raw DICOM from scanners).

    Returns:
        Sorted list of file paths.

    Raises:
        ValueError: If root is not a directory.
    """
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.exists():
                    continue
            except (OSError, ValueError):
                continue

        if _is_dicom_like_path(path, include_extensionless=include_extensionless):
            files.append(path)

    return sorted(files)


def _has_dicom_magic(path: Path) -> bool:
    """Return True if the file has the DICOM preamble magic bytes (DICM at offset 128)."""
    try:
        with open(path, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def _is_dicom_like_path(path: Path, *, include_extensionless: bool = True) -> bool:
    """Return True when a path looks like a DICOM file we should ingest."""
    if path.name.startswith("."):
        return False

    suffix = path.suffix.lower()
    if suffix in DICOM_EXTENSIONS:
        return True
    if include_extensionless and suffix == "":
        return _has_dicom_magic(path)
    return False


def split_into_batches(
    files: Sequence[Path],
    batch_size: int,
) -> list[list[Path]]:
    """Split files into batches of specified size.

    Args:
        files: Sequence of file paths to split.
        batch_size: Maximum files per batch.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if batch_size <= 0:
        return [list(files)]

    batches: list[list[Path]] = []
    current_batch: list[Path] = []

    for file_path in files:
        current_batch.append(file_path)
        if len(current_batch) >= batch_size:
            batches.append(current_batch)
            current_batch = []

    if current_batch:
        batches.append(current_batch)

    return batches


def split_into_n_batches(
    files: Sequence[Path],
    num_batches: int,
) -> list[list[Path]]:
    """Split files into N roughly equal batches using round-robin.

    Args:
        files: Sequence of file paths to split.
        num_batches: Number of batches to create.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if num_batches <= 0:
        return [list(files)]

    actual_batches = min(num_batches, len(files))
    batches: list[list[Path]] = [[] for _ in range(actual_batches)]

    for idx, file_path in enumerate(files):
        batches[idx % actual_batches].append(file_path)

    return batches


def _error_signature(error: str) -> str:
    """Collapse an error message to something comparable across files.

    Two rejections of the same *kind* differ in the file name and any UID the
    server echoes back, so comparing raw strings would never find them equal.
    The leading prefix is stable enough to group by and short enough not to
    reach the variable tail.
    """
    return error.strip()[:200]


# =============================================================================
# Archive Helpers (private)
# =============================================================================


def _create_tar_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a TAR archive from files, returning size in bytes."""
    with tarfile.open(output_path, "w") as tf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            tf.add(file_path, arcname=arcname)
    return output_path.stat().st_size


def _create_zip_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a ZIP archive from files, returning size in bytes."""
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED, allowZip64=True) as zf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            zf.write(file_path, arcname)
    return output_path.stat().st_size


def _create_archive(
    files: list[Path],
    output_path: Path,
    base_dir: Path,
    archive_format: str,
) -> int:
    """Create an archive from files.

    Args:
        files: List of file paths to include.
        output_path: Path for the output archive.
        base_dir: Base directory for relative paths in archive.
        archive_format: Format ("tar" or "zip").

    Returns:
        Size of created archive in bytes.

    Raises:
        ValueError: If archive format is unsupported.
    """
    if archive_format == "tar":
        return _create_tar_archive(files, output_path, base_dir)
    if archive_format == "zip":
        return _create_zip_archive(files, output_path, base_dir)
    raise ValueError(f"Unsupported archive format: {archive_format}")


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


def _safe_mtime(date_time: tuple[int, ...]) -> float:
    """Convert ZIP date_time tuple to timestamp safely.

    Args:
        date_time: 6-tuple (year, month, day, hour, minute, second)

    Returns:
        Unix timestamp, defaulting to 0 if conversion fails or date is invalid.
    """
    try:
        year = date_time[0]
        # Validate year is in reasonable range (ZIP format supports 1980-2107)
        if year < 1980 or year > 2107:
            return 0.0
        # mktime wants a full 9-tuple: the ZIP header carries 6 fields, and the
        # trailing three (weekday, yearday, DST) are filled with 0 -- 0 for DST
        # rather than -1 so the platform resolves it instead of guessing.
        y, mo, d, h, mi, sec = date_time[:6]
        return time.mktime((y, mo, d, h, mi, sec, 0, 0, 0))
    except (ValueError, OverflowError, OSError):
        # Invalid date - use epoch
        return 0.0


def _zip_to_tar(archive_path: Path, tar_path: Path) -> None:
    """Convert ZIP archive to TAR format.

    Args:
        archive_path: Source ZIP file
        tar_path: Destination TAR file

    Raises:
        zipfile.BadZipFile: If ZIP is corrupted
        OSError: If file operations fail
    """
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Validate ZIP integrity first
            bad_file = zf.testzip()
            if bad_file:
                raise zipfile.BadZipFile(f"Corrupted file in archive: {bad_file}")

            with tarfile.open(tar_path, "w") as tf:
                for info in zf.infolist():
                    name = info.filename
                    if info.is_dir():
                        tarinfo = tarfile.TarInfo(name.rstrip("/") + "/")
                        tarinfo.type = tarfile.DIRTYPE
                        tarinfo.mtime = _safe_mtime(info.date_time)
                        tarinfo.size = 0
                        tf.addfile(tarinfo)
                        continue

                    tarinfo = tarfile.TarInfo(name)
                    tarinfo.size = info.file_size
                    tarinfo.mtime = _safe_mtime(info.date_time)
                    with zf.open(info, "r") as src:
                        tf.addfile(tarinfo, fileobj=src)
    except zipfile.BadZipFile:
        raise
    except Exception as e:
        raise OSError(f"Failed to convert ZIP to TAR: {e}") from e


def _should_zip_to_tar(archive_path: Path, zip_to_tar: bool) -> bool:
    return zip_to_tar and archive_path.suffix.lower() == ".zip"


def _maybe_zip_to_tar(
    archive_path: Path, zip_to_tar: bool
) -> contextlib.AbstractContextManager[Path]:
    """Yield the path to upload, converting ZIP to TAR when asked.

    A context manager because the converted archive lives in a temporary
    directory that must outlive the conversion but not the upload. Content type
    is derived from the yielded path's name by the uploader.
    """

    @contextlib.contextmanager
    def _converter() -> Iterator[Path]:
        if _should_zip_to_tar(archive_path, zip_to_tar):
            with tempfile.TemporaryDirectory() as temp_dir:
                tar_path = Path(temp_dir) / f"{archive_path.stem}.tar"
                _zip_to_tar(archive_path, tar_path)
                yield tar_path
        else:
            yield archive_path

    return _converter()


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


# =============================================================================
# DICOM C-STORE Helpers (private, lazy imports)
# =============================================================================


def _get_verification_sop_class() -> Any:
    """Get VerificationSOPClass with compatibility for pynetdicom versions."""
    from pynetdicom import sop_class as _sop_class

    verification_uid = "1.2.840.10008.1.1"
    return getattr(
        _sop_class,
        "VerificationSOPClass",
        getattr(_sop_class, "Verification", verification_uid),
    )


def _get_storage_contexts() -> list[Any]:
    """Get storage presentation contexts with version compatibility."""
    try:
        from pynetdicom import StoragePresentationContexts

        return list(StoragePresentationContexts)
    except ImportError:
        from pynetdicom import sop_class as _sc
        from pynetdicom.presentation import build_context

        uids = [getattr(_sc, name) for name in dir(_sc) if name.endswith("Storage")]
        return [build_context(uid) for uid in uids]


def _ensure_sop_uids(ds: Any) -> None:
    """Populate missing SOP UID attributes from file-meta.

    Args:
        ds: pydicom Dataset object.
    """
    if not getattr(ds, "SOPClassUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPClassUID", None)
        if uid:
            ds.SOPClassUID = uid

    if not getattr(ds, "SOPInstanceUID", None):
        uid = getattr(ds.file_meta, "MediaStorageSOPInstanceUID", None)
        if uid:
            ds.SOPInstanceUID = uid


def _tls_kwargs(tls_context: ssl.SSLContext | None, host: str) -> dict[str, Any]:
    """Association kwargs for TLS, or nothing at all when plaintext.

    ``host`` is passed as the TLS ``server_hostname`` rather than the ``None``
    the pynetdicom examples use: with ``check_hostname`` on, omitting it means
    the certificate is never matched against the host being talked to, which
    removes most of the protection.
    """
    if tls_context is None:
        return {}
    return {"tls_args": (tls_context, host)}


def build_dicom_tls_context(
    ca_bundle: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> ssl.SSLContext:
    """Build the TLS context for a DICOM association.

    Plain C-STORE puts pixel data and the patient identifiers attached to it on
    the wire in cleartext. DICOM's own answer is TLS, which pynetdicom supports
    through ``ae.associate(..., tls_args=...)``.

    There is deliberately no way to switch verification off. An "insecure TLS"
    mode is the worst of both worlds -- it looks encrypted in the command line
    and in the logs while accepting any certificate presented, so a
    man-in-the-middle reads the PHI anyway and nobody notices. A site that
    genuinely cannot verify certificates should send plaintext knowingly and
    see the notice that goes with it.

    Args:
        ca_bundle: PEM file of CAs to trust. Falls back to the system store.
        client_cert: Client certificate, for SCPs requiring mutual TLS.
        client_key: Its private key. May be omitted if the cert file holds both.

    Returns:
        A context with certificate and hostname verification enabled.

    Raises:
        UploadError: If the certificate material cannot be loaded.
    """
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    # create_default_context already sets these; asserted rather than assigned
    # so a future edit that weakens them fails loudly here.
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    if client_cert:
        try:
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
        except (OSError, ssl.SSLError) as e:
            raise UploadError(
                f"Could not load the DICOM TLS client certificate: {e}",
                client_cert,
                {"client_key": client_key},
            ) from e

    return context


def _c_echo(
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    tls_context: ssl.SSLContext | None = None,
) -> bool:
    """Send a C-ECHO to verify connectivity and AE titles.

    Args:
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        tls_context: When given, the association is encrypted.

    Returns:
        True if C-ECHO succeeded.
    """
    from pynetdicom import AE

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(_get_verification_sop_class())

    assoc = ae.associate(host, port, ae_title=called_aet, **_tls_kwargs(tls_context, host))
    if not assoc.is_established:
        return False

    status = assoc.send_c_echo()
    assoc.release()

    return bool(status and status.Status == 0x0000)


def _send_dicom_batch(
    batch_id: str,
    files: list[Path],
    host: str,
    port: int,
    calling_aet: str,
    called_aet: str,
    log_dir: Path,
    tls_context: ssl.SSLContext | None = None,
) -> tuple[int, int]:
    """Send a batch of DICOM files over a single association.

    Args:
        batch_id: Identifier for this batch (for logging).
        files: List of DICOM file paths.
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.
        log_dir: Directory for batch log files.
        tls_context: When given, the association is encrypted.

    Returns:
        Tuple of (sent_count, failed_count).
    """
    import pydicom
    from pydicom.errors import InvalidDicomError
    from pynetdicom import AE

    sent = failed = 0
    log_path = log_dir / f"{batch_id}.log"

    with log_path.open("w") as log:
        ae = AE(ae_title=calling_aet)
        ae.requested_contexts = _get_storage_contexts()
        ae.add_requested_context("1.3.12.2.1107.5.9.1")

        assoc = ae.associate(host, port, ae_title=called_aet, **_tls_kwargs(tls_context, host))
        if not assoc.is_established:
            log.write("Association rejected/aborted\n")
            return sent, len(files)

        for file_path in files:
            try:
                ds = pydicom.dcmread(file_path, force=True)
            except InvalidDicomError:
                failed += 1
                log.write(f"Skip non-DICOM {file_path}\n")
                continue

            _ensure_sop_uids(ds)

            try:
                status = assoc.send_c_store(ds)
            except Exception as e:
                failed += 1
                log.write(f"Store error {file_path}: {type(e).__name__}: {e}\n")
                continue

            if status and status.Status == 0x0000:
                sent += 1
            else:
                failed += 1
                status_hex = hex(status.Status) if status else "0x0000"
                log.write(f"Failed {file_path} status {status_hex}\n")

        assoc.release()

    return sent, failed


# =============================================================================
# Gradual-DICOM Helpers (private, thread-safe standalone functions)
# =============================================================================


def _upload_single_file_gradual(
    *,
    base_url: str,
    session_refresher: SessionRefresher,
    verify_ssl: bool | ssl.SSLContext,
    file_path: Path,
    display_path: str | None = None,
    project: str,
    subject: str,
    session: str,
    direct_archive: bool = True,
    cancel_token: CancellationToken = NULL_TOKEN,
) -> tuple[str, bool, str]:
    """Upload a single file via the gradual-DICOM import handler.

    Uses a thread-local httpx client to reuse keep-alive connections per worker thread.
    On HTTP 401, refreshes the session token via *session_refresher* and retries once.

    Args:
        base_url: XNAT server base URL.
        session_refresher: Thread-safe token manager for reauth on 401.
        verify_ssl: Whether to verify SSL certificates.
        display_path: Path shown in progress and error messages, when it
            should differ from ``file_path`` (e.g. relative to the upload root).
        file_path: Path to the DICOM file.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.
        direct_archive: Use direct archive vs prearchive (default: True).
        cancel_token: Checked by the retry ladder so an interrupted upload
            abandons its backoff instead of waiting it out.

    Returns:
        Tuple of (filename, success, error_message).
    """
    name = display_path or file_path.name

    try:
        client = _get_gradual_http_client(base_url=base_url, verify_ssl=verify_ssl)

        def _do_upload(token: str | None) -> httpx.Response:
            cookies = {"JSESSIONID": token} if token else {}

            def _attempt() -> httpx.Response:
                with open(file_path, "rb") as f:
                    return client.post(
                        IMPORT_ENDPOINT,
                        params=build_import_params(
                            import_handler="gradual-DICOM",
                            project=project,
                            subject=subject,
                            session=session,
                            entity_keys="experiment",
                            inbody=True,
                            direct_archive=direct_archive,
                        ),
                        content=f,
                        headers={"Content-Type": "application/dicom"},
                        cookies=cookies,
                    )

            return upload_with_retry(
                _attempt, label=f"gradual-DICOM {name}", cancel_token=cancel_token
            )

        token = session_refresher.token
        resp = _do_upload(token)

        if resp.status_code == 401:
            new_token = session_refresher.refresh(token)
            if new_token != token:
                resp = _do_upload(new_token)
                if resp.status_code == 401:
                    logger.warning("Still 401 after session refresh for %s", name)
            else:
                logger.debug("Session refresh returned same token for %s", name)

        if 200 <= resp.status_code < 300:
            return name, True, ""

        # Include a small snippet of server response for debugging (XNAT often returns
        # useful details for 4xx/5xx in plain text or HTML).
        snippet = ""
        try:
            snippet = resp.text.strip().replace("\n", " ")
        except Exception:
            snippet = ""
        if snippet:
            snippet = snippet[:200]

        detail = f"HTTP {resp.status_code}"
        if snippet:
            detail = f"{detail}: {snippet}"
        return name, False, detail
    except Exception as e:
        return name, False, str(e)


# =============================================================================
# Upload Service
# =============================================================================


class UploadService(BaseService):
    """Service for XNAT upload operations.

    Provides methods for all upload transports: REST batch, parallel REST,
    DICOM C-STORE, and resource uploads.
    """

    def upload_dicom_parallel(
        self,
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

        base_url = self.client.base_url
        session_token = self.client.session_token
        verify_ssl = self.client.httpx_verify()
        effective_username = username or self.client.username
        effective_password = password or self.client.password

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
                    owner=self.client,
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

    def upload_dicom_store(
        self,
        dicom_root: Path,
        host: str,
        called_aet: str,
        *,
        port: int = DEFAULT_DICOM_PORT,
        calling_aet: str = DEFAULT_DICOM_CALLING_AET,
        workers: int = DEFAULT_DICOM_STORE_WORKERS,
        cleanup: bool = True,
        tls: bool = False,
        tls_ca_bundle: str | None = None,
        tls_cert: str | None = None,
        tls_key: str | None = None,
    ) -> DICOMStoreSummary:
        """Send DICOM files to an SCP using C-STORE.

        This method:
        1. Verifies connectivity with C-ECHO
        2. Collects DICOM files from the root directory
        3. Splits files into batches for parallel associations
        4. Sends files using multiple concurrent C-STORE associations

        Args:
            dicom_root: Directory containing DICOM files.
            host: DICOM SCP host.
            called_aet: Remote AE title.
            port: DICOM SCP port (default: 104).
            calling_aet: Our AE title (default: XNATCTL).
            workers: Number of parallel associations (default: 4).
            cleanup: Remove temporary workspace on completion (default: True).
            tls: Encrypt the associations. Off by default, which matches the
                DICOM standard's own default and the many deployments that run
                C-STORE inside a trusted VLAN -- but see the notice logged when
                it is off, because the alternative is PHI in cleartext.
            tls_ca_bundle: PEM file of CAs to trust (default: system store).
            tls_cert: Client certificate, for SCPs requiring mutual TLS.
            tls_key: Its private key.

        Returns:
            DICOMStoreSummary with results.

        Raises:
            ValueError: If dicom_root is not a directory.
            RuntimeError: If C-ECHO fails or no DICOM files found.
            UploadError: If TLS material is requested but cannot be loaded.
        """
        tls_context = build_dicom_tls_context(tls_ca_bundle, tls_cert, tls_key) if tls else None
        if tls_context is None:
            # Informational, not alarming: plenty of sites run C-STORE on a
            # segregated network on purpose. But it should be a decision
            # someone made, not one they never knew they had.
            logger.info(
                "DICOM C-STORE to %s:%s is unencrypted; use --tls if the server supports it",
                host,
                port,
            )
        else:
            logger.info("DICOM C-STORE to %s:%s is TLS-encrypted", host, port)

        if not dicom_root.exists() or not dicom_root.is_dir():
            raise ValueError(f"dicom_root is not a directory: {dicom_root}")

        workspace = Path(tempfile.mkdtemp(prefix="xnatctl_dicom_store_"))
        log_dir = workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        failed_total = 0

        try:
            logger.info(
                "Pre-flight C-ECHO %s -> %s @ %s:%s",
                calling_aet,
                called_aet,
                host,
                port,
            )
            if not _c_echo(host, port, calling_aet, called_aet, tls_context):
                raise RuntimeError(
                    f"C-ECHO failed - check host/port/AET settings "
                    f"(host={host}, port={port}, called_aet={called_aet})"
                )

            files = collect_dicom_files(dicom_root)
            if not files:
                raise RuntimeError(f"No DICOM files found in {dicom_root}")

            batches = split_into_n_batches(files, workers)
            logger.info(
                "Discovered %d files, using %d parallel associations",
                len(files),
                len(batches),
            )

            sent_total = 0

            with cancellable_pool(len(batches)) as (pool, _cstore_token):
                futures = {
                    pool.submit(
                        _send_dicom_batch,
                        f"{i:03d}",
                        batch,
                        host,
                        port,
                        calling_aet,
                        called_aet,
                        log_dir,
                        tls_context,
                    ): i
                    for i, batch in enumerate(batches)
                }

                for future in as_completed(futures):
                    batch_idx = futures[future]
                    sent, failed = future.result()
                    sent_total += sent
                    failed_total += failed
                    logger.info(
                        "Batch %03d complete: %d sent, %d failed",
                        batch_idx,
                        sent,
                        failed,
                    )

            return DICOMStoreSummary(
                total_files=len(files),
                sent=sent_total,
                failed=failed_total,
                log_dir=log_dir,
                workspace=workspace,
                success=failed_total == 0,
            )

        finally:
            if cleanup and failed_total == 0:
                shutil.rmtree(workspace, ignore_errors=True)

    def upload_dicom_gradual(
        self,
        source_path: Path,
        project: str,
        subject: str,
        session: str,
        *,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        direct_archive: bool = True,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files using the gradual-DICOM handler (parallel per-file).

        Each file is uploaded individually to the XNAT import service using
        the gradual-DICOM handler, which lets XNAT parse each file on ingest.
        Files are uploaded in parallel using per-thread HTTP clients.

        Accepts directories or ZIP archives. ZIP archives are extracted to a
        temporary directory before upload. Only DICOM-like files are sent:
        known DICOM extensions plus extensionless files commonly produced by
        scanners.

        Args:
            source_path: Directory or ZIP file containing DICOM files.
            project: Target project ID.
            subject: Target subject label.
            session: Target session label.
            workers: Number of parallel upload workers (default: 4).
            direct_archive: Use direct archive vs prearchive (default: True).
            progress_callback: Optional callback for progress updates.

        Returns:
            UploadSummary with results.

        Raises:
            ValueError: If source_path is not a directory or ZIP file.
            FileNotFoundError: If source_path does not exist.
        """
        with _gradual_http_clients_scope():
            start_time = time.time()
            source_path = Path(source_path)

            if not source_path.exists():
                raise FileNotFoundError(f"Source not found: {source_path}")

            temp_dir: str | None = None
            files: list[Path] = []

            try:
                if source_path.is_file() and source_path.suffix.lower() == ".zip":
                    temp_dir = tempfile.mkdtemp(prefix="xnatctl_gradual_")
                    temp_path = Path(temp_dir)
                    with zipfile.ZipFile(source_path, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            target = (temp_path / member.filename).resolve()
                            if not target.is_relative_to(temp_path.resolve()):
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                    files = collect_dicom_files(temp_path)
                elif source_path.is_dir():
                    files = collect_dicom_files(source_path)
                else:
                    raise ValueError("gradual-DICOM requires a directory or ZIP file")

                if not files:
                    return UploadSummary(
                        success=False,
                        total=0,
                        succeeded=0,
                        failed=0,
                        duration=time.time() - start_time,
                        errors=["No DICOM files found"],
                    )

                # Prefer stable relative paths in logs/errors (especially for ZIP
                # extractions into a temp directory).
                display_root = Path(temp_dir) if temp_dir else source_path

                return self._upload_dicom_gradual_from_files(
                    files=files,
                    display_root=display_root,
                    project=project,
                    subject=subject,
                    session=session,
                    workers=workers,
                    direct_archive=direct_archive,
                    progress_callback=progress_callback,
                    start_time=start_time,
                )

            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)

    def upload_dicom_gradual_files(
        self,
        *,
        files: Sequence[Path],
        project: str,
        subject: str,
        session: str,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        direct_archive: bool = True,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload a specific list of DICOM files via the gradual-DICOM handler.

        Unlike :meth:`upload_dicom_gradual`, this method uploads only the files
        explicitly provided and does not scan any directories.

        Args:
            files: Explicit list of files to upload.
            project: Target project ID.
            subject: Target subject label.
            session: Target session label.
            direct_archive: Use direct archive vs prearchive (default: True).
            workers: Number of parallel upload workers.
            progress_callback: Optional callback for progress updates.

        Returns:
            UploadSummary with results.

        Raises:
            FileNotFoundError: If any provided path does not exist.
            ValueError: If any provided path is not a file.
        """
        with _gradual_http_clients_scope():
            start_time = time.time()
            file_list = [Path(p) for p in files]
            if not file_list:
                return UploadSummary(
                    success=False,
                    total=0,
                    succeeded=0,
                    failed=0,
                    duration=0.0,
                    errors=["No files provided"],
                )

            for p in file_list:
                if not p.exists():
                    raise FileNotFoundError(f"File not found: {p}")
                if not p.is_file():
                    raise ValueError(f"Not a file: {p}")

            dicom_file_list = [p for p in file_list if _is_dicom_like_path(p)]
            if not dicom_file_list:
                return UploadSummary(
                    success=False,
                    total=0,
                    succeeded=0,
                    failed=0,
                    duration=0.0,
                    errors=["No DICOM files found"],
                )

            resolved_to_original: dict[Path, Path] = {}
            duplicate_resolved: set[Path] = set()
            for p in dicom_file_list:
                resolved = p.expanduser().resolve(strict=False)
                if resolved in resolved_to_original:
                    duplicate_resolved.add(resolved)
                else:
                    resolved_to_original[resolved] = p

            if duplicate_resolved:
                dup_str = ", ".join(sorted(str(p) for p in duplicate_resolved))
                raise ValueError(f"Duplicate file paths provided: {dup_str}")

            # Use a stable common root for relative display paths.
            try:
                common = Path(os.path.commonpath([str(p.resolve()) for p in dicom_file_list]))
                display_root = common if common.is_dir() else common.parent
            except Exception:
                display_root = dicom_file_list[0].parent

            return self._upload_dicom_gradual_from_files(
                files=dicom_file_list,
                display_root=display_root,
                project=project,
                subject=subject,
                session=session,
                workers=workers,
                direct_archive=direct_archive,
                progress_callback=progress_callback,
                start_time=start_time,
            )

    def _upload_dicom_gradual_from_files(  # noqa: C901  # pre-existing; see pyproject
        self,
        *,
        files: Sequence[Path],
        display_root: Path,
        project: str,
        subject: str,
        session: str,
        workers: int,
        direct_archive: bool = True,
        progress_callback: Callable[[UploadProgress], None] | None,
        start_time: float,
    ) -> UploadSummary:
        """Upload a precomputed list of files using the gradual-DICOM handler.

        Args:
            files: Files to upload.
            display_root: Root used for stable relative display paths.
            project: Target project ID.
            subject: Target subject label.
            session: Target session label.
            workers: Number of parallel upload workers.
            direct_archive: Use direct archive vs prearchive (default: True).
            progress_callback: Optional callback for progress updates.
            start_time: Start timestamp for duration calculation.

        Returns:
            UploadSummary with results.
        """
        base_url = self.client.base_url
        verify_ssl = self.client.httpx_verify()
        session_refresher = SessionRefresher(
            base_url=base_url,
            verify_ssl=verify_ssl,
            token=self.client.session_token,
            username=self.client.username,
            password=self.client.password,
            # So the phases that run after this upload inherit the refreshed
            # token instead of the one it started with.
            owner=self.client,
        )

        file_list = list(files)

        def report(phase: OperationPhase, **kwargs: Any) -> None:
            if progress_callback:
                progress_callback(UploadProgress(phase=phase, **kwargs))

        report(
            OperationPhase.PREPARING,
            total=len(file_list),
            message=f"Found {len(file_list)} files for gradual-DICOM upload",
        )

        total_files = len(file_list)

        def display(path: Path) -> str:
            try:
                return str(path.relative_to(display_root))
            except Exception:
                return path.name

        failed_paths: set[Path] = set()
        error_by_path: dict[Path, str] = {}
        completed = 0

        # Warm-up: upload a small set of files sequentially before going wide-parallel.
        #
        # XNAT can return transient HTTP 400s when a session/scan is being created in
        # prearchive. With high concurrency, multiple workers can hit that "cold start"
        # race at the same time.
        def scan_id_for(path: Path) -> str | None:
            """Extract scan ID from standard session layout paths, if present."""
            try:
                rel = path.relative_to(display_root)
            except Exception:
                return None
            parts = rel.parts
            # Expected layout: scans/<scan_id>/resources/DICOM/files/<...>
            if (
                len(parts) >= 6
                and parts[0] == "scans"
                and parts[2] == "resources"
                and parts[3] == "DICOM"
                and parts[4] == "files"
            ):
                return parts[1]
            return None

        def _scan_sort_key(scan_id: str) -> tuple[int, int, str]:
            try:
                return (0, int(scan_id), scan_id)
            except ValueError:
                return (1, 0, scan_id)

        scan_groups: dict[str, list[Path]] = {}
        other_files: list[Path] = []
        for p in file_list:
            sid = scan_id_for(p)
            if sid:
                scan_groups.setdefault(sid, []).append(p)
            else:
                other_files.append(p)

        warmup_files: list[Path] = []
        remaining_files: list[Path] = []

        if scan_groups:
            # Warm up one file per scan (capped) and interleave remaining uploads
            # across scans to reduce per-scan contention under high worker counts.
            from collections import deque

            queues: dict[str, deque[Path]] = {
                sid: deque(paths) for sid, paths in scan_groups.items()
            }
            if other_files:
                queues["_other"] = deque(other_files)

            scan_ids = sorted(queues.keys(), key=_scan_sort_key)
            max_warmup_scans = min(50, len(scan_ids))
            warmup_scan_ids = [sid for sid in scan_ids if sid != "_other"][:max_warmup_scans]

            for sid in warmup_scan_ids:
                q = queues.get(sid)
                if q:
                    warmup_files.append(q.popleft())

            # Round-robin remaining files across scan queues
            scan_order = deque(scan_ids)
            while scan_order:
                sid = scan_order.popleft()
                q = queues.get(sid)
                if not q:
                    queues.pop(sid, None)
                    continue
                remaining_files.append(q.popleft())
                if q:
                    scan_order.append(sid)
                else:
                    queues.pop(sid, None)
        else:
            # Fallback: warm up a few files in provided order
            warmup_n = min(5, total_files)
            warmup_files = file_list[:warmup_n]
            remaining_files = file_list[warmup_n:]

        if warmup_files:
            report(
                OperationPhase.PREPARING,
                message=f"Warming up gradual-DICOM upload with {len(warmup_files)} file(s)...",
            )

        # No cancel_token here on purpose: the warmup is sequential and runs on
        # the main thread, so Ctrl+C interrupts its retry sleep directly. The
        # token only earns its place where work happens in worker threads.
        for p in warmup_files:
            _name, ok, err = _upload_single_file_gradual(
                base_url=base_url,
                session_refresher=session_refresher,
                verify_ssl=verify_ssl,
                file_path=p,
                display_path=display(p),
                project=project,
                subject=subject,
                session=session,
                direct_archive=direct_archive,
            )
            completed += 1
            if not ok:
                failed_paths.add(p)
                error_by_path[p] = err

            succeeded_so_far = completed - len(failed_paths)
            report(
                OperationPhase.UPLOADING,
                current=completed,
                total=total_files,
                success=ok,
                message=(
                    f"Uploaded {completed}/{total_files} "
                    f"({succeeded_so_far} ok, {len(failed_paths)} failed)"
                ),
            )

        # Circuit-breaker. The warmup exists to try a small, deterministic set
        # before opening the throttle; if the server refused every one of them
        # for the same reason, the remaining files will be refused for that
        # reason too. Stopping here is the difference between one round of
        # errors and hours of them: the wide-parallel phase plus two salvage
        # passes would otherwise retry every file in a 100k-file directory
        # before reporting the same message.
        if warmup_files and len(failed_paths) == len(warmup_files):
            reasons = {_error_signature(error_by_path.get(p, "")) for p in warmup_files}
            if len(reasons) == 1:
                reason = error_by_path.get(warmup_files[0], "").strip()
                message = (
                    f"Server rejected all {len(warmup_files)} warmup files with the same "
                    f"error, so the remaining {total_files - len(warmup_files)} would fail "
                    f"the same way: {reason}. Check the project, subject and session "
                    f"labels before retrying."
                )
                logger.error("Aborting gradual upload: %s", message)
                report(OperationPhase.ERROR, message=message, success=False, errors=[message])
                return UploadSummary(
                    success=False,
                    total=total_files,
                    succeeded=0,
                    failed=len(warmup_files),
                    duration=time.time() - start_time,
                    errors=[message],
                    session_id=session,
                )

        # Main pass: parallel per-file upload (bounded in-flight window)
        with cancellable_pool(workers) as (executor, gradual_token):
            prefetch = max(1, workers * 2)
            file_iter = iter(remaining_files)

            in_flight: set[Future[tuple[str, bool, str]]] = set()
            future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

            def _submit_one(path: Path) -> None:
                # This pass refills its window as files complete, so without
                # this check a cancelled run would keep feeding itself the rest
                # of the directory one file at a time.
                if gradual_token.cancelled:
                    raise StopIteration
                fut: Future[tuple[str, bool, str]] = executor.submit(
                    _upload_single_file_gradual,
                    base_url=base_url,
                    session_refresher=session_refresher,
                    verify_ssl=verify_ssl,
                    file_path=path,
                    display_path=display(path),
                    project=project,
                    subject=subject,
                    session=session,
                    direct_archive=direct_archive,
                    cancel_token=gradual_token,
                )
                in_flight.add(fut)
                future_to_path[fut] = path

            for _ in range(min(prefetch, len(remaining_files))):
                try:
                    _submit_one(next(file_iter))
                except StopIteration:
                    break

            while in_flight:
                done, _pending = wait(in_flight, return_when=FIRST_COMPLETED)
                in_flight = _pending

                for future in done:
                    completed += 1
                    p = future_to_path.pop(future)

                    try:
                        _name, ok, err = future.result()
                    except Exception as e:
                        ok = False
                        err = str(e)

                    if not ok:
                        failed_paths.add(p)
                        error_by_path[p] = err

                    succeeded_so_far = completed - len(failed_paths)
                    report(
                        OperationPhase.UPLOADING,
                        current=completed,
                        total=total_files,
                        success=ok,
                        message=(
                            f"Uploaded {completed}/{total_files} "
                            f"({succeeded_so_far} ok, {len(failed_paths)} failed)"
                        ),
                    )

                    while len(in_flight) < prefetch:
                        try:
                            _submit_one(next(file_iter))
                        except StopIteration:
                            break

        # Salvage pass: retry a small number of failed files at lower concurrency.
        # This helps when XNAT returns transient 400s under high parallel load.
        max_salvage = min(5000, max(500, int(total_files * 0.01)))
        if failed_paths and len(failed_paths) <= max_salvage:
            retry_workers = max(1, min(4, workers))
            report(
                OperationPhase.PREPARING,
                message=(
                    f"Retrying {len(failed_paths)} failed file(s) "
                    f"at lower concurrency ({retry_workers} workers)..."
                ),
            )

            to_retry = sorted(failed_paths, key=display)
            remaining_failed: set[Path] = set(failed_paths)

            with cancellable_pool(retry_workers, gradual_token) as (retry_executor, _):
                prefetch = max(1, retry_workers * 2)
                retry_iter = iter(to_retry)
                retry_in_flight: set[Future[tuple[str, bool, str]]] = set()
                retry_future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

                def _submit_retry(path: Path) -> None:
                    # Same two guards as the main pass. Without them the
                    # salvage pass ran on NULL_TOKEN: after Ctrl+C every
                    # in-flight file sat out its full 2+4+8+16+32s retry
                    # ladder before shutdown(wait=True) could return -- the
                    # exact wait cooperative cancellation exists to remove,
                    # reintroduced in the one pass whose files are already
                    # known to be failing.
                    if gradual_token.cancelled:
                        raise StopIteration
                    fut: Future[tuple[str, bool, str]] = retry_executor.submit(
                        _upload_single_file_gradual,
                        base_url=base_url,
                        session_refresher=session_refresher,
                        verify_ssl=verify_ssl,
                        file_path=path,
                        display_path=display(path),
                        project=project,
                        subject=subject,
                        session=session,
                        direct_archive=direct_archive,
                        cancel_token=gradual_token,
                    )
                    retry_in_flight.add(fut)
                    retry_future_to_path[fut] = path

                for _ in range(min(prefetch, len(to_retry))):
                    try:
                        _submit_retry(next(retry_iter))
                    except StopIteration:
                        break

                while retry_in_flight:
                    done, _pending = wait(retry_in_flight, return_when=FIRST_COMPLETED)
                    retry_in_flight = _pending

                    for future in done:
                        p = retry_future_to_path.pop(future)
                        try:
                            _name, ok, err = future.result()
                        except Exception as e:
                            ok = False
                            err = str(e)

                        if ok:
                            remaining_failed.discard(p)
                            error_by_path.pop(p, None)
                        else:
                            error_by_path[p] = err

                        while len(retry_in_flight) < prefetch:
                            try:
                                _submit_retry(next(retry_iter))
                            except StopIteration:
                                break

            failed_paths = remaining_failed

        # Final safety net: if only a handful of files are still failing, retry them
        # sequentially.
        if failed_paths and len(failed_paths) <= 50:
            report(
                OperationPhase.PREPARING,
                message=f"Final sequential retry for {len(failed_paths)} file(s)...",
            )

            remaining_failed = set[Path]()
            for p in sorted(failed_paths, key=display):
                _name, ok, err = _upload_single_file_gradual(
                    base_url=base_url,
                    session_refresher=session_refresher,
                    verify_ssl=verify_ssl,
                    file_path=p,
                    display_path=display(p),
                    project=project,
                    subject=subject,
                    session=session,
                    direct_archive=direct_archive,
                )
                if ok:
                    error_by_path.pop(p, None)
                else:
                    remaining_failed.add(p)
                    error_by_path[p] = err

            failed_paths = remaining_failed

        duration = time.time() - start_time
        failed = len(failed_paths)
        succeeded = total_files - failed
        overall_success = failed == 0

        errors = [
            f"{display(p)}: {error_by_path.get(p, '')}".rstrip(": ")
            for p in sorted(failed_paths, key=display)
        ]

        report(
            OperationPhase.COMPLETE if overall_success else OperationPhase.ERROR,
            current=total_files,
            total=total_files,
            message=(
                f"Uploaded {succeeded} files via gradual-DICOM"
                if overall_success
                else f"Uploaded {succeeded}/{total_files} files ({failed} failed)"
            ),
            success=overall_success,
            errors=errors,
        )

        return UploadSummary(
            success=overall_success,
            total=total_files,
            succeeded=succeeded,
            failed=failed,
            duration=duration,
            errors=errors,
            total_files=total_files,
            session_id=session,
        )

    @staticmethod
    def _notify_upload_error(
        progress_callback: Callable[[UploadProgress], None] | None,
        exc: Exception,
    ) -> None:
        """Emit an ERROR progress event without ever masking the failure."""
        if progress_callback is None:
            return
        with contextlib.suppress(Exception):
            progress_callback(
                UploadProgress(
                    phase=OperationPhase.ERROR,
                    message=str(exc),
                    success=False,
                    errors=[str(exc)],
                )
            )

    def upload_resource(  # noqa: C901  # pre-existing; see pyproject
        self,
        session_id: str,
        resource_label: str,
        source_path: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        overwrite: bool = False,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload files to a resource.

        Args:
            session_id: Session ID.
            resource_label: Resource label.
            source_path: File or directory to upload.
            scan_id: Scan ID (for scan-level resources).
            project: Project ID.
            extract: Extract ZIP/TAR after upload.
            overwrite: Overwrite existing files.
            progress_callback: Progress callback.

        Returns:
            UploadSummary describing the completed upload (always a success;
            failures raise).

        Raises:
            FileNotFoundError: If ``source_path`` does not exist.
            XNATCtlError: Any typed failure from the client layer (authentication,
                permission, not-found) passes through untouched.
            UploadError: Any other failure (HTTP error, OSError, unexpected
                exception) wrapped with the source path and ``__cause__`` set.
        """
        start_time = time.time()
        source_path = Path(source_path)
        upload_source = source_path

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        temp_zip: Path | None = None
        try:
            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.PREPARING,
                        message="Preparing upload",
                    )
                )

            if scan_id:
                if project:
                    base_path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
                else:
                    base_path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
            else:
                if project:
                    base_path = f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}/files"
                else:
                    base_path = f"/data/experiments/{session_id}/resources/{resource_label}/files"

            # A directory source is zipped to a temp file that MUST be removed on
            # every exit path. It used to leak on all of them: a 50 GB resource
            # directory left a 50 GB zip behind on each invocation, and
            # `session upload-exam` calls this once per resource directory.
            if source_path.is_dir():
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    zip_path = Path(tmp.name)

                # Assigned before make_archive so a failed archive build is
                # still cleaned up by the finally below.
                temp_zip = zip_path
                shutil.make_archive(str(zip_path.with_suffix("")), "zip", source_path)
                source_path = zip_path
                extract = True

            file_size = source_path.stat().st_size

            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.UPLOADING,
                        total_bytes=file_size,
                        message=f"Uploading {source_path.name}",
                    )
                )

            params: dict[str, Any] = {}
            if extract:
                params["extract"] = "true"
            if overwrite:
                params["overwrite"] = "true"

            path = f"{base_path}/{source_path.name}"

            base_url = self.client.base_url
            session_token = self.client.session_token
            verify_ssl = self.client.httpx_verify()
            res_timeout = self.client.timeout
            cookies = {"JSESSIONID": session_token} if session_token else {}

            with httpx.Client(
                base_url=base_url,
                timeout=build_httpx_timeout(res_timeout),  # connect fails fast
                verify=verify_ssl,
            ) as http:

                def _attempt() -> httpx.Response:
                    with open(source_path, "rb") as f:
                        return http.put(
                            path,
                            params=params,
                            content=f,
                            headers={"Content-Type": "application/octet-stream"},
                            cookies=cookies,
                        )

                resp = upload_with_retry(_attempt, label=f"resource {source_path.name}")
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

            duration = time.time() - start_time

            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.COMPLETE,
                        bytes_sent=file_size,
                        total_bytes=file_size,
                        message="Upload complete",
                        success=True,
                    )
                )

            return UploadSummary(
                success=True,
                total=1,
                succeeded=1,
                failed=0,
                duration=duration,
                total_files=1,
                total_size_mb=file_size / (1024 * 1024),
                session_id=session_id,
            )

        except XNATCtlError as e:
            # Typed failures already carry the right class and exit code; the
            # notification fires (suppressed if the callback itself raises, so
            # it can never mask the failure), then the exception propagates
            # unchanged.
            self._notify_upload_error(progress_callback, e)
            raise
        except Exception as e:
            self._notify_upload_error(progress_callback, e)
            raise UploadError(str(e), file_path=str(upload_source)) from e
        finally:
            # Covers the success return and both raising branches above.
            if temp_zip is not None:
                temp_zip.unlink(missing_ok=True)

    def _split_into_batches(
        self,
        files: list[Path],
        batch_size: int,
    ) -> Iterator[list[Path]]:
        """Split files into batches.

        Args:
            files: List of file paths.
            batch_size: Maximum files per batch.

        Yields:
            Lists of files for each batch.
        """
        for i in range(0, len(files), batch_size):
            yield files[i : i + batch_size]
