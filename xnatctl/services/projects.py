"""Project service for XNAT project operations."""

from __future__ import annotations

import builtins
from typing import Any

import httpx

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.hierarchy import ProjectRef
from xnatctl.models.project import Project

from .base import BaseService
from .hierarchy import HierarchyService


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
        results = HierarchyService.extract_rows(data)

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
            item = HierarchyService.extract_first_item(data) if isinstance(data, dict) else None
            if item is not None:
                fields, _meta = item
                return Project.model_validate(fields)

            results = HierarchyService.extract_rows(data)
            if results:
                return Project.model_validate(results[0])
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
        results = HierarchyService.extract_rows(data)

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
        results = HierarchyService.extract_rows(data)

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

    # -------------------------------------------------------------------------
    # Raw-row accessors
    #
    # These return the untyped rows XNAT sends so the CLI can render every key
    # it prints today. The typed ``list``/``get`` above drop unknown keys
    # (``extra="ignore"``) and issue a different query (``accessible=true``),
    # which would change both the wire request and the rendered output.
    # -------------------------------------------------------------------------

    def list_rows(self, columns: str) -> builtins.list[dict[str, Any]]:
        """Return raw project rows for the requested columns."""
        data = self.client.get_json("/data/projects", params={"columns": columns})
        return HierarchyService.extract_rows(data)

    def get_detail(self, project_id: str) -> dict[str, Any] | None:
        """Return a project's raw detail row, or ``None`` if not found."""
        data = self.client.get_json(
            HierarchyService.build_project_path(ProjectRef(project_id=project_id))
        )
        item = HierarchyService.extract_first_item(data)
        if item is not None:
            return item[0]
        rows = HierarchyService.extract_rows(data)
        return rows[0] if rows else None

    def subject_rows(self, project_id: str) -> builtins.list[dict[str, Any]]:
        """Return raw subject rows for a project."""
        data = self.client.get_json(
            HierarchyService.build_project_path(ProjectRef(project_id=project_id), "subjects")
        )
        return HierarchyService.extract_rows(data)

    def experiment_rows(self, project_id: str) -> builtins.list[dict[str, Any]]:
        """Return raw experiment rows for a project."""
        data = self.client.get_json(
            HierarchyService.build_project_path(ProjectRef(project_id=project_id), "experiments")
        )
        return HierarchyService.extract_rows(data)

    def create_via_post(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        pi_lastname: str | None = None,
        accessibility: str = "private",
    ) -> httpx.Response:
        """Create a project by POSTing its XML document.

        Distinct from :meth:`create`, which PUTs querystring params. This mirrors
        the XML-body POST the CLI has always sent; the caller inspects the
        returned response's status code.
        """
        name = name or project_id
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xnat:Project ID="{project_id}" xmlns:xnat="http://nrg.wustl.edu/xnat">
    <xnat:name>{name}</xnat:name>
"""
        if description:
            xml += f"    <xnat:description>{description}</xnat:description>\n"
        if pi_lastname:
            xml += f"    <xnat:PI><xnat:lastname>{pi_lastname}</xnat:lastname></xnat:PI>\n"
        xml += "</xnat:Project>"

        return self.client.post(
            f"/data/projects/{project_id}",
            params={"accessibility": accessibility},
            data=xml,
            headers={"Content-Type": "text/xml"},
        )
