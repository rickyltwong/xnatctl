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
        data = self.client.get_json(f"/data/subjects/{quote_path_segment(subject_id)}/projects")
        return HierarchyService.extract_rows(data)
