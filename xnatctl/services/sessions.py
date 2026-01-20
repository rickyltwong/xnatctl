"""Session/Experiment service for XNAT session operations."""

from __future__ import annotations

from typing import Any, Optional

from xnatctl.models.session import Session
from xnatctl.core.exceptions import ResourceNotFoundError

from .base import BaseService


class SessionService(BaseService):
    """Service for XNAT session/experiment operations."""

    def list(
        self,
        project: Optional[str] = None,
        subject: Optional[str] = None,
        modality: Optional[str] = None,
        limit: Optional[int] = None,
        columns: Optional[list[str]] = None,
    ) -> list[Session]:
        """List sessions/experiments.

        Args:
            project: Filter by project ID
            subject: Filter by subject ID
            modality: Filter by modality (MR, PET, CT)
            limit: Maximum number of results
            columns: Specific columns to retrieve

        Returns:
            List of Session objects
        """
        if project and subject:
            path = f"/data/projects/{project}/subjects/{subject}/experiments"
        elif project:
            path = f"/data/projects/{project}/experiments"
        else:
            path = "/data/experiments"

        params: dict[str, Any] = {"format": "json"}
        if columns:
            params["columns"] = ",".join(columns)
        if modality:
            params["xsiType"] = f"xnat:{modality.lower()}SessionData"

        data = self._get(path, params=params)
        results = self._extract_results(data)

        if limit:
            results = results[:limit]

        return [Session(**r) for r in results]

    def get(
        self,
        session_id: str,
        project: Optional[str] = None,
    ) -> Session:
        """Get session details.

        Args:
            session_id: Session ID or label
            project: Project ID (helps with label lookup)

        Returns:
            Session object

        Raises:
            ResourceNotFoundError: If session not found
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}"
        else:
            path = f"/data/experiments/{session_id}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
            results = self._extract_results(data)
            if results:
                return Session(**results[0])
            raise ResourceNotFoundError("session", session_id)
        except Exception as e:
            if "404" in str(e):
                raise ResourceNotFoundError("session", session_id)
            raise

    def create(
        self,
        project: str,
        subject: str,
        label: str,
        xsi_type: str = "xnat:mrSessionData",
        date: Optional[str] = None,
        time: Optional[str] = None,
        visit_id: Optional[str] = None,
        modality: Optional[str] = None,
    ) -> Session:
        """Create a new session/experiment.

        Args:
            project: Project ID
            subject: Subject ID or label
            label: Session label
            xsi_type: XSI type (xnat:mrSessionData, xnat:petSessionData, etc)
            date: Session date (YYYY-MM-DD)
            time: Session time (HH:MM:SS)
            visit_id: Visit identifier
            modality: Modality (overrides xsi_type if provided)

        Returns:
            Created Session object
        """
        path = f"/data/projects/{project}/subjects/{subject}/experiments/{label}"
        params: dict[str, Any] = {}

        # Determine xsi_type from modality if provided
        if modality:
            modality_map = {
                "MR": "xnat:mrSessionData",
                "PET": "xnat:petSessionData",
                "CT": "xnat:ctSessionData",
            }
            xsi_type = modality_map.get(modality.upper(), xsi_type)

        params["xsiType"] = xsi_type
        if date:
            params["date"] = date
        if time:
            params["time"] = time
        if visit_id:
            params["visit_id"] = visit_id

        self._put(path, params=params)
        return self.get(label, project=project)

    def delete(
        self,
        session_id: str,
        project: Optional[str] = None,
        remove_files: bool = False,
    ) -> bool:
        """Delete a session.

        Args:
            session_id: Session ID
            project: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}"
        else:
            path = f"/data/experiments/{session_id}"

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def get_scans(
        self,
        session_id: str,
        project: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get scans for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of scan data dicts
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/scans"
        else:
            path = f"/data/experiments/{session_id}/scans"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def get_resources(
        self,
        session_id: str,
        project: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get resources for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of resource data dicts
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}/resources"
        else:
            path = f"/data/experiments/{session_id}/resources"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def set_field(
        self,
        session_id: str,
        field: str,
        value: str,
        project: Optional[str] = None,
    ) -> bool:
        """Set a field value on a session.

        Args:
            session_id: Session ID
            field: Field name (e.g., 'note', 'acquisition_site')
            value: Field value
            project: Project ID

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/experiments/{session_id}"
        else:
            path = f"/data/experiments/{session_id}"

        params = {field: value}
        self._put(path, params=params)
        return True

    def share(
        self,
        session_id: str,
        target_project: str,
        label: Optional[str] = None,
        primary: bool = False,
    ) -> bool:
        """Share a session with another project.

        Args:
            session_id: Session ID
            target_project: Target project ID
            label: New label in target project
            primary: Make target the primary project

        Returns:
            True if successful
        """
        path = f"/data/experiments/{session_id}/projects/{target_project}"
        params: dict[str, Any] = {}

        if label:
            params["label"] = label
        if primary:
            params["primary"] = "true"

        self._put(path, params=params)
        return True
