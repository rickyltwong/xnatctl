"""Scan service for XNAT scan operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from xnatctl.models.scan import Scan
from xnatctl.core.exceptions import ResourceNotFoundError

from .base import BaseService


class ScanService(BaseService):
    """Service for XNAT scan operations."""

    def list(
        self,
        session_id: str,
        project: Optional[str] = None,
        columns: Optional[list[str]] = None,
    ) -> list[Scan]:
        """List scans in a session.

        Args:
            session_id: Session ID
            project: Project ID (optional)
            columns: Specific columns to retrieve

        Returns:
            List of Scan objects
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans"
        else:
            path = f"/data/experiments/{session_id}/scans"

        params: dict[str, Any] = {"format": "json"}
        if columns:
            params["columns"] = ",".join(columns)

        data = self._get(path, params=params)
        results = self._extract_results(data)

        scans = []
        for r in results:
            r["session_id"] = session_id
            if project:
                r["project"] = project
            scans.append(Scan(**r))

        return scans

    def get(
        self,
        session_id: str,
        scan_id: str,
        project: Optional[str] = None,
    ) -> Scan:
        """Get scan details.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            project: Project ID (optional)

        Returns:
            Scan object

        Raises:
            ResourceNotFoundError: If scan not found
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}"
        else:
            path = f"/data/experiments/{session_id}/scans/{scan_id}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
            results = self._extract_results(data)
            if results:
                results[0]["session_id"] = session_id
                if project:
                    results[0]["project"] = project
                return Scan(**results[0])
            raise ResourceNotFoundError("scan", f"{session_id}/{scan_id}")
        except Exception as e:
            if "404" in str(e):
                raise ResourceNotFoundError("scan", f"{session_id}/{scan_id}")
            raise

    def delete(
        self,
        session_id: str,
        scan_id: str,
        project: Optional[str] = None,
        remove_files: bool = False,
    ) -> bool:
        """Delete a scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            project: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}"
        else:
            path = f"/data/experiments/{session_id}/scans/{scan_id}"

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def delete_multiple(
        self,
        session_id: str,
        scan_ids: list[str],
        project: Optional[str] = None,
        remove_files: bool = False,
        parallel: bool = True,
        workers: int = 4,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        """Delete multiple scans.

        Args:
            session_id: Session ID
            scan_ids: List of scan IDs to delete ("*" for all)
            project: Project ID
            remove_files: Also remove files
            parallel: Use parallel deletion
            workers: Number of parallel workers
            progress_callback: Callback(current, total, scan_id)

        Returns:
            Summary dict with deleted, failed, errors
        """
        # Handle wildcard
        if scan_ids == ["*"] or "*" in scan_ids:
            scans = self.list(session_id, project=project)
            scan_ids = [s.id for s in scans]

        results = {
            "deleted": [],
            "failed": [],
            "errors": [],
            "total": len(scan_ids),
        }

        def delete_scan(scan_id: str) -> tuple[str, bool, str]:
            """Delete a single scan and return status."""
            try:
                self.delete(session_id, scan_id, project=project, remove_files=remove_files)
                return (scan_id, True, "")
            except Exception as e:
                return (scan_id, False, str(e))

        if parallel and len(scan_ids) > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(delete_scan, scan_id): scan_id
                    for scan_id in scan_ids
                }

                for i, future in enumerate(as_completed(futures)):
                    scan_id, success, error = future.result()
                    if success:
                        results["deleted"].append(scan_id)
                    else:
                        results["failed"].append(scan_id)
                        results["errors"].append({"scan": scan_id, "error": error})

                    if progress_callback:
                        progress_callback(i + 1, len(scan_ids), scan_id)
        else:
            for i, scan_id in enumerate(scan_ids):
                scan_id, success, error = delete_scan(scan_id)
                if success:
                    results["deleted"].append(scan_id)
                else:
                    results["failed"].append(scan_id)
                    results["errors"].append({"scan": scan_id, "error": error})

                if progress_callback:
                    progress_callback(i + 1, len(scan_ids), scan_id)

        return results

    def get_resources(
        self,
        session_id: str,
        scan_id: str,
        project: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get resources for a scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            project: Project ID

        Returns:
            List of resource data dicts
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources"
        else:
            path = f"/data/experiments/{session_id}/scans/{scan_id}/resources"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def set_quality(
        self,
        session_id: str,
        scan_id: str,
        quality: str,
        project: Optional[str] = None,
    ) -> bool:
        """Set scan quality assessment.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            quality: Quality value (usable, questionable, unusable)
            project: Project ID

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}"
        else:
            path = f"/data/experiments/{session_id}/scans/{scan_id}"

        params = {"xnat:imageScanData/quality": quality}
        self._put(path, params=params)
        return True

    def set_note(
        self,
        session_id: str,
        scan_id: str,
        note: str,
        project: Optional[str] = None,
    ) -> bool:
        """Set scan note.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            note: Note text
            project: Project ID

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}"
        else:
            path = f"/data/experiments/{session_id}/scans/{scan_id}"

        params = {"xnat:imageScanData/note": note}
        self._put(path, params=params)
        return True
