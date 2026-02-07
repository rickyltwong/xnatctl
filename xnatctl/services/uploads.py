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

import logging
import os
import shutil
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS
from xnatctl.models.progress import (
    OperationPhase,
    UploadProgress,
    UploadSummary,
)

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

UPLOAD_MAX_RETRIES = 5
UPLOAD_RETRY_BACKOFF_BASE = 2  # seconds: 2, 4, 8, 16, 32
RETRYABLE_STATUS_CODES = {400, 429, 500, 502, 503, 504}


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

        if path.name.startswith("."):
            continue

        if path.is_symlink():
            try:
                resolved = path.resolve()
                if not resolved.exists():
                    continue
            except (OSError, ValueError):
                continue

        suffix = path.suffix.lower()
        if suffix in DICOM_EXTENSIONS:
            files.append(path)
        elif include_extensionless and suffix == "":
            files.append(path)

    return sorted(files)


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


def is_retryable_status(status_code: int) -> bool:
    """Check if an HTTP status code warrants a retry.

    Retryable: 400 (XNAT transient), 429 (rate limit), 5xx (server errors).
    Non-retryable: 2xx (success), 401/403 (auth), other 4xx (client error).
    """
    return status_code in RETRYABLE_STATUS_CODES


def upload_with_retry(
    upload_fn: Callable[[], Any],
    *,
    max_retries: int = UPLOAD_MAX_RETRIES,
    backoff_base: int = UPLOAD_RETRY_BACKOFF_BASE,
    label: str = "upload",
) -> Any:
    """Execute an upload function with retry on transient HTTP errors.

    Args:
        upload_fn: Callable that performs the upload and returns an httpx.Response.
                   Will be called multiple times on retry -- must be idempotent.
        max_retries: Maximum number of retries (default: 5).
        backoff_base: Base for exponential backoff in seconds (default: 2).
        label: Label for log messages.

    Returns:
        The httpx.Response from a successful attempt.

    Raises:
        The last exception if all retries are exhausted and no response was obtained.
    """
    last_resp = None
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = upload_fn()
            if not is_retryable_status(resp.status_code):
                return resp
            last_resp = resp
            last_exc = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                logger.warning(
                    "%s: HTTP %d on attempt %d/%d, retrying in %ds",
                    label,
                    resp.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            last_resp = None
            if attempt < max_retries:
                delay = backoff_base ** (attempt + 1)
                logger.warning(
                    "%s: %s on attempt %d/%d, retrying in %ds",
                    label,
                    type(e).__name__,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)

    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label}: all retries exhausted with no response")


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
# Parallel Upload Helpers (private, thread-safe standalone functions)
# =============================================================================


def _upload_single_archive(
    *,
    base_url: str,
    username: str | None,
    password: str | None,
    session_token: str | None,
    verify_ssl: bool,
    timeout: int,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
) -> tuple[bool, str]:
    """Upload a single archive file to XNAT.

    Creates a fresh httpx client for thread-safety in parallel execution.

    Returns:
        Tuple of (success, error_message).
    """
    name = archive_path.name.lower()
    content_type = (
        "application/x-tar" if name.endswith((".tar", ".tar.gz", ".tgz")) else "application/zip"
    )

    params = {
        "import-handler": import_handler,
        "Ignore-Unparsable": "true" if ignore_unparsable else "false",
        "project": project,
        "subject": subject,
        "session": session,
        "overwrite": overwrite,
        "overwrite_files": "true",
        "quarantine": "false",
        "triggerPipelines": "true",
        "rename": "false",
        "Direct-Archive": "true" if direct_archive else "false",
        "inbody": "true",
    }

    with httpx.Client(
        base_url=base_url,
        timeout=timeout,
        verify=verify_ssl,
    ) as client:
        try:
            cookies: dict[str, str] = {}
            created_session = False

            if session_token:
                cookies = {"JSESSIONID": session_token}
            else:
                if not username or not password:
                    return False, "Authentication failed: missing credentials"

                auth_resp = client.post(
                    "/data/JSESSION",
                    auth=(str(username), str(password)),
                )
                if auth_resp.status_code != 200:
                    return False, f"Authentication failed: HTTP {auth_resp.status_code}"

                if "<html" in auth_resp.text.lower():
                    return False, "Authentication failed: invalid credentials"

                session_token = auth_resp.text.strip()
                cookies = {"JSESSIONID": session_token}
                created_session = True

            def _attempt() -> httpx.Response:
                with archive_path.open("rb") as data:
                    return client.post(
                        "/data/services/import",
                        params=params,
                        headers={"Content-Type": content_type},
                        content=data,
                        cookies=cookies,
                    )

            try:
                resp = upload_with_retry(_attempt, label=f"batch {archive_path.name}")
            finally:
                if created_session:
                    try:
                        client.delete("/data/JSESSION", cookies=cookies)
                    except Exception:
                        pass

            if resp.status_code == 200:
                return True, ""
            if resp.status_code in (401, 403):
                return False, "Authentication failed: invalid or expired session"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"

        except httpx.TimeoutException:
            return False, "Upload timed out (after retries)"
        except httpx.ConnectError as e:
            return False, f"Connection failed (after retries): {e}"
        except Exception as e:
            return False, str(e)


def _upload_batch(
    *,
    base_url: str,
    username: str | None,
    password: str | None,
    session_token: str | None,
    verify_ssl: bool,
    timeout: int,
    batch_id: int,
    archive_path: Path,
    file_count: int,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
) -> _UploadResult:
    """Upload a single batch archive, returning an _UploadResult."""
    archive_size = archive_path.stat().st_size
    start_time = time.time()

    try:
        success, error = _upload_single_archive(
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
        )
        return _UploadResult(
            batch_id=batch_id,
            success=success,
            duration=time.time() - start_time,
            file_count=file_count,
            archive_size=archive_size,
            error=error,
        )
    except Exception as e:
        return _UploadResult(
            batch_id=batch_id,
            success=False,
            duration=time.time() - start_time,
            file_count=file_count,
            archive_size=archive_size,
            error=str(e),
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
    verify_ssl: bool,
    timeout: int,
    batch_id: int,
    project: str,
    subject: str,
    session: str,
    import_handler: str,
    ignore_unparsable: bool,
    overwrite: str,
    direct_archive: bool,
) -> _UploadResult:
    """Create archive, upload it, then delete the archive immediately.

    Combines archive creation and upload into a single task to reduce peak
    disk and memory usage. The archive is deleted as soon as the upload
    completes (or fails), preventing all archives from existing on disk
    simultaneously.
    """
    start_time = time.time()
    archive_size = 0

    try:
        archive_size = _create_archive(batch, archive_path, source_path, archive_format)

        success, error = _upload_single_archive(
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
        )

        return _UploadResult(
            batch_id=batch_id,
            success=success,
            duration=time.time() - start_time,
            file_count=len(batch),
            archive_size=archive_size,
            error=error,
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


def _check_dicom_deps() -> None:
    """Check if DICOM dependencies are available.

    Raises:
        ImportError: If pydicom or pynetdicom are not installed.
    """
    try:
        import pydicom  # noqa: F401
        import pynetdicom  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "DICOM C-STORE requires pydicom and pynetdicom. "
            "Install with: pip install xnatctl[dicom]"
        ) from e


def _get_verification_sop_class():
    """Get VerificationSOPClass with compatibility for pynetdicom versions."""
    from pynetdicom import sop_class as _sop_class

    verification_uid = "1.2.840.10008.1.1"
    return getattr(
        _sop_class,
        "VerificationSOPClass",
        getattr(_sop_class, "Verification", verification_uid),
    )


def _get_storage_contexts():
    """Get storage presentation contexts with version compatibility."""
    try:
        from pynetdicom import StoragePresentationContexts

        return list(StoragePresentationContexts)
    except ImportError:
        from pynetdicom import sop_class as _sc
        from pynetdicom.presentation import build_context

        uids = [getattr(_sc, name) for name in dir(_sc) if name.endswith("Storage")]
        return [build_context(uid) for uid in uids]


def _ensure_sop_uids(ds) -> None:
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


def _c_echo(host: str, port: int, calling_aet: str, called_aet: str) -> bool:
    """Send a C-ECHO to verify connectivity and AE titles.

    Args:
        host: DICOM SCP host.
        port: DICOM SCP port.
        calling_aet: Our AE title.
        called_aet: Remote AE title.

    Returns:
        True if C-ECHO succeeded.
    """
    _check_dicom_deps()
    from pynetdicom import AE

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(_get_verification_sop_class())

    assoc = ae.associate(host, port, ae_title=called_aet)
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

    Returns:
        Tuple of (sent_count, failed_count).
    """
    _check_dicom_deps()
    import pydicom
    from pydicom.errors import InvalidDicomError
    from pynetdicom import AE

    sent = failed = 0
    log_path = log_dir / f"{batch_id}.log"

    with log_path.open("w") as log:
        ae = AE(ae_title=calling_aet)
        ae.requested_contexts = _get_storage_contexts()
        ae.add_requested_context("1.3.12.2.1107.5.9.1")

        assoc = ae.associate(host, port, ae_title=called_aet)
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
    session_token: str | None,
    verify_ssl: bool,
    file_path: Path,
    display_path: str | None = None,
    project: str,
    subject: str,
    session: str,
) -> tuple[str, bool, str]:
    """Upload a single file via the gradual-DICOM import handler.

    Creates a fresh httpx client for thread-safety in parallel execution.

    Args:
        base_url: XNAT server base URL.
        session_token: JSESSIONID token.
        verify_ssl: Whether to verify SSL certificates.
        file_path: Path to the DICOM file.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.

    Returns:
        Tuple of (filename, success, error_message).
    """
    name = display_path or file_path.name

    try:
        with httpx.Client(base_url=base_url, timeout=120.0, verify=verify_ssl) as client:
            cookies = {"JSESSIONID": session_token} if session_token else {}

            def _attempt() -> httpx.Response:
                with open(file_path, "rb") as f:
                    return client.post(
                        "/data/services/import",
                        params={
                            "inbody": "true",
                            "import-handler": "gradual-DICOM",
                            "PROJECT_ID": project,
                            "SUBJECT_ID": subject,
                            "EXPT_LABEL": session,
                        },
                        content=f,
                        headers={"Content-Type": "application/dicom"},
                        cookies=cookies,
                    )

            resp = upload_with_retry(_attempt, label=f"gradual-DICOM {name}")
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

    def upload_dicom(
        self,
        project: str,
        subject: str,
        session: str,
        source_path: Path,
        overwrite: bool = False,
        quarantine: bool = False,
        batch_size: int = DEFAULT_BATCH_SIZE,
        parallel: bool = True,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files via simple REST batch (ZIP per batch).

        Args:
            project: Project ID.
            subject: Subject label.
            session: Session label.
            source_path: Path to DICOM files (directory or ZIP).
            overwrite: Overwrite existing scans.
            quarantine: Send to prearchive instead.
            batch_size: Files per upload batch.
            parallel: Use parallel uploads.
            workers: Number of parallel workers.
            progress_callback: Progress callback function.

        Returns:
            UploadSummary with results.
        """
        start_time = time.time()
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        if progress_callback:
            progress_callback(
                UploadProgress(
                    phase=OperationPhase.PREPARING,
                    message="Preparing upload",
                )
            )

        # Collect DICOM files
        temp_dir: str | None = None
        try:
            dicom_files: list[Path] = []
            if source_path.is_file():
                if source_path.suffix.lower() == ".zip":
                    temp_dir = tempfile.mkdtemp()
                    temp_root = Path(temp_dir)
                    with zipfile.ZipFile(source_path, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            target = (temp_root / member.filename).resolve()
                            if not target.is_relative_to(temp_root.resolve()):
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                    source_path = Path(temp_dir)
                    dicom_files = list(source_path.rglob("*"))
                else:
                    dicom_files = [source_path]
            else:
                dicom_files = [
                    f for f in source_path.rglob("*") if f.is_file() and not f.name.startswith(".")
                ]

            dicom_files = [
                f
                for f in dicom_files
                if f.is_file() and f.suffix.lower() in ("", ".dcm", ".dicom", ".ima")
            ]

            total_files = len(dicom_files)
            if total_files == 0:
                return UploadSummary(
                    success=False,
                    total=0,
                    succeeded=0,
                    failed=0,
                    duration=0,
                    errors=["No DICOM files found"],
                )

            total_size = sum(f.stat().st_size for f in dicom_files)

            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.ARCHIVING,
                        total=total_files,
                        message=f"Found {total_files} files",
                    )
                )

            batches = list(self._split_into_batches(dicom_files, batch_size))
            total_batches = len(batches)
            results: dict[str, Any] = {"succeeded": 0, "failed": 0, "errors": []}

            dest = f"/archive/projects/{project}/subjects/{subject}/experiments/{session}"

            base_url = self.client.base_url
            session_token = self.client.session_token
            verify_ssl = self.client.verify_ssl
            timeout = self.client.timeout

            def _upload_batch_fn(batch_id: int, files: list[Path]) -> tuple[bool, str]:
                try:
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        zip_path = Path(tmp.name)

                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for file_path in files:
                            zf.write(file_path, file_path.name)

                    params: dict[str, Any] = {
                        "dest": dest,
                        "overwrite": "delete" if overwrite else "none",
                        "import-handler": "SI",
                        "PROJECT_ID": project,
                        "SUBJECT_ID": subject,
                        "EXPT_LABEL": session,
                    }
                    if quarantine:
                        params["dest"] = f"/prearchive/projects/{project}"

                    cookies = {"JSESSIONID": session_token} if session_token else {}
                    with httpx.Client(
                        base_url=base_url,
                        timeout=timeout,
                        verify=verify_ssl,
                    ) as http:
                        with open(zip_path, "rb") as zip_file:
                            http.post(
                                "/data/services/import",
                                params=params,
                                content=zip_file,
                                headers={"Content-Type": "application/zip"},
                                cookies=cookies,
                            )

                    zip_path.unlink()
                    return (True, "")
                except Exception as e:
                    return (False, str(e))

            if parallel and total_batches > 1:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(_upload_batch_fn, i, batch): i
                        for i, batch in enumerate(batches)
                    }
                    for future in as_completed(futures):
                        batch_id = futures[future]
                        success, error = future.result()
                        if success:
                            results["succeeded"] += 1
                        else:
                            results["failed"] += 1
                            results["errors"].append(f"Batch {batch_id}: {error}")
                        if progress_callback:
                            progress_callback(
                                UploadProgress(
                                    phase=OperationPhase.UPLOADING,
                                    current=results["succeeded"] + results["failed"],
                                    total=total_batches,
                                    batch_id=batch_id,
                                    message=f"Uploading batch {batch_id + 1}/{total_batches}",
                                    success=success,
                                )
                            )
            else:
                for batch_id, batch in enumerate(batches):
                    success, error = _upload_batch_fn(batch_id, batch)
                    if success:
                        results["succeeded"] += 1
                    else:
                        results["failed"] += 1
                        results["errors"].append(f"Batch {batch_id}: {error}")
                    if progress_callback:
                        progress_callback(
                            UploadProgress(
                                phase=OperationPhase.UPLOADING,
                                current=batch_id + 1,
                                total=total_batches,
                                batch_id=batch_id,
                                message=f"Uploading batch {batch_id + 1}/{total_batches}",
                                success=success,
                            )
                        )

            duration = time.time() - start_time
            overall_success = results["failed"] == 0

            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.COMPLETE if overall_success else OperationPhase.ERROR,
                        current=total_batches,
                        total=total_batches,
                        message="Upload complete"
                        if overall_success
                        else "Upload completed with errors",
                        success=overall_success,
                        errors=results["errors"],
                    )
                )

            return UploadSummary(
                success=overall_success,
                total=total_batches,
                succeeded=results["succeeded"],
                failed=results["failed"],
                duration=duration,
                errors=results["errors"],
                total_files=total_files,
                total_size_mb=total_size / (1024 * 1024),
                batches_total=total_batches,
                batches_succeeded=results["succeeded"],
                batches_failed=results["failed"],
                session_id=session,
            )

        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

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
        verify_ssl = self.client.verify_ssl
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

            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                futures: dict[Future[_UploadResult], int] = {}
                for i, batch in enumerate(batches):
                    fut: Future[_UploadResult] = executor.submit(  # type: ignore[arg-type]
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
                    )
                    futures[fut] = i + 1

                for done in as_completed(futures):  # type: ignore[arg-type]
                    result: _UploadResult = done.result()  # type: ignore[assignment]
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

        Returns:
            DICOMStoreSummary with results.

        Raises:
            ImportError: If pydicom/pynetdicom are not installed.
            ValueError: If dicom_root is not a directory.
            RuntimeError: If C-ECHO fails or no DICOM files found.
        """
        _check_dicom_deps()

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
            if not _c_echo(host, port, calling_aet, called_aet):
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

            with ThreadPoolExecutor(max_workers=len(batches)) as pool:
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
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files using the gradual-DICOM handler (parallel per-file).

        Each file is uploaded individually to the XNAT import service using
        the gradual-DICOM handler, which lets XNAT parse each file on ingest.
        Files are uploaded in parallel using per-thread HTTP clients.

        Accepts directories or ZIP archives. ZIP archives are extracted to a
        temporary directory before upload. All non-hidden files are sent
        (the gradual-DICOM handler decides what is parsable).

        Args:
            source_path: Directory or ZIP file containing DICOM files.
            project: Target project ID.
            subject: Target subject label.
            session: Target session label.
            workers: Number of parallel upload workers (default: 4).
            progress_callback: Optional callback for progress updates.

        Returns:
            UploadSummary with results.

        Raises:
            ValueError: If source_path is not a directory or ZIP file.
            FileNotFoundError: If source_path does not exist.
        """
        start_time = time.time()
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        base_url = self.client.base_url
        session_token = self.client.session_token
        verify_ssl = self.client.verify_ssl

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
                files = sorted(
                    f for f in temp_path.rglob("*") if f.is_file() and not f.name.startswith(".")
                )
            elif source_path.is_dir():
                files = sorted(
                    f for f in source_path.rglob("*") if f.is_file() and not f.name.startswith(".")
                )
            else:
                raise ValueError("gradual-DICOM requires a directory or ZIP file")

            if not files:
                return UploadSummary(
                    success=False,
                    total=0,
                    succeeded=0,
                    failed=0,
                    duration=time.time() - start_time,
                    errors=["No files found to upload"],
                )

            def report(phase: OperationPhase, **kwargs: Any) -> None:
                if progress_callback:
                    progress_callback(UploadProgress(phase=phase, **kwargs))

            report(
                OperationPhase.PREPARING,
                total=len(files),
                message=f"Found {len(files)} files for gradual-DICOM upload",
            )

            total_files = len(files)

            # Prefer stable relative paths in logs/errors (especially for ZIP extractions
            # into a temp directory).
            display_root = source_path
            if temp_dir:
                display_root = Path(temp_dir)

            def display(path: Path) -> str:
                try:
                    return str(path.relative_to(display_root))
                except Exception:
                    return path.name

            failed_paths: set[Path] = set()
            error_by_path: dict[Path, str] = {}
            completed = 0

            # Warm-up: upload the first few files sequentially. XNAT can return transient
            # HTTP 400s when a prearchive/session is first being created, and parallel
            # workers may hit that race at startup.
            warmup_n = min(5, total_files)
            if warmup_n:
                report(
                    OperationPhase.PREPARING,
                    message=f"Warming up gradual-DICOM upload with {warmup_n} file(s)...",
                )

            warmup_files = files[:warmup_n]
            remaining_files = files[warmup_n:]

            for p in warmup_files:
                name, ok, err = _upload_single_file_gradual(
                    base_url=base_url,
                    session_token=session_token,
                    verify_ssl=verify_ssl,
                    file_path=p,
                    display_path=display(p),
                    project=project,
                    subject=subject,
                    session=session,
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
                    message=f"Uploaded {completed}/{total_files} ({succeeded_so_far} ok, {len(failed_paths)} failed)",
                )

            # Main pass: parallel per-file upload (bounded in-flight window)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                prefetch = max(1, workers * 2)
                file_iter = iter(remaining_files)

                in_flight: set[Future[tuple[str, bool, str]]] = set()
                future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

                def _submit_one(path: Path) -> None:
                    fut: Future[tuple[str, bool, str]] = executor.submit(  # type: ignore[arg-type]
                        _upload_single_file_gradual,
                        base_url=base_url,
                        session_token=session_token,
                        verify_ssl=verify_ssl,
                        file_path=path,
                        display_path=display(path),
                        project=project,
                        subject=subject,
                        session=session,
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
                        p = future_to_path.pop(future, None)

                        try:
                            _name, ok, err = future.result()
                        except Exception as e:
                            ok = False
                            err = str(e)

                        if not ok and p is not None:
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

                with ThreadPoolExecutor(max_workers=retry_workers) as retry_executor:
                    prefetch = max(1, retry_workers * 2)
                    retry_iter = iter(to_retry)
                    in_flight: set[Future[tuple[str, bool, str]]] = set()
                    future_to_path: dict[Future[tuple[str, bool, str]], Path] = {}

                    def _submit_retry(path: Path) -> None:
                        fut: Future[tuple[str, bool, str]] = retry_executor.submit(  # type: ignore[arg-type]
                            _upload_single_file_gradual,
                            base_url=base_url,
                            session_token=session_token,
                            verify_ssl=verify_ssl,
                            file_path=path,
                            display_path=display(path),
                            project=project,
                            subject=subject,
                            session=session,
                        )
                        in_flight.add(fut)
                        future_to_path[fut] = path

                    for _ in range(min(prefetch, len(to_retry))):
                        try:
                            _submit_retry(next(retry_iter))
                        except StopIteration:
                            break

                    while in_flight:
                        done, _pending = wait(in_flight, return_when=FIRST_COMPLETED)
                        in_flight = _pending

                        for future in done:
                            p = future_to_path.pop(future, None)
                            try:
                                _name, ok, err = future.result()
                            except Exception as e:
                                ok = False
                                err = str(e)

                            if p is not None:
                                if ok:
                                    remaining_failed.discard(p)
                                    error_by_path.pop(p, None)
                                else:
                                    error_by_path[p] = err

                            while len(in_flight) < prefetch:
                                try:
                                    _submit_retry(next(retry_iter))
                                except StopIteration:
                                    break

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

        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def upload_resource(
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
            UploadSummary with results.
        """
        start_time = time.time()
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

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

        if source_path.is_dir():
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                zip_path = Path(tmp.name)

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

        try:
            base_url = self.client.base_url
            session_token = self.client.session_token
            verify_ssl = self.client.verify_ssl
            res_timeout = self.client.timeout
            cookies = {"JSESSIONID": session_token} if session_token else {}

            with httpx.Client(
                base_url=base_url,
                timeout=res_timeout,
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

        except Exception as e:
            duration = time.time() - start_time

            if progress_callback:
                progress_callback(
                    UploadProgress(
                        phase=OperationPhase.ERROR,
                        message=str(e),
                        success=False,
                        errors=[str(e)],
                    )
                )

            return UploadSummary(
                success=False,
                total=1,
                succeeded=0,
                failed=1,
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )

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
