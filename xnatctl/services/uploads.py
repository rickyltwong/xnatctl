"""Upload service for XNAT upload operations."""

from __future__ import annotations

import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xnatctl.models.progress import (
    OperationPhase,
    UploadProgress,
    UploadSummary,
)

from .base import BaseService


class UploadService(BaseService):
    """Service for XNAT upload operations."""

    def upload_dicom(
        self,
        project: str,
        subject: str,
        session: str,
        source_path: Path,
        overwrite: bool = False,
        quarantine: bool = False,
        batch_size: int = 500,
        parallel: bool = True,
        workers: int = 4,
        progress_callback: Callable[[UploadProgress], None] | None = None,
    ) -> UploadSummary:
        """Upload DICOM files to create/update a session.

        Uses the XNAT REST import service with direct-archive.

        Args:
            project: Project ID
            subject: Subject label
            session: Session label
            source_path: Path to DICOM files (directory or ZIP)
            overwrite: Overwrite existing scans
            quarantine: Send to prearchive instead
            batch_size: Files per upload batch
            parallel: Use parallel uploads
            workers: Number of parallel workers
            progress_callback: Progress callback function

        Returns:
            UploadSummary with results
        """
        start_time = time.time()
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        # Report preparation phase
        if progress_callback:
            progress_callback(
                UploadProgress(
                    phase=OperationPhase.PREPARING,
                    message="Preparing upload",
                )
            )

        # Collect DICOM files
        dicom_files: list[Path] = []
        if source_path.is_file():
            if source_path.suffix.lower() == ".zip":
                # Extract ZIP to temp directory
                temp_dir = tempfile.mkdtemp()
                with zipfile.ZipFile(source_path, "r") as zf:
                    zf.extractall(temp_dir)
                source_path = Path(temp_dir)
                dicom_files = list(source_path.rglob("*"))
            else:
                dicom_files = [source_path]
        else:
            dicom_files = [
                f for f in source_path.rglob("*") if f.is_file() and not f.name.startswith(".")
            ]

        # Filter to likely DICOM files
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

        # Calculate total size
        total_size = sum(f.stat().st_size for f in dicom_files)

        if progress_callback:
            progress_callback(
                UploadProgress(
                    phase=OperationPhase.ARCHIVING,
                    total=total_files,
                    message=f"Found {total_files} files",
                )
            )

        # Split into batches
        batches = list(self._split_into_batches(dicom_files, batch_size))
        total_batches = len(batches)

        results: dict[str, Any] = {
            "succeeded": 0,
            "failed": 0,
            "errors": [],
        }

        # Build destination parameter
        dest = f"/archive/projects/{project}/subjects/{subject}/experiments/{session}"

        def upload_batch(batch_id: int, files: list[Path]) -> tuple[bool, str]:
            """Upload a single batch."""
            try:
                # Create temporary ZIP
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    zip_path = Path(tmp.name)

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for file_path in files:
                        zf.write(file_path, file_path.name)

                # Upload to import service
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

                with open(zip_path, "rb") as zip_file:
                    content = zip_file.read()

                self.client.post(
                    "/data/services/import",
                    params=params,
                    data=content,
                    headers={
                        "Content-Type": "application/zip",
                    },
                )

                zip_path.unlink()
                return (True, "")

            except Exception as e:
                return (False, str(e))

        # Upload batches
        if parallel and total_batches > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(upload_batch, i, batch): i for i, batch in enumerate(batches)
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
                success, error = upload_batch(batch_id, batch)

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
            session_id: Session ID
            resource_label: Resource label
            source_path: File or directory to upload
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP/TAR after upload
            overwrite: Overwrite existing files
            progress_callback: Progress callback

        Returns:
            UploadSummary with results
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

        # Build path
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

        # Handle directory by creating ZIP
        if source_path.is_dir():
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                zip_path = Path(tmp.name)

            shutil.make_archive(
                str(zip_path.with_suffix("")),
                "zip",
                source_path,
            )
            source_path = zip_path
            extract = True

        # Get file size
        file_size = source_path.stat().st_size

        if progress_callback:
            progress_callback(
                UploadProgress(
                    phase=OperationPhase.UPLOADING,
                    total_bytes=file_size,
                    message=f"Uploading {source_path.name}",
                )
            )

        # Upload
        params: dict[str, Any] = {}
        if extract:
            params["extract"] = "true"
        if overwrite:
            params["overwrite"] = "true"

        path = f"{base_path}/{source_path.name}"

        try:
            with open(source_path, "rb") as f:
                content = f.read()

            self.client.put(
                path,
                params=params,
                data=content,
                headers={"Content-Type": "application/octet-stream"},
            )

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
            files: List of file paths
            batch_size: Maximum files per batch

        Yields:
            Lists of files for each batch
        """
        for i in range(0, len(files), batch_size):
            yield files[i : i + batch_size]
