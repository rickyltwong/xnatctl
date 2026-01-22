"""Download service for XNAT download operations."""

from __future__ import annotations

import hashlib
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xnatctl.models.progress import (
    DownloadProgress,
    DownloadSummary,
    OperationPhase,
)

from .base import BaseService


class DownloadService(BaseService):
    """Service for XNAT download operations."""

    def download_session(
        self,
        session_id: str,
        output_dir: Path,
        project: str | None = None,
        include_resources: bool = True,
        include_assessors: bool = False,
        pattern: str | None = None,
        resume: bool = False,
        verify: bool = False,
        parallel: bool = True,
        workers: int = 4,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download session data.

        Args:
            session_id: Session ID
            output_dir: Output directory path
            project: Project ID
            include_resources: Include session-level resources
            include_assessors: Include assessor data
            pattern: File pattern filter
            resume: Resume interrupted download
            verify: Verify checksums after download
            parallel: Use parallel downloads
            workers: Number of parallel workers
            progress_callback: Progress callback function

        Returns:
            DownloadSummary with results
        """
        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build download URL
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/ALL/files"
        else:
            path = f"/data/experiments/{session_id}/scans/ALL/files"

        params: dict[str, Any] = {"format": "zip"}
        if pattern:
            params["file_format"] = pattern

        # Download ZIP
        if progress_callback:
            progress_callback(
                DownloadProgress(
                    phase=OperationPhase.PREPARING,
                    message=f"Preparing download for {session_id}",
                )
            )

        zip_path = output_dir / f"{session_id}.zip"

        try:
            # Stream download
            total_bytes = 0
            client = self.client._get_client()
            with client.stream("GET", path, params=params) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("content-length", 0))

                with open(zip_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        total_bytes += len(chunk)

                        if progress_callback:
                            progress_callback(
                                DownloadProgress(
                                    phase=OperationPhase.DOWNLOADING,
                                    bytes_received=total_bytes,
                                    total_bytes=total_size,
                                    file_path=str(zip_path),
                                    message=f"Downloading {session_id}",
                                )
                            )

            # Extract if needed
            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        phase=OperationPhase.PROCESSING,
                        message=f"Extracting {session_id}",
                    )
                )

            extract_dir = output_dir / session_id
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            # Count files
            file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())

            # Clean up ZIP
            zip_path.unlink()

            # Verify if requested
            verified = False
            if verify:
                verified = self._verify_download(session_id, extract_dir, project)

            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        phase=OperationPhase.COMPLETE,
                        message=f"Download complete: {file_count} files",
                        success=True,
                    )
                )

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=1,
                succeeded=1,
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=str(extract_dir),
                session_id=session_id,
                verified=verified,
            )

        except Exception as e:
            if progress_callback:
                progress_callback(
                    DownloadProgress(
                        phase=OperationPhase.ERROR,
                        message=str(e),
                        success=False,
                        errors=[str(e)],
                    )
                )

            duration = time.time() - start_time
            return DownloadSummary(
                success=False,
                total=1,
                succeeded=0,
                failed=1,
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )

    def download_resource(
        self,
        session_id: str,
        resource_label: str,
        output_dir: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = True,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            output_dir: Output directory
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP files
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results
        """
        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build path
        if scan_id:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
        else:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}/files"
            else:
                path = f"/data/experiments/{session_id}/resources/{resource_label}/files"

        params = {"format": "zip"}

        zip_path = output_dir / f"{resource_label}.zip"

        try:
            total_bytes = 0
            client = self.client._get_client()
            with client.stream("GET", path, params=params) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("content-length", 0))

                with open(zip_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        total_bytes += len(chunk)

                        if progress_callback:
                            progress_callback(
                                DownloadProgress(
                                    phase=OperationPhase.DOWNLOADING,
                                    bytes_received=total_bytes,
                                    total_bytes=total_size,
                                    file_path=str(zip_path),
                                )
                            )

            file_count = 1
            if extract:
                extract_dir = output_dir / resource_label
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                zip_path.unlink()

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=1,
                succeeded=1,
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=str(output_dir),
                session_id=session_id,
            )

        except Exception as e:
            duration = time.time() - start_time
            return DownloadSummary(
                success=False,
                total=1,
                succeeded=0,
                failed=1,
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )

    def download_scan(
        self,
        session_id: str,
        scan_id: str,
        output_dir: Path,
        project: str | None = None,
        resource: str = "DICOM",
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            output_dir: Output directory
            project: Project ID
            resource: Resource type to download (DICOM, NIFTI, etc)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results
        """
        return self.download_resource(
            session_id=session_id,
            resource_label=resource,
            output_dir=output_dir,
            scan_id=scan_id,
            project=project,
            progress_callback=progress_callback,
        )

    def _verify_download(
        self,
        session_id: str,
        download_dir: Path,
        project: str | None = None,
    ) -> bool:
        """Verify downloaded files against server checksums.

        Args:
            session_id: Session ID
            download_dir: Directory with downloaded files
            project: Project ID

        Returns:
            True if all checksums match
        """
        # Get file list with checksums from server
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/files"
        else:
            path = f"/data/experiments/{session_id}/files"

        params = {"format": "json"}
        data = self._get(path, params=params)
        results = self._extract_results(data)

        # Build checksum map
        server_checksums: dict[str, str] = {}
        for r in results:
            name = r.get("Name", "")
            digest = r.get("digest", "")
            if name and digest:
                server_checksums[name] = digest

        # Verify local files
        all_valid = True
        for file_path in download_dir.rglob("*"):
            if not file_path.is_file():
                continue

            name = file_path.name
            if name in server_checksums:
                local_hash = hashlib.md5(file_path.read_bytes()).hexdigest()
                if local_hash != server_checksums[name]:
                    all_valid = False

        return all_valid
