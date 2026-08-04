"""Download service for XNAT download operations."""

from __future__ import annotations

import hashlib
import shutil
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xnatctl.core.exceptions import AuthenticationError
from xnatctl.models.hierarchy import ExperimentRef, ResourceRef, ScanRef
from xnatctl.models.progress import (
    DownloadProgress,
    DownloadSummary,
    OperationPhase,
)

from .base import BaseService
from .hierarchy import HierarchyService


def _md5_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute MD5 checksum of a file without reading it entirely into memory."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """Extract ZIP contents safely, guarding against path traversal."""
    resolved_root = extract_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (extract_dir / member.filename).resolve()
            if not target.is_relative_to(resolved_root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


class DownloadService(BaseService):
    """Service for XNAT download operations."""

    def _resolve_zip_experiment_ref(
        self,
        session_id: str,
        *,
        project: str | None = None,
        subject: str | None = None,
    ) -> ExperimentRef:
        """Resolve label-based experiment references to a canonical experiment ID."""

        if project and not session_id.startswith("XNAT_E"):
            source_ref = ExperimentRef(
                experiment=session_id,
                project_id=project,
                subject=subject,
                experiment_is_label=True,
                subject_is_label=subject is not None,
            )
            resolved = HierarchyService.parse_resolved_experiment(
                source_ref,
                self._get(
                    HierarchyService.build_experiment_path(source_ref),
                    params={"format": "json"},
                ),
            )
            return ExperimentRef(experiment=resolved.experiment_id)

        return ExperimentRef(experiment=session_id)

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

        TODO: currently has no CLI caller -- ``session download`` runs
        the inline fast path in ``cli/session.py``. A planned refactor folds that engine
        into this method (routed through the client for retry/auth) rather than
        deleting it; kept intentionally per the M1 dead-code carve-out.

        Args:
            session_id: Session ID
            output_dir: Output directory path
            project: Kept for call compatibility; not used to build the URL,
                because the project-scoped file listing does not route.
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

        # Build download URL. The flat form is the only one that routes: XNAT
        # answers /data/projects/{P}/experiments/{E}/scans/ALL/files with the
        # experiment document instead of the file listing, so the project-scoped
        # variant downloaded a ZIP of nothing.
        path = HierarchyService.build_scan_path(
            ScanRef(experiment=ExperimentRef(experiment=session_id), scan_id="ALL"),
            "files",
        )

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
            cookies = self.client._get_cookies()
            with client.stream("GET", path, params=params, cookies=cookies) as response:
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
            _safe_extract_zip(zip_path, extract_dir)

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
        extract: bool = False,
        zip_filename: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            output_dir: Output directory
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP files (default: False)
            zip_filename: Custom ZIP filename (default: {resource_label}.zip)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results
        """
        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            resolved_experiment_ref = self._resolve_zip_experiment_ref(
                session_id,
                project=project,
            )
        except AuthenticationError:
            raise
        except Exception as e:
            if "not found" in str(e).lower() or isinstance(e, ValueError):
                raise
            resolved_experiment_ref = ExperimentRef(experiment=session_id)

        # Build path - always use /data/experiments/{id}/... for reliable ZIP downloads
        if scan_id:
            path = HierarchyService.build_resource_path(
                ResourceRef(
                    parent=ScanRef(experiment=resolved_experiment_ref, scan_id=scan_id),
                    resource_label=resource_label,
                ),
                "files",
            )
        else:
            path = HierarchyService.build_resource_path(
                ResourceRef(parent=resolved_experiment_ref, resource_label=resource_label),
                "files",
            )

        params = {"format": "zip"}

        zip_path = output_dir / (zip_filename or f"{resource_label}.zip")

        try:
            total_bytes = 0
            client = self.client._get_client()
            cookies = self.client._get_cookies()
            with client.stream("GET", path, params=params, cookies=cookies) as response:
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
                _safe_extract_zip(zip_path, extract_dir)
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
        resource: str | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download a specific scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            output_dir: Output directory
            project: Project ID
            resource: Resource type to download (None = all resources)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results
        """
        if resource is None:
            return self.download_scans(
                session_id=session_id,
                scan_ids=[scan_id],
                output_dir=output_dir,
                project=project,
                resource=None,
                progress_callback=progress_callback,
            )
        return self.download_resource(
            session_id=session_id,
            resource_label=resource,
            output_dir=output_dir,
            scan_id=scan_id,
            project=project,
            progress_callback=progress_callback,
        )

    def download_scans(
        self,
        session_id: str,
        scan_ids: list[str],
        output_dir: Path,
        project: str | None = None,
        subject: str | None = None,
        resource: str | None = None,
        zip_filename: str | None = None,
        extract: bool = False,
        cleanup: bool = True,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadSummary:
        """Download multiple scans in a single request.

        Uses XNAT's comma-separated scan ID feature for efficient batch downloads.
        When resource is None, downloads ALL files (DICOM + SNAPSHOTS).

        Args:
            session_id: Session ID or label
            scan_ids: List of scan IDs (or ["ALL"] for all scans)
            output_dir: Output directory
            project: Project ID (required when using session label)
            subject: Subject ID/label (optional, narrows experiment lookup)
            resource: Resource type (None = all resources, "DICOM" = DICOM only)
            zip_filename: Output ZIP filename (default: scans.zip)
            extract: Extract ZIP after download
            cleanup: Remove ZIP after successful extraction (with extract=True)
            progress_callback: Progress callback

        Returns:
            DownloadSummary with results
        """
        start_time = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            resolved_experiment_ref = self._resolve_zip_experiment_ref(
                session_id,
                project=project,
                subject=subject,
            )
        except AuthenticationError:
            raise
        except Exception as e:
            if "not found" in str(e).lower() or isinstance(e, ValueError):
                raise
            resolved_experiment_ref = ExperimentRef(experiment=session_id)

        scan_spec = ",".join(scan_ids) if len(scan_ids) > 1 else scan_ids[0]

        if resource:
            path = HierarchyService.build_resource_path(
                ResourceRef(
                    parent=ScanRef(experiment=resolved_experiment_ref, scan_id=scan_spec),
                    resource_label=resource,
                ),
                "files",
            )
        else:
            path = HierarchyService.build_scan_path(
                ScanRef(experiment=resolved_experiment_ref, scan_id=scan_spec),
                "files",
            )

        params = {"format": "zip"}
        zip_path = output_dir / (zip_filename or "scans.zip")

        try:
            total_bytes = 0
            client = self.client._get_client()
            cookies = self.client._get_cookies()
            with client.stream("GET", path, params=params, cookies=cookies) as response:
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
            output_path = str(zip_path)
            if extract:
                extract_dir = output_dir / "scans"
                _safe_extract_zip(zip_path, extract_dir)
                file_count = sum(1 for _ in extract_dir.rglob("*") if _.is_file())
                if cleanup:
                    zip_path.unlink()
                output_path = str(extract_dir)

            duration = time.time() - start_time
            return DownloadSummary(
                success=True,
                total=len(scan_ids),
                succeeded=len(scan_ids),
                failed=0,
                duration=duration,
                total_files=file_count,
                total_size_mb=total_bytes / (1024 * 1024),
                output_path=output_path,
                session_id=session_id,
            )

        except Exception as e:
            duration = time.time() - start_time
            return DownloadSummary(
                success=False,
                total=len(scan_ids),
                succeeded=0,
                failed=len(scan_ids),
                duration=duration,
                errors=[str(e)],
                session_id=session_id,
            )

    def _verify_download(
        self,
        session_id: str,
        download_dir: Path,
        project: str | None = None,
    ) -> bool:
        """Verify downloaded files against server checksums.

        Both file listings are consulted. ``/files`` returns only the
        session-level resources, while the scan files this method is usually
        asked to verify live under ``/scans/ALL/files`` -- checking just the
        former meant nothing downloaded was ever compared, and verification
        reported success without doing any (verified live: a session whose
        ``/files`` listed 12 resource files and whose ``/scans/ALL/files``
        listed 3112).

        Names are matched on basename against the *set* of digests recorded
        for that name. XNAT sites that number DICOM files per scan repeat
        names like ``00001.dcm`` in every scan, so a single-digest map would
        report a byte-perfect download as corrupt.

        Args:
            session_id: Session ID (accession ID; a label cannot be listed here)
            download_dir: Directory with downloaded files
            project: Accepted for call compatibility but deliberately not used
                to build the listing URL -- see below.

        Returns:
            True if every file that could be checked matched, and at least one
            file was checked. Verifying nothing is not a pass.
        """
        # Always the flat form. XNAT does not route file listings under
        # /data/projects/{P}/experiments/{E}: both /files and /scans/ALL/files
        # return 200 with the experiment document there (verified live -- the
        # flat URLs returned 12 and 3112 rows, the project-scoped ones a single
        # items[] record). Building the project-scoped URL would yield zero
        # checksums and report a byte-perfect download as unverifiable.
        base = f"/data/experiments/{session_id}"

        server_checksums: dict[str, set[str]] = {}
        for path in (f"{base}/scans/ALL/files", f"{base}/files"):
            try:
                results = self._extract_results(self._get(path, params={"format": "json"}))
            except Exception:
                # A session with no scans 404s on the scans listing; the other
                # listing may still cover what was downloaded.
                continue
            for r in results:
                name = r.get("Name", "")
                digest = r.get("digest", "")
                if name and digest:
                    server_checksums.setdefault(name, set()).add(digest)

        if not server_checksums:
            return False

        checked = 0
        all_valid = True
        for file_path in download_dir.rglob("*"):
            if not file_path.is_file():
                continue

            digests = server_checksums.get(file_path.name)
            if not digests:
                continue

            checked += 1
            if _md5_file(file_path) not in digests:
                all_valid = False

        return all_valid and checked > 0
