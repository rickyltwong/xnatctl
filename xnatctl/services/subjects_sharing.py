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
    """Share/unshare subjects across projects (see the notes below)."""

    # -------------------------------------------------------------------------
    # Cross-project sharing
    #
    # Verified live against XNAT 1.9.2.1: PUT/DELETE
    # /data/subjects/{id}/projects/{target_project} shares/unshares a subject
    # without moving it (its primary project is untouched). The subject *ID*
    # here must be the canonical accession ID, not a label -- a bare label
    # (even inside its own project) answered 403 "Sharing is not allowed" in
    # testing, while the resolved accession ID worked with no project scoping
    # needed at all. Callers resolve the label first (see
    # ``cli/sharing.py``, which uses ``HierarchyService.resolve_subject``).
    # -------------------------------------------------------------------------

    def share(
        self, subject_id: str, target_project: str, *, label: str | None = None
    ) -> httpx.Response:
        """Share a subject into another project without moving it.

        Args:
            subject_id: Canonical (accession ID) subject identifier.
            target_project: Project to share the subject into.
            label: Label the subject should carry in ``target_project``. XNAT
                omitted, XNAT defaults it to the subject's accession ID
                (verified live) -- NOT the label it carries in its primary
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
        because XNAT does NOT treat that as a no-op: verified live against
        1.9.2.1, ``DELETE /data/subjects/{id}/projects/{primary}`` answers
        200 and **deletes the subject outright** -- afterwards the subject
        is 404. There is no separate confirmation and nothing in the
        response distinguishes it from removing an ordinary share, so a
        caller who mistypes the target project loses the subject and every
        experiment under it while being told a share was removed. The guard
        lives here rather than only in the CLI so a library caller gets it
        too; deleting a subject has its own verb.

        Project IDs are compared case-insensitively. XNAT's own IDs are
        case-sensitive, so this refuses a little more than it strictly must
        -- the right trade when the cost of a false negative is deleting a
        subject.

        Verified live: a subject that was never shared into
        ``target_project`` answers HTTP 403 ("Subject is not assigned to
        specified project ..."), which the client surfaces as
        :class:`~xnatctl.core.exceptions.PermissionDeniedError` -- misleading
        wording from the server, not actually a permission problem, but not
        translated here since the message is server-version-specific text,
        not a stable status code to key off of.

        Args:
            subject_id: Canonical (accession ID) subject identifier.
            target_project: Project to remove the share from.
            primary_project: The subject's owning project, used to refuse
                the destructive case.

        Returns:
            The raw DELETE response.

        Raises:
            InputValidationError: If ``target_project`` is the subject's
                primary project.
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
        """List every project a subject is assigned to (primary + shared).

        Args:
            subject_id: Canonical (accession ID) subject identifier.

        Returns:
            One row per project, each carrying ``ID`` (the project id),
            ``label`` (the subject's label in that project),
            ``Secondary_ID`` and ``Name`` -- exactly what
            ``GET /data/subjects/{id}/projects`` returns, including the
            subject's own primary project as one of the rows.
        """
        data = self.client.get_json(f"/data/subjects/{quote_path_segment(subject_id)}/projects")
        return HierarchyService.extract_rows(data)
