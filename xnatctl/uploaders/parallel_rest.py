"""Parallel REST uploader for DICOM data.

This module provides high-throughput DICOM upload via the XNAT REST import service.
Files are batched into archives that are uploaded in parallel.

This is an internal implementation detail. Use `UploadService` from
`xnatctl.services.uploads` as the public API.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from xnatctl.uploaders.common import collect_dicom_files, split_into_n_batches
from xnatctl.uploaders.constants import (
    DEFAULT_ARCHIVE_FORMAT,
    DEFAULT_ARCHIVE_WORKERS,
    DEFAULT_IMPORT_HANDLER,
    DEFAULT_OVERWRITE,
    DEFAULT_TIMEOUT,
    DEFAULT_UPLOAD_WORKERS,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class UploadProgress:
    """Progress information for upload callbacks."""

    phase: str  # "preparing", "archiving", "uploading", "complete", "error"
    current: int = 0
    total: int = 0
    message: str = ""
    batch_id: int = 0
    success: bool = True
    errors: list[str] = field(default_factory=list)


@dataclass
class UploadResult:
    """Result of a single batch upload."""

    batch_id: int
    success: bool
    duration: float
    file_count: int
    archive_size: int
    error: str = ""


@dataclass
class UploadSummary:
    """Summary of the complete upload operation."""

    success: bool
    total_files: int
    total_size_mb: float
    duration: float
    batches_succeeded: int
    batches_failed: int
    errors: list[str] = field(default_factory=list)


# =============================================================================
# Archive Creation
# =============================================================================


def create_tar_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a TAR archive from files.

    Args:
        files: List of file paths to include.
        output_path: Path for the output archive.
        base_dir: Base directory for relative paths in archive.

    Returns:
        Size of created archive in bytes.
    """
    with tarfile.open(output_path, "w") as tf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            tf.add(file_path, arcname=arcname)
    return output_path.stat().st_size


def create_zip_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a ZIP archive from files.

    Args:
        files: List of file paths to include.
        output_path: Path for the output archive.
        base_dir: Base directory for relative paths in archive.

    Returns:
        Size of created archive in bytes.
    """
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED, allowZip64=True) as zf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            zf.write(file_path, arcname)
    return output_path.stat().st_size


def create_archive(
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
        return create_tar_archive(files, output_path, base_dir)
    if archive_format == "zip":
        return create_zip_archive(files, output_path, base_dir)
    raise ValueError(f"Unsupported archive format: {archive_format}")


# =============================================================================
# Upload Functions
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
    # Determine content type
    name = archive_path.name.lower()
    if name.endswith((".tar", ".tar.gz", ".tgz")):
        content_type = "application/x-tar"
    else:
        content_type = "application/zip"

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

    # Create per-thread client with session auth
    with httpx.Client(
        base_url=base_url,
        timeout=timeout,
        verify=verify_ssl,
    ) as client:
        try:
            cookies: dict[str, str] = {}
            created_session = False

            if session_token:
                # Reuse existing session token
                cookies = {"JSESSIONID": session_token}
            else:
                # Need to authenticate with username/password
                if not username or not password:
                    return False, "Authentication failed: missing credentials"

                # Type narrowing: we know username/password are str after the check
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

            # Upload the archive with retry on transient errors
            from xnatctl.uploaders.common import upload_with_retry

            def _attempt():
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
                # Logout if we created the session
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
) -> UploadResult:
    """Upload a single batch archive.

    Args:
        Various upload parameters.

    Returns:
        UploadResult with success/failure details.
    """
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

        return UploadResult(
            batch_id=batch_id,
            success=success,
            duration=time.time() - start_time,
            file_count=file_count,
            archive_size=archive_size,
            error=error,
        )
    except Exception as e:
        return UploadResult(
            batch_id=batch_id,
            success=False,
            duration=time.time() - start_time,
            file_count=file_count,
            archive_size=archive_size,
            error=str(e),
        )


# =============================================================================
# Main Upload Function
# =============================================================================


def upload_dicom_parallel_rest(
    *,
    base_url: str,
    username: str | None,
    password: str | None,
    session_token: str | None = None,
    verify_ssl: bool,
    source_dir: Path,
    project: str,
    subject: str,
    session: str,
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

    This function:
    1. Collects DICOM files from the source directory
    2. Splits files into N batches (N = upload_workers)
    3. Creates archives in parallel
    4. Uploads archives in parallel

    Args:
        base_url: XNAT server URL.
        username: XNAT username.
        password: XNAT password.
        session_token: Optional cached session token to reuse.
        verify_ssl: Whether to verify SSL certificates.
        source_dir: Directory containing DICOM files.
        project: Target project ID.
        subject: Target subject label.
        session: Target session label.
        upload_workers: Parallel upload workers (default: 4).
        archive_workers: Parallel archive workers (default: 4).
        archive_format: Archive format, "tar" or "zip" (default: tar).
        import_handler: XNAT import handler (default: DICOM-zip).
        ignore_unparsable: Skip unparsable DICOM files (default: True).
        overwrite: Overwrite mode: none, append, delete (default: delete).
        direct_archive: Use direct archive vs prearchive (default: True).
        timeout: HTTP timeout in seconds (default: 28800 = 8 hours).
        progress_callback: Optional callback for progress updates.

    Returns:
        UploadSummary with results.
    """
    total_start = time.time()
    errors: list[str] = []

    def report(progress: UploadProgress) -> None:
        """Invoke the progress callback if provided."""
        if progress_callback:
            progress_callback(progress)

    # Phase 1: Collect files
    report(UploadProgress(phase="preparing", message="Scanning for DICOM files..."))

    try:
        files = collect_dicom_files(source_dir)
    except Exception as e:
        return UploadSummary(
            success=False,
            total_files=0,
            total_size_mb=0.0,
            duration=time.time() - total_start,
            batches_succeeded=0,
            batches_failed=0,
            errors=[f"Failed to scan directory: {e}"],
        )

    if not files:
        return UploadSummary(
            success=False,
            total_files=0,
            total_size_mb=0.0,
            duration=time.time() - total_start,
            batches_succeeded=0,
            batches_failed=0,
            errors=["No DICOM files found"],
        )

    # Phase 2: Split into batches (one batch per upload worker)
    batch_count = max(1, min(upload_workers, len(files)))
    batches = split_into_n_batches(files, batch_count)
    report(
        UploadProgress(
            phase="preparing",
            message=f"Split {len(files)} files into {len(batches)} batches",
        )
    )

    # Phase 3: Create archives
    ext = ".tar" if archive_format == "tar" else ".zip"
    temp_dir = Path(tempfile.mkdtemp(prefix="xnatctl_upload_"))
    archive_paths: list[Path] = []
    total_archive_size = 0

    try:
        # Prepare archive paths
        for i in range(len(batches)):
            archive_paths.append(temp_dir / f"batch_{i + 1}{ext}")

        report(
            UploadProgress(
                phase="archiving",
                total=len(batches),
                message="Creating archives...",
            )
        )

        # Create archives in parallel
        archive_workers = max(1, min(archive_workers, len(batches)))
        source_path = source_dir.expanduser().resolve()

        with ThreadPoolExecutor(max_workers=archive_workers) as archive_executor:
            archive_futures: dict[Future[int], int] = {}
            for i, batch in enumerate(batches):
                archive_future = archive_executor.submit(
                    create_archive,
                    batch,
                    archive_paths[i],
                    source_path,
                    archive_format,
                )
                archive_futures[archive_future] = i

            completed = 0
            for archive_future in as_completed(archive_futures):
                completed += 1
                try:
                    size = archive_future.result()
                    total_archive_size += size
                except Exception as e:
                    idx = archive_futures[archive_future]
                    errors.append(f"Archive batch {idx + 1} failed: {e}")

                report(
                    UploadProgress(
                        phase="archiving",
                        current=completed,
                        total=len(batches),
                        message=f"Created archive {completed}/{len(batches)}",
                    )
                )

        if errors:
            # Archive creation failed
            return UploadSummary(
                success=False,
                total_files=len(files),
                total_size_mb=total_archive_size / 1024 / 1024,
                duration=time.time() - total_start,
                batches_succeeded=0,
                batches_failed=len(batches),
                errors=errors,
            )

        # Phase 4: Upload archives
        report(
            UploadProgress(
                phase="uploading",
                total=len(batches),
                message="Starting uploads...",
            )
        )

        results: list[UploadResult] = []
        upload_workers = max(1, min(upload_workers, len(batches)))

        with ThreadPoolExecutor(max_workers=upload_workers) as upload_executor:
            upload_futures: dict[Future[UploadResult], int] = {}
            for i, (batch, archive_path) in enumerate(zip(batches, archive_paths, strict=True)):
                upload_future = upload_executor.submit(
                    _upload_batch,
                    base_url=base_url,
                    username=username,
                    password=password,
                    session_token=session_token,
                    verify_ssl=verify_ssl,
                    timeout=timeout,
                    batch_id=i + 1,
                    archive_path=archive_path,
                    file_count=len(batch),
                    project=project,
                    subject=subject,
                    session=session,
                    import_handler=import_handler,
                    ignore_unparsable=ignore_unparsable,
                    overwrite=overwrite,
                    direct_archive=direct_archive,
                )
                upload_futures[upload_future] = i + 1

            for upload_future in as_completed(upload_futures):
                result = upload_future.result()
                results.append(result)

                if not result.success:
                    errors.append(f"Batch {result.batch_id}: {result.error}")

                succeeded = sum(1 for r in results if r.success)
                report(
                    UploadProgress(
                        phase="uploading",
                        current=len(results),
                        total=len(batches),
                        batch_id=result.batch_id,
                        success=result.success,
                        message=f"Uploaded {len(results)}/{len(batches)} ({succeeded} succeeded)",
                    )
                )

        # Phase 5: Complete
        total_duration = time.time() - total_start
        batches_succeeded = sum(1 for r in results if r.success)
        batches_failed = len(results) - batches_succeeded
        success = batches_failed == 0

        report(
            UploadProgress(
                phase="complete" if success else "error",
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
        )

        if not success:
            logger.warning("Upload completed with %s failures", batches_failed)

        return UploadSummary(
            success=success,
            total_files=len(files),
            total_size_mb=total_archive_size / 1024 / 1024,
            duration=total_duration,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            errors=errors,
        )

    finally:
        # Cleanup temp archives
        shutil.rmtree(temp_dir, ignore_errors=True)
