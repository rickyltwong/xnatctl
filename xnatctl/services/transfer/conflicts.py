"""Conflict checker for project transfers.

Queries the destination XNAT server to detect label/project mismatches
before transferring subjects and experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xnatctl.services.base import BaseService


@dataclass(frozen=True, slots=True)
class ConflictResult:
    """Result of a conflict check against the destination server.

    Attributes:
        has_conflict: True if a conflict was detected.
        reason: Human-readable description of the conflict.
        remote_id: ID of the conflicting entity on the destination, if found.
    """

    has_conflict: bool
    reason: str
    remote_id: str | None


class ConflictChecker(BaseService):
    """Check for conflicts on the destination XNAT before transfer.

    Queries the destination server's REST API to verify that entities
    with matching IDs have consistent labels and project assignments.
    """

    def check_subject(
        self, subject_id: str, expected_label: str, expected_project: str
    ) -> ConflictResult:
        """Check if a subject conflicts with an existing one on the destination.

        Args:
            subject_id: Subject accession ID (e.g. XNAT_S00001).
            expected_label: Expected subject label on the destination.
            expected_project: Expected project ID on the destination.

        Returns:
            ConflictResult indicating whether a conflict was found.
        """
        return self._check_entity("subjects", subject_id, expected_label, expected_project)

    def check_experiment(
        self, experiment_id: str, expected_label: str, expected_project: str
    ) -> ConflictResult:
        """Check if an experiment conflicts with an existing one on the destination.

        Args:
            experiment_id: Experiment accession ID (e.g. XNAT_E00001).
            expected_label: Expected experiment label on the destination.
            expected_project: Expected project ID on the destination.

        Returns:
            ConflictResult indicating whether a conflict was found.
        """
        return self._check_entity("experiments", experiment_id, expected_label, expected_project)

    def _check_entity(
        self,
        entity_type: str,
        entity_id: str,
        expected_label: str,
        expected_project: str,
    ) -> ConflictResult:
        """Query destination for an entity and compare label/project.

        Args:
            entity_type: XNAT entity type path segment (subjects/experiments).
            entity_id: Accession ID to look up.
            expected_label: Label we expect on the destination.
            expected_project: Project we expect on the destination.

        Returns:
            ConflictResult with conflict details if mismatches found.
        """
        data: dict[str, Any] = self._get(
            f"/data/{entity_type}",
            params={"format": "json", "ID": entity_id, "columns": "ID,label,project"},
        )
        results = self._extract_results(data)

        if not results:
            return ConflictResult(has_conflict=False, reason="", remote_id=None)

        remote = results[0]
        remote_id = remote.get("ID", entity_id)
        remote_label = remote.get("label", "")
        remote_project = remote.get("project", "")

        if remote_label != expected_label:
            return ConflictResult(
                has_conflict=True,
                reason=(
                    f"Label mismatch: expected '{expected_label}', "
                    f"found '{remote_label}' on destination"
                ),
                remote_id=remote_id,
            )

        if remote_project != expected_project:
            return ConflictResult(
                has_conflict=True,
                reason=(
                    f"Project mismatch: expected '{expected_project}', "
                    f"found '{remote_project}' on destination"
                ),
                remote_id=remote_id,
            )

        return ConflictResult(has_conflict=False, reason="", remote_id=remote_id)
