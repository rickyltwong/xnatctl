"""Project service for XNAT project operations."""

from __future__ import annotations

import builtins
from typing import Any

import httpx

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.hierarchy import ProjectRef
from xnatctl.models.project import Project

from .base import BaseService
from .hierarchy import HierarchyService

# Project membership roles, as accepted on the CLI/library surface. XNAT's
# actual group ID for each is the SINGULAR form ``{project}_{role}`` -- e.g.
# ``PROJ01_owner`` -- confirmed against xnat-web:
# DefaultGroupsAndPermissionsCache.java (queries group ids with
# ``LIKE '%_owner'`` / ``projectId + "_collaborator"`` / ``projectId +
# "_member"``) and matches the existing ``AdminService.add_user_to_groups``
# convention (``f"{project}_{role}"``). It is NOT the plural
# ``owners``/``members``/``collaborators`` form.
_VALID_PROJECT_ROLES = ("owner", "member", "collaborator")


def _validate_project_role(role: str) -> str:
    """Validate a project membership role, raising with the valid set on failure."""
    if role not in _VALID_PROJECT_ROLES:
        raise InputValidationError(
            f"Invalid role '{role}'. Valid roles: {', '.join(_VALID_PROJECT_ROLES)}",
            field="role",
            value=role,
        )
    return role


def _row_login(row: dict[str, Any]) -> str:
    """Extract a users-listing row's username, tolerating a couple of key spellings."""
    return str(row.get("login") or row.get("username") or row.get("ID") or "").strip()


def _row_group_id(row: dict[str, Any]) -> str:
    """Extract a users-listing row's XNAT group ID.

    ``GROUP_ID`` is the canonical column -- confirmed against xnat-web's
    ``ProjectUserListResource``/``ProjectMemberResource`` (``SELECT g.id AS
    "GROUP_ID", displayname, login, firstname, lastname, email FROM
    xdat_userGroup g ...``), which is also the exact segment
    ``/data/projects/{P}/users/{GROUP_ID}/{USER_ID}`` expects back for a
    mutation. The looser fallbacks exist only for defensiveness against a
    server fork that renders the column differently.
    """
    return str(row.get("GROUP_ID") or row.get("groupname") or row.get("group") or "").strip()


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
        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
            params["limit"] = str(limit)

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # belt-and-braces: some XNAT endpoints ignore `limit`
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
        path = f"/data/projects/{quote_path_segment(project_id)}"
        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("project", project_id) from e

        item = HierarchyService.extract_first_item(data) if isinstance(data, dict) else None
        if item is not None:
            fields, _meta = item
            return Project.model_validate(fields)

        results = HierarchyService.extract_rows(data)
        if results:
            return Project.model_validate(results[0])
        raise ResourceNotFoundError("project", project_id)

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
        path = f"/data/projects/{quote_path_segment(project_id)}"
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
        path = f"/data/projects/{quote_path_segment(project_id)}"
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
        path = f"/data/projects/{quote_path_segment(project_id)}/subjects"
        params = {"format": "json"}

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
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
        path = f"/data/projects/{quote_path_segment(project_id)}/experiments"
        params = {"format": "json"}

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
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
        path = (
            f"/data/projects/{quote_path_segment(project_id)}"
            f"/accessibility/{quote_path_segment(accessibility)}"
        )
        self._put(path)
        return True

    def get_accessibility(self, project_id: str) -> str:
        """Get a project's accessibility level.

        Args:
            project_id: Project ID.

        Returns:
            One of ``"private"``, ``"protected"``, ``"public"``.
        """
        path = f"/data/projects/{quote_path_segment(project_id)}/accessibility"
        resp = self.client.get(path)
        return resp.text.strip()

    def list_users(self, project_id: str) -> builtins.list[dict[str, Any]]:
        """List a project's users and their group membership.

        Args:
            project_id: Project ID.

        Returns:
            List of raw user rows, as XNAT returns them.
        """
        data = self.client.get_json(f"/data/projects/{quote_path_segment(project_id)}/users")
        if isinstance(data, list):
            return data
        return HierarchyService.extract_rows(data) if isinstance(data, dict) else []

    def grant(self, project_id: str, username: str, role: str) -> httpx.Response:
        """Grant a user a role on a project.

        Args:
            project_id: Project ID.
            username: XNAT username.
            role: One of ``owner``, ``member``, ``collaborator``.

        Raises:
            InputValidationError: If ``role`` is not one of the valid roles.
        """
        role = _validate_project_role(role)
        group_id = f"{project_id}_{role}"
        path = (
            f"/data/projects/{quote_path_segment(project_id)}"
            f"/users/{quote_path_segment(group_id)}/{quote_path_segment(username)}"
        )
        return self.client.put(path)

    def revoke(self, project_id: str, username: str) -> builtins.list[str]:
        """Revoke a user's project membership, from every group they are in.

        A user appears once per group they belong to in the
        ``/data/projects/{ID}/users`` listing (one row per membership, not
        one row per user), so a user who somehow holds more than one role on
        the same project is removed from ALL of them. Every membership row is
        resolved and validated BEFORE any DELETE is issued -- a user with one
        resolvable and one unresolvable membership must fail outright rather
        than silently revoking the resolvable one and reporting full success
        while the other membership survives untouched.

        Args:
            project_id: Project ID.
            username: XNAT username.

        Returns:
            The distinct XNAT group IDs the user was removed from (a
            duplicate membership row, e.g. from a server-side join quirk,
            issues one DELETE, not one per row).

        Raises:
            ResourceNotFoundError: If ``username`` is not a member of the project.
            InputValidationError: If any of the user's membership rows lacks
                a resolvable group ID.
        """
        rows = self.list_users(project_id)
        user_rows = [row for row in rows if _row_login(row).casefold() == username.casefold()]

        if not user_rows:
            raise ResourceNotFoundError("project user", f"{project_id}/{username}")

        group_ids: builtins.list[str] = []
        seen: set[str] = set()
        for row in user_rows:
            group_id = _row_group_id(row)
            if not group_id:
                raise InputValidationError(
                    f"Found {username} in project {project_id} but at least one of "
                    "their membership rows carried no resolvable group ID -- refusing "
                    "to revoke a subset of their access.",
                    field="username",
                    value=username,
                )
            if group_id not in seen:
                seen.add(group_id)
                group_ids.append(group_id)

        removed: builtins.list[str] = []
        for group_id in group_ids:
            path = (
                f"/data/projects/{quote_path_segment(project_id)}"
                f"/users/{quote_path_segment(group_id)}/{quote_path_segment(username)}"
            )
            self.client.delete(path)
            removed.append(group_id)
        return removed

    def access_requests(self, project_id: str) -> builtins.list[dict[str, Any]]:
        """List a project's access requests (XNAT's PARS), pending and resolved.

        Args:
            project_id: Project ID.

        Returns:
            List of raw access-request rows (``par_id``, ``proj_id``,
            ``level``, ``create_date``, ``email``, ``login``,
            ``secondary_id``, ``approved``, ``approval_date``).

        The underlying query (xnat-web's ``ProjectPARListResource``) carries
        no ``approval_date IS NULL`` filter -- unlike the global, self-service
        ``/data/pars`` listing -- so this returns every request ever made for
        the project, not just pending ones. ``approved``/``approval_date``
        show each row's resolution state.

        There is deliberately no method here to approve or deny a request:
        XNAT's PAR resolution (``PUT /data/pars/{id}``, confirmed against
        ``ProjectAccessRequest.process()``) always acts on the CURRENT
        SESSION USER, ignoring who the request was actually for -- an
        invitation is accepted by the invitee logging in and responding to
        it themselves, not resolved by an admin on someone else's behalf.
        Calling it from an admin session would add the ADMIN to the project's
        group, not the intended user. Stock XNAT does not expose admin-side
        PAR resolution over REST at all.
        """
        data = self.client.get_json(f"/data/projects/{quote_path_segment(project_id)}/pars")
        if isinstance(data, list):
            return data
        return HierarchyService.extract_rows(data) if isinstance(data, dict) else []

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
            f"/data/projects/{quote_path_segment(project_id)}",
            params={"accessibility": accessibility},
            data=xml,
            headers={"Content-Type": "text/xml"},
        )
