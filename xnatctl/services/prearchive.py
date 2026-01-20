"""Prearchive service for XNAT prearchive operations."""

from __future__ import annotations

from typing import Any, Optional

from xnatctl.core.exceptions import ResourceNotFoundError, OperationError

from .base import BaseService


class PrearchiveService(BaseService):
    """Service for XNAT prearchive operations."""

    def list(
        self,
        project: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List prearchive sessions.

        Args:
            project: Filter by project ID

        Returns:
            List of prearchive session dicts
        """
        if project:
            path = f"/data/prearchive/projects/{project}"
        else:
            path = "/data/prearchive"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def get(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> dict[str, Any]:
        """Get prearchive session details.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            Prearchive session dict

        Raises:
            ResourceNotFoundError: If session not found
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}"
        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
            results = self._extract_results(data)
            if results:
                return results[0]
            raise ResourceNotFoundError("prearchive session", f"{project}/{timestamp}/{session_name}")
        except Exception as e:
            if "404" in str(e):
                raise ResourceNotFoundError("prearchive session", f"{project}/{timestamp}/{session_name}")
            raise

    def archive(
        self,
        project: str,
        timestamp: str,
        session_name: str,
        subject: Optional[str] = None,
        experiment_label: Optional[str] = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Archive a session from prearchive.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive
            subject: Target subject ID (optional, uses DICOM if not provided)
            experiment_label: Target session label
            overwrite: Overwrite existing session data

        Returns:
            Result dict with archived session info
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}"

        params: dict[str, Any] = {
            "action": "commit",
            "SOURCE": "prearchive",
        }

        if subject:
            params["subject"] = subject
        if experiment_label:
            params["label"] = experiment_label
        if overwrite:
            params["overwrite"] = "delete"

        result = self._post(path, params=params)

        return {
            "success": True,
            "project": project,
            "session": session_name,
            "result": result,
        }

    def delete(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> bool:
        """Delete a session from prearchive.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            True if successful
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}"
        return self._delete(path)

    def rebuild(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> dict[str, Any]:
        """Rebuild/refresh a prearchive session.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            Result dict
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}"
        params = {"action": "rebuild"}

        result = self._post(path, params=params)

        return {
            "success": True,
            "project": project,
            "session": session_name,
            "result": result,
        }

    def move(
        self,
        project: str,
        timestamp: str,
        session_name: str,
        target_project: str,
    ) -> dict[str, Any]:
        """Move a prearchive session to another project.

        Args:
            project: Source project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive
            target_project: Target project ID

        Returns:
            Result dict
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}"
        params = {
            "action": "move",
            "newProject": target_project,
        }

        result = self._post(path, params=params)

        return {
            "success": True,
            "source_project": project,
            "target_project": target_project,
            "session": session_name,
            "result": result,
        }

    def get_scans(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> list[dict[str, Any]]:
        """Get scans from a prearchive session.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            List of scan dicts
        """
        path = f"/data/prearchive/projects/{project}/{timestamp}/{session_name}/scans"
        params = {"format": "json"}

        data = self._get(path, params=params)
        return self._extract_results(data)
