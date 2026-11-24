"""Cross-project sharing operations for subjects.

Mixin composed into :class:`~xnatctl.services.subjects.SubjectService`; split
out so the sharing surface (share/unshare/list shares) lives beside its own
server-behaviour notes.
"""

from __future__ import annotations

import builtins
from typing import Any

import httpx

from xnatctl.core.exceptions import (
    ClientRequestError,
    InputValidationError,
    ResourceExistsError,
)
from xnatctl.core.validation import quote_path_segment

from .base import BaseService
from .hierarchy import HierarchyService


class _SubjectSharingMixin(BaseService):
    def share(
        self, subject_id: str, target_project: str, *, label: str | None = None
    ) -> httpx.Response:
        """Share a subject into another project without moving it.

        Args:
            subject_id: Canonical (accession ID) subject identifier.
            target_project: Project to share the subject into.
            label: Label the subject should carry in ``target_project``. If
                omitted, XNAT defaults it to the subject's accession ID
                (verified live), not the label it carries in its primary
                project.

        Returns:
            The raw PUT response.

        Raises:
            ResourceExistsError: The subject is already shared into
                ``target_project`` (XNAT answers this with HTTP 409).
        """
        path = (
            f"/data/subjects/{quote_path_segment(subject_id)}"
            f"/projects/{quote_path_segment(target_project)}"
        )
        params: dict[str, str] = {}
        if label:
            params["label"] = label
        try:
            return self.client.put(path, params=params)
        except ClientRequestError as e:
            if e.status_code == 409:
                raise ResourceExistsError(
                    "subject share", f"{subject_id} -> {target_project}"
                ) from e
            raise

    def unshare(
        self, subject_id: str, target_project: str, *, primary_project: str
    ) -> httpx.Response:
        """Remove a subject's share into another project.

        ``primary_project`` is required, and aiming this at it is refused,
        because XNAT does not treat that as a no-op. Verified live against
        1.9.2.1: ``DELETE /data/subjects/{id}/projects/{primary}`` answers 200
        and deletes the subject outright, with nothing in the response to tell
        it apart from removing a share. The guard lives here rather than only
        in the CLI so a library caller gets it too. Project IDs are compared
        case-insensitively, which refuses slightly more than it must.

        A subject that was never shared into ``target_project`` answers HTTP
        403, which surfaces as
        :class:`~xnatctl.core.exceptions.PermissionDeniedError` even though it
        is not a permission problem.

        Args:
            subject_id: Canonical (accession ID) subject identifier.
            target_project: Project to remove the share from.
            primary_project: The subject's owning project, used to refuse
                the destructive case.

        Returns:
            The raw DELETE response.

        Raises:
            InputValidationError: If ``primary_project`` is empty, or
                ``target_project`` is the subject's primary project.
        """
        if not primary_project or not primary_project.strip():
            raise InputValidationError(
                f"refusing to unshare subject {subject_id}: the primary project is unknown, so "
                "the check that stops this from deleting the subject outright cannot run. "
                "Pass the owning project explicitly.",
                field="primary_project",
                value=primary_project,
            )
        if target_project.strip().casefold() == primary_project.strip().casefold():
            raise InputValidationError(
                f"refusing to unshare subject {subject_id} from {target_project}: that is its "
                "primary project, and XNAT answers this by deleting the subject and everything "
                "under it, not by removing a share. Use `xnatctl subject delete` if deletion "
                "is what you want.",
                field="from",
                value=target_project,
            )
        path = (
            f"/data/subjects/{quote_path_segment(subject_id)}"
            f"/projects/{quote_path_segment(target_project)}"
        )
        return self.client.delete(path)

    def list_shares(self, subject_id: str) -> builtins.list[dict[str, Any]]:
        """List every project a subject is assigned to (primary and shared).

        Args:
            subject_id: Canonical (accession ID) subject identifier.

        Returns:
            One row per project, each carrying ``ID`` (the project id),
            ``label`` (the subject's label in that project), ``Secondary_ID``
            and ``Name``, as ``GET /data/subjects/{id}/projects`` returns them,
            including the subject's own primary project.
        """
        data = self.client.get_json(f"/data/subjects/{quote_path_segment(subject_id)}/projects")
        return HierarchyService.extract_rows(data)
