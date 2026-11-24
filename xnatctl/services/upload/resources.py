"""Resource file upload transport.

Uploads a single file or a zipped directory to a session or scan resource
via a plain HTTP PUT to the import service, independent of the DICOM
transports.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import UploadError, XNATCtlError
from xnatctl.core.retry import upload_with_retry
from xnatctl.core.timeouts import build_httpx_timeout
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.progress import OperationPhase, UploadProgress, UploadSummary

logger = logging.getLogger(__name__)


def _notify_upload_error(
    progress_callback: Callable[[UploadProgress], None] | None,
    exc: Exception,
) -> None:
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


def _build_resource_upload_path(
    session_id: str,
    resource_label: str,
    scan_id: str | None,
    project: str | None,
) -> str:
    q_session_id = quote_path_segment(session_id)
    q_resource_label = quote_path_segment(resource_label)
    if scan_id is not None:
        q_scan_id = quote_path_segment(scan_id)
        if project is not None:
            return (
                f"/data/projects/{quote_path_segment(project)}/experiments/{q_session_id}"
                f"/scans/{q_scan_id}/resources/{q_resource_label}/files"
            )
        return (
            f"/data/experiments/{q_session_id}/scans/{q_scan_id}/resources/{q_resource_label}/files"
        )
    if project is not None:
        return (
            f"/data/projects/{quote_path_segment(project)}/experiments/{q_session_id}"
            f"/resources/{q_resource_label}/files"
        )
    return f"/data/experiments/{q_session_id}/resources/{q_resource_label}/files"


def _zip_directory_source(source_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    try:
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", source_path)
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise
    return zip_path


def _do_upload_request(
    client: XNATClient,
    source_path: Path,
    path: str,
    params: dict[str, Any],
) -> None:
    base_url = client.base_url
    session_token = client.session_token
    verify_ssl = client.httpx_verify()
    res_timeout = client.timeout
    cookies = {"JSESSIONID": session_token} if session_token else {}

    with httpx.Client(
        base_url=base_url,
        timeout=build_httpx_timeout(res_timeout),
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


def upload_resource(
    client: XNATClient,
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
        client: Bound XNAT client.
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

        base_path = _build_resource_upload_path(session_id, resource_label, scan_id, project)

        if source_path.is_dir():
            temp_zip = _zip_directory_source(source_path)
            source_path = temp_zip
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

        path = f"{base_path}/{quote_path_segment(source_path.name)}"

        _do_upload_request(client, source_path, path, params)

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
        _notify_upload_error(progress_callback, e)
        raise
    except Exception as e:
        _notify_upload_error(progress_callback, e)
        raise UploadError(str(e), file_path=str(upload_source)) from e
    finally:
        if temp_zip is not None:
            temp_zip.unlink(missing_ok=True)
