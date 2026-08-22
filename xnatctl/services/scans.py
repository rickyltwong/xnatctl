"""Scan service for XNAT scan operations."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from concurrent.futures import as_completed
from typing import Any

import httpx

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.hierarchy import ExperimentRef, ScanRef
from xnatctl.models.scan import Scan

from .base import BaseService
from .hierarchy import HierarchyService


def _scan_collection_path(session_id: str, project: str | None) -> str:
    """Build the scans listing path for a session."""
    return HierarchyService.build_scan_collection_path(
        ExperimentRef(experiment=session_id, project_id=project)
    )


def _scan_item_path(session_id: str, scan_id: str, project: str | None, *parts: str) -> str:
    """Build a scan item path (optionally with a nested suffix).

    Routed through ``HierarchyService`` rather than interpolated here, because
    ``/data/projects/{P}/experiments/{E}/scans/{id}`` does not address a scan:
    XNAT ignores the suffix and answers with the experiment document, so a
    listing came back empty and a DELETE resolved to the whole session. Without
    a subject segment the flat form is the only one that routes; access is
    enforced server-side either way. See ``routable_scan_parent``.
    """
    return HierarchyService.build_scan_path(
        ScanRef(
            experiment=ExperimentRef(experiment=session_id, project_id=project),
            scan_id=scan_id,
        ),
        *parts,
    )


class ScanService(BaseService):
    """Service for XNAT scan operations."""

    def list(
        self,
        session_id: str,
        project: str | None = None,
        columns: builtins.list[str] | None = None,
    ) -> builtins.list[Scan]:
        """List scans in a session.

        Args:
            session_id: Session ID
            project: Project ID (optional)
            columns: Specific columns to retrieve

        Returns:
            List of Scan objects
        """
        path = _scan_collection_path(session_id, project)

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
        project: str | None = None,
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
        path = _scan_item_path(session_id, scan_id, project)

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("scan", f"{session_id}/{scan_id}") from e

        results = self._extract_results(data)
        if results:
            results[0]["session_id"] = session_id
            if project:
                results[0]["project"] = project
            return Scan(**results[0])
        raise ResourceNotFoundError("scan", f"{session_id}/{scan_id}")

    def delete(
        self,
        session_id: str,
        scan_id: str,
        project: str | None = None,
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
        path = _scan_item_path(session_id, scan_id, project)

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def delete_multiple(
        self,
        session_id: str,
        scan_ids: builtins.list[str],
        project: str | None = None,
        remove_files: bool = False,
        parallel: bool = True,
        workers: int = 4,
        progress_callback: Callable[[int, int, str], None] | None = None,
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

        results: dict[str, Any] = {
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
            with cancellable_pool(workers) as (executor, _token):
                futures = {executor.submit(delete_scan, scan_id): scan_id for scan_id in scan_ids}

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
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get resources for a scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID
            project: Project ID

        Returns:
            List of resource data dicts
        """
        path = _scan_item_path(session_id, scan_id, project, "resources")

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def set_quality(
        self,
        session_id: str,
        scan_id: str,
        quality: str,
        project: str | None = None,
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
        path = _scan_item_path(session_id, scan_id, project)

        params = {"xnat:imageScanData/quality": quality}
        self._put(path, params=params)
        return True

    def set_note(
        self,
        session_id: str,
        scan_id: str,
        note: str,
        project: str | None = None,
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
        path = _scan_item_path(session_id, scan_id, project)

        params = {"xnat:imageScanData/note": note}
        self._put(path, params=params)
        return True

    # -------------------------------------------------------------------------
    # Ref-based accessors
    #
    # The CLI resolves an experiment to a canonical, routable ``ScanRef``
    # before addressing a scan -- XNAT silently answers a mis-routed scan URL
    # with the parent experiment document, so the route must be one XNAT
    # actually dispatches. These operate on that already-resolved ref so the
    # wire path matches what inspection produced, rather than re-deriving it
    # from ``(session_id, project)``.
    # -------------------------------------------------------------------------

    def get_scan_document(self, scan_ref: ScanRef) -> dict[str, Any] | None:
        """Return a scan's raw detail row, or ``None`` if not found."""
        data = self.client.get_json(HierarchyService.build_scan_path(scan_ref))
        item = HierarchyService.extract_first_item(data)
        if item is not None:
            return item[0]
        rows = HierarchyService.extract_rows(data)
        return rows[0] if rows else None

    def delete_scan_ref(self, scan_ref: ScanRef) -> httpx.Response:
        """DELETE a scan addressed by ref and return the raw response."""
        return self.client.delete(HierarchyService.build_scan_path(scan_ref))
