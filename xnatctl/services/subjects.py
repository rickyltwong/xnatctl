"""Subject service for XNAT subject operations."""

from __future__ import annotations

import re
from typing import Any, Optional

from xnatctl.models.subject import Subject
from xnatctl.core.exceptions import ResourceNotFoundError, ValidationError

from .base import BaseService


class SubjectService(BaseService):
    """Service for XNAT subject operations."""

    def list(
        self,
        project: Optional[str] = None,
        limit: Optional[int] = None,
        columns: Optional[list[str]] = None,
    ) -> list[Subject]:
        """List subjects.

        Args:
            project: Filter by project ID
            limit: Maximum number of results
            columns: Specific columns to retrieve

        Returns:
            List of Subject objects
        """
        if project:
            path = f"/data/projects/{project}/subjects"
        else:
            path = "/data/subjects"

        params: dict[str, Any] = {"format": "json"}
        if columns:
            params["columns"] = ",".join(columns)

        data = self._get(path, params=params)
        results = self._extract_results(data)

        if limit:
            results = results[:limit]

        return [Subject(**r) for r in results]

    def get(
        self,
        subject_id: str,
        project: Optional[str] = None,
    ) -> Subject:
        """Get subject details.

        Args:
            subject_id: Subject ID or label
            project: Project ID (required if using label)

        Returns:
            Subject object

        Raises:
            ResourceNotFoundError: If subject not found
        """
        if project:
            path = f"/data/projects/{project}/subjects/{subject_id}"
        else:
            path = f"/data/subjects/{subject_id}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
            results = self._extract_results(data)
            if results:
                return Subject(**results[0])
            raise ResourceNotFoundError("subject", subject_id)
        except Exception as e:
            if "404" in str(e):
                raise ResourceNotFoundError("subject", subject_id)
            raise

    def create(
        self,
        project: str,
        label: str,
        group: Optional[str] = None,
        gender: Optional[str] = None,
        yob: Optional[int] = None,
    ) -> Subject:
        """Create a new subject.

        Args:
            project: Project ID
            label: Subject label
            group: Subject group
            gender: Gender (male, female, other, unknown)
            yob: Year of birth

        Returns:
            Created Subject object
        """
        path = f"/data/projects/{project}/subjects/{label}"
        params: dict[str, Any] = {}

        if group:
            params["group"] = group
        if gender:
            params["gender"] = gender
        if yob:
            params["yob"] = str(yob)

        self._put(path, params=params)
        return self.get(label, project=project)

    def delete(
        self,
        subject_id: str,
        project: Optional[str] = None,
        remove_files: bool = False,
    ) -> bool:
        """Delete a subject.

        Args:
            subject_id: Subject ID
            project: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        if project:
            path = f"/data/projects/{project}/subjects/{subject_id}"
        else:
            path = f"/data/subjects/{subject_id}"

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def rename(
        self,
        subject_id: str,
        new_label: str,
        project: Optional[str] = None,
    ) -> Subject:
        """Rename a subject.

        Args:
            subject_id: Current subject ID or label
            new_label: New label
            project: Project ID

        Returns:
            Updated Subject object
        """
        if project:
            path = f"/data/projects/{project}/subjects/{subject_id}"
        else:
            path = f"/data/subjects/{subject_id}"

        params = {"label": new_label}
        self._put(path, params=params)

        return self.get(new_label, project=project)

    def rename_batch(
        self,
        project: str,
        mapping: dict[str, str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Batch rename subjects using a mapping.

        Args:
            project: Project ID
            mapping: Dict of old_label -> new_label
            dry_run: Preview changes without applying

        Returns:
            Summary dict with renamed, skipped, errors
        """
        results = {
            "renamed": [],
            "skipped": [],
            "errors": [],
            "dry_run": dry_run,
        }

        for old_label, new_label in mapping.items():
            try:
                if dry_run:
                    # Verify subject exists
                    self.get(old_label, project=project)
                    results["renamed"].append({
                        "from": old_label,
                        "to": new_label,
                    })
                else:
                    self.rename(old_label, new_label, project=project)
                    results["renamed"].append({
                        "from": old_label,
                        "to": new_label,
                    })
            except ResourceNotFoundError:
                results["skipped"].append({
                    "label": old_label,
                    "reason": "not found",
                })
            except Exception as e:
                results["errors"].append({
                    "label": old_label,
                    "error": str(e),
                })

        return results

    def rename_pattern(
        self,
        project: str,
        match_pattern: str,
        to_template: str,
        dry_run: bool = False,
        merge: bool = False,
    ) -> dict[str, Any]:
        """Rename subjects matching a pattern.

        Args:
            project: Project ID
            match_pattern: Regex pattern with capture groups
            to_template: Template using {1}, {2} for groups
            dry_run: Preview changes without applying
            merge: Allow merging into existing subjects

        Returns:
            Summary dict with renamed, merged, skipped, errors
        """
        try:
            pattern = re.compile(match_pattern)
        except re.error as e:
            raise ValidationError(f"Invalid regex pattern: {e}")

        subjects = self.list(project=project)
        results = {
            "renamed": [],
            "merged": [],
            "skipped": [],
            "errors": [],
            "dry_run": dry_run,
        }

        # Build mapping from pattern
        mapping: dict[str, str] = {}
        for subject in subjects:
            label = subject.label or subject.id
            match = pattern.match(label)
            if match:
                # Build new label from template
                new_label = to_template
                for i, group in enumerate(match.groups(), 1):
                    new_label = new_label.replace(f"{{{i}}}", group or "")
                mapping[label] = new_label

        # Check for duplicates (merges)
        target_labels: dict[str, list[str]] = {}
        for old_label, new_label in mapping.items():
            if new_label not in target_labels:
                target_labels[new_label] = []
            target_labels[new_label].append(old_label)

        # Process renames
        for new_label, old_labels in target_labels.items():
            if len(old_labels) > 1:
                # Multiple subjects mapping to same target
                if merge:
                    for old_label in old_labels:
                        if dry_run:
                            results["merged"].append({
                                "from": old_label,
                                "to": new_label,
                            })
                        else:
                            try:
                                self.rename(old_label, new_label, project=project)
                                results["merged"].append({
                                    "from": old_label,
                                    "to": new_label,
                                })
                            except Exception as e:
                                results["errors"].append({
                                    "label": old_label,
                                    "error": str(e),
                                })
                else:
                    for old_label in old_labels:
                        results["skipped"].append({
                            "label": old_label,
                            "reason": f"would merge into {new_label} (use --merge)",
                        })
            else:
                old_label = old_labels[0]
                if old_label == new_label:
                    results["skipped"].append({
                        "label": old_label,
                        "reason": "no change",
                    })
                elif dry_run:
                    results["renamed"].append({
                        "from": old_label,
                        "to": new_label,
                    })
                else:
                    try:
                        self.rename(old_label, new_label, project=project)
                        results["renamed"].append({
                            "from": old_label,
                            "to": new_label,
                        })
                    except Exception as e:
                        results["errors"].append({
                            "label": old_label,
                            "error": str(e),
                        })

        return results

    def get_sessions(
        self,
        subject_id: str,
        project: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get sessions for a subject.

        Args:
            subject_id: Subject ID
            project: Project ID

        Returns:
            List of session data dicts
        """
        if project:
            path = f"/data/projects/{project}/subjects/{subject_id}/experiments"
        else:
            path = f"/data/subjects/{subject_id}/experiments"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)
