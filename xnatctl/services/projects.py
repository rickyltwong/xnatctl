"""Project service for XNAT project operations."""

from __future__ import annotations

import builtins
from typing import Any

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.project import Project

from .base import BaseService


class ProjectService(BaseService):
    """Service for XNAT project operations."""

    def list(
        self,
        accessible: bool = True,
        limit: int | None = None,
    ) -> builtins.list[Project]:
        """List projects.

        Args:
            accessible: Only list accessible projects
            limit: Maximum number of results

        Returns:
            List of Project objects
        """
        path = "/data/projects"
        params: dict[str, Any] = {"format": "json"}

        if accessible:
            params["accessible"] = "true"

        data = self._get(path, params=params)
        results = self._extract_results(data)

        if limit:
            results = results[:limit]

        return [Project(**r) for r in results]

    def get(self, project_id: str) -> Project:
        """Get project details.

        Args:
            project_id: Project ID

        Returns:
            Project object

        Raises:
            ResourceNotFoundError: If project not found
        """
        path = f"/data/projects/{project_id}"
        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
            results = self._extract_results(data)
            if results:
                return Project(**results[0])
            raise ResourceNotFoundError("project", project_id)
        except Exception as e:
            if "404" in str(e):
                raise ResourceNotFoundError("project", project_id) from e
            raise

    def create(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        keywords: str | None = None,
        pi_firstname: str | None = None,
        pi_lastname: str | None = None,
        accessibility: str = "private",
    ) -> Project:
        """Create a new project.

        Args:
            project_id: Project ID (must be unique)
            name: Project name (defaults to project_id)
            description: Project description
            keywords: Comma-separated keywords
            pi_firstname: PI first name
            pi_lastname: PI last name
            accessibility: Access level (private, protected, public)

        Returns:
            Created Project object
        """
        path = f"/data/projects/{project_id}"
        params: dict[str, Any] = {}

        if name:
            params["name"] = name
        if description:
            params["description"] = description
        if keywords:
            params["keywords"] = keywords
        if pi_firstname:
            params["pi_firstname"] = pi_firstname
        if pi_lastname:
            params["pi_lastname"] = pi_lastname
        if accessibility:
            params["accessibility"] = accessibility

        self._put(path, params=params)
        return self.get(project_id)

    def delete(
        self,
        project_id: str,
        remove_files: bool = False,
    ) -> bool:
        """Delete a project.

        Args:
            project_id: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        path = f"/data/projects/{project_id}"
        params: dict[str, Any] = {}

        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def get_subjects(
        self,
        project_id: str,
        limit: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get subjects in a project.

        Args:
            project_id: Project ID
            limit: Maximum number of results

        Returns:
            List of subject data dicts
        """
        path = f"/data/projects/{project_id}/subjects"
        params = {"format": "json"}

        data = self._get(path, params=params)
        results = self._extract_results(data)

        if limit:
            results = results[:limit]

        return results

    def get_sessions(
        self,
        project_id: str,
        limit: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get sessions/experiments in a project.

        Args:
            project_id: Project ID
            limit: Maximum number of results

        Returns:
            List of session data dicts
        """
        path = f"/data/projects/{project_id}/experiments"
        params = {"format": "json"}

        data = self._get(path, params=params)
        results = self._extract_results(data)

        if limit:
            results = results[:limit]

        return results

    def set_accessibility(
        self,
        project_id: str,
        accessibility: str,
    ) -> bool:
        """Set project accessibility level.

        Args:
            project_id: Project ID
            accessibility: Access level (private, protected, public)

        Returns:
            True if successful
        """
        path = f"/data/projects/{project_id}/accessibility/{accessibility}"
        self._put(path)
        return True
