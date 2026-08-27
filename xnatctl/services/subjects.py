"""Subject service for XNAT subject operations."""

from __future__ import annotations

import builtins
import re
from typing import Any

import httpx

from xnatctl.core.exceptions import (
    InputValidationError,
    ResourceNotFoundError,
)
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.subject import Subject

from .hierarchy import HierarchyService
from .subjects_sharing import _SubjectSharingMixin

#: Custom-variable names are embedded literally inside an XNAT xpath-style
#: query key (``xnat:subjectData/fields/field[name=X]/field``). httpx encodes
#: whatever characters we put in *transport-safe*, but a name containing `]`,
#: `=`, or `/` would still change the *meaning* the server parses back out of
#: it once decoded -- e.g. a value ending `]/field=evil` could close the
#: bracket expression early and append a second one. Restricted to a plain
#: identifier shape rather than relying on the server to reject the rest.
# Matched with `fullmatch`, not `match`: Python's `$` also matches just
# BEFORE a final newline, so a `^...$` + `.match()` pair accepts
# "field\n" and embeds the newline in the XNAT query key.
_FIELD_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _validate_field_name(name: str) -> str:
    """Reject a custom-variable name that could break the field[] expression."""
    if not name or not _FIELD_NAME_RE.fullmatch(name):
        raise InputValidationError(
            "custom variable name must be a non-empty string of letters, digits, '.', '_' or '-'",
            field="name",
            value=name,
        )
    return name


class SubjectService(_SubjectSharingMixin):
    """Service for XNAT subject operations."""

    def list(
        self,
        project: str | None = None,
        limit: int | None = None,
        columns: builtins.list[str] | None = None,
    ) -> builtins.list[Subject]:
        """List subjects.

        Args:
            project: Filter by project ID
            limit: Maximum number of results
            columns: Specific columns to retrieve

        Returns:
            List of Subject objects
        """
        # `is not None`, not truthy: `project=""` is a caller mistake, not
        # "no filter" -- it must not silently widen to the site-wide listing.
        if project is not None:
            path = f"/data/projects/{quote_path_segment(project)}/subjects"
        else:
            path = "/data/subjects"

        params: dict[str, Any] = {"format": "json"}
        if columns:
            params["columns"] = ",".join(columns)
        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
            params["limit"] = str(limit)

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # belt-and-braces: some XNAT endpoints ignore `limit`
            results = results[:limit]

        return [Subject(**r) for r in results]

    def get(
        self,
        subject_id: str,
        project: str | None = None,
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
        if project is not None:  # not truthy -- "" must raise, not fall to the flat subjects path
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("subject", subject_id) from e

        item = HierarchyService.extract_first_item(data) if isinstance(data, dict) else None
        if item is not None:
            fields, _meta = item
            return Subject.model_validate(fields)

        results = HierarchyService.extract_rows(data)
        if results:
            return Subject.model_validate(results[0])
        raise ResourceNotFoundError("subject", subject_id)

    def create(
        self,
        project: str,
        label: str,
        group: str | None = None,
        gender: str | None = None,
        yob: int | None = None,
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
        path = f"/data/projects/{quote_path_segment(project)}/subjects/{quote_path_segment(label)}"
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
        project: str | None = None,
        remove_files: bool = False,
        force: bool = False,
    ) -> bool:
        """Delete a subject.

        By default, refuses to delete a subject that still has experiments
        attached (XNAT would cascade-delete the experiments). Pass
        ``force=True`` to override this safety check — but only do so after
        explicitly reassigning or archiving the experiments yourself.

        Args:
            subject_id: Subject ID or label.
            project: Project ID (required when using a label).
            remove_files: Also remove files from filesystem.
            force: Skip the "subject has experiments" safety check. Use with
                extreme caution; cascade-deletes all attached experiments.

        Returns:
            True if successful.

        Raises:
            RuntimeError: If the subject has attached experiments and
                ``force=False``.
        """
        if not force and project:
            try:
                sessions = self.get_sessions(subject_id, project=project)
            except ResourceNotFoundError:
                # Subject/project genuinely gone -- no sessions is a valid
                # inference. Any other failure (network, auth, ...) must
                # propagate: this check fails closed, not open.
                sessions = []
            if sessions:
                raise RuntimeError(
                    f"Refusing to delete subject '{subject_id}' in project "
                    f"'{project}': {len(sessions)} experiment(s) still attached "
                    f"(IDs: {[e.get('ID') for e in sessions]}). "
                    "Reassign or archive experiments first, or pass force=True "
                    "to cascade-delete them."
                )

        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}"

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def rename(
        self,
        subject_id: str,
        new_label: str,
        project: str | None = None,
    ) -> Subject:
        """Rename a subject.

        Args:
            subject_id: Current subject ID or label
            new_label: New label
            project: Project ID

        Returns:
            Updated Subject object
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}"

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
        results: dict[str, Any] = {
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
                    results["renamed"].append(
                        {
                            "from": old_label,
                            "to": new_label,
                        }
                    )
                else:
                    self.rename(old_label, new_label, project=project)
                    results["renamed"].append(
                        {
                            "from": old_label,
                            "to": new_label,
                        }
                    )
            except ResourceNotFoundError:
                results["skipped"].append(
                    {
                        "label": old_label,
                        "reason": "not found",
                    }
                )
            except Exception as e:  # noqa: BLE001  # per-item batch isolation in rename_batch/rename_pattern
                results["errors"].append(
                    {
                        "label": old_label,
                        "error": str(e),
                    }
                )

        return results

    def rename_pattern(  # noqa: C901  # pre-existing; see pyproject
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
            raise InputValidationError(f"Invalid regex pattern: {e}") from e

        subjects = self.list(project=project)
        results: dict[str, Any] = {
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
        target_labels: dict[str, builtins.list[str]] = {}
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
                            results["merged"].append(
                                {
                                    "from": old_label,
                                    "to": new_label,
                                }
                            )
                        else:
                            try:
                                self.rename(old_label, new_label, project=project)
                                results["merged"].append(
                                    {
                                        "from": old_label,
                                        "to": new_label,
                                    }
                                )
                            except Exception as e:  # noqa: BLE001  # per-item batch isolation in rename_batch/rename_pattern
                                results["errors"].append(
                                    {
                                        "label": old_label,
                                        "error": str(e),
                                    }
                                )
                else:
                    for old_label in old_labels:
                        results["skipped"].append(
                            {
                                "label": old_label,
                                "reason": f"would merge into {new_label} (use --merge)",
                            }
                        )
            else:
                old_label = old_labels[0]
                if old_label == new_label:
                    results["skipped"].append(
                        {
                            "label": old_label,
                            "reason": "no change",
                        }
                    )
                elif dry_run:
                    results["renamed"].append(
                        {
                            "from": old_label,
                            "to": new_label,
                        }
                    )
                else:
                    try:
                        self.rename(old_label, new_label, project=project)
                        results["renamed"].append(
                            {
                                "from": old_label,
                                "to": new_label,
                            }
                        )
                    except Exception as e:  # noqa: BLE001  # per-item batch isolation in rename_batch/rename_pattern
                        results["errors"].append(
                            {
                                "label": old_label,
                                "error": str(e),
                            }
                        )

        return results

    def get_sessions(
        self,
        subject_id: str,
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get sessions for a subject.

        Args:
            subject_id: Subject ID
            project: Project ID

        Returns:
            List of session data dicts
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}/experiments"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}/experiments"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return HierarchyService.extract_rows(data)

    def merge_subjects(
        self,
        project: str,
        source_label: str,
        target_label: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Merge source subject into target subject.

        This moves all experiments/sessions from the source subject to the
        target subject, then deletes the empty source subject.

        Use this when renaming would result in a duplicate - the experiments
        are consolidated under the target subject.

        Args:
            project: Project ID
            source_label: Source subject label (will be deleted)
            target_label: Target subject label (will receive experiments)
            dry_run: Preview changes without applying

        Returns:
            Summary dict with:
            - experiments_moved: number of experiments moved
            - source_deleted: whether source was deleted
            - experiments: list of moved experiment IDs

        Raises:
            ResourceNotFoundError: If source or target not found
        """
        # Verify both subjects exist
        self.get(source_label, project=project)
        target = self.get(target_label, project=project)

        # Get experiments from source
        experiments = self.get_sessions(source_label, project=project)

        result: dict[str, Any] = {
            "source": source_label,
            "target": target_label,
            "experiments_moved": 0,
            "source_deleted": False,
            "experiments": [],
            "dry_run": dry_run,
        }

        if dry_run:
            result["experiments_moved"] = len(experiments)
            result["experiments"] = [e.get("ID") for e in experiments]
            result["source_deleted"] = True
            return result

        # Move each experiment to target using the same scoped PUT shape that
        # the XNAT web UI issues for "Change parent subject":
        #
        #   PUT /data/projects/{p}/subjects/{TARGET_SUBJECT_ID}/experiments/{EXP_ID}
        #
        # The target subject is encoded in the URL *path* (internal XNAT ID),
        # not in an XML-path-shortcut querystring. A prior implementation
        # used PUT /data/experiments/{EXP_ID}?xnat:experimentData/subject_ID=...
        # which was silently destructive on this XNAT version — it neither
        # moved the experiment to the target nor left it on the source, and
        # the subsequent source DELETE had nothing to cascade. Do not
        # reintroduce that pattern. Verified on the dev XNAT on 2026-04-23.
        target_id = target.id
        if not target_id:
            raise InputValidationError(
                f"Could not resolve internal subject ID for target '{target_label}'. "
                "Refusing to merge without a resolved target subject ID."
            )
        for exp in experiments:
            exp_id = exp.get("ID")
            if not exp_id:
                continue

            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(target_id)}"
                f"/experiments/{quote_path_segment(exp_id)}"
            )
            self._put(
                path,
                params={
                    "format": "json",
                    "event_type": "WEB_FORM",
                    "event_action": "Modified subject",
                },
            )

            # Verify the reassignment before proceeding. GET the experiment
            # and confirm its subject_ID is now the target. If the PUT failed
            # silently or destructively, this guard aborts before we touch
            # the source subject.
            verify = self._get(
                f"/data/experiments/{quote_path_segment(exp_id)}", params={"format": "json"}
            )
            item = HierarchyService.extract_first_item(verify) if isinstance(verify, dict) else None
            if item is None:
                raise RuntimeError(
                    f"Reassignment of experiment '{exp_id}' could not be verified: "
                    "experiment not returned by GET. Aborting merge before source delete."
                )
            fields, _meta = item
            actual_subject = fields.get("subject_ID")
            if actual_subject != target_id:
                raise RuntimeError(
                    f"Reassignment of experiment '{exp_id}' did not take effect: "
                    f"subject_ID is '{actual_subject}', expected '{target_id}'. "
                    "Aborting merge before source delete to prevent data loss."
                )

            result["experiments"].append(exp_id)
            result["experiments_moved"] += 1

        # Fail-safe: re-list source experiments before delete. With the
        # per-experiment verify loop above this should always be empty, but
        # the check stays as defence in depth — the subsequent DELETE is
        # cascade-capable, so any residual experiment here would be
        # destroyed.
        remaining = self.get_sessions(source_label, project=project)
        if remaining:
            raise RuntimeError(
                f"Merge of '{source_label}' -> '{target_label}' left "
                f"{len(remaining)} experiment(s) still attached to source "
                f"(IDs: {[e.get('ID') for e in remaining]}). "
                "Refusing to delete source subject to prevent data loss."
            )

        # Delete the now-empty source subject
        self.delete(source_label, project=project)
        result["source_deleted"] = True

        return result

    # -------------------------------------------------------------------------
    # Raw-row / raw-response accessors
    #
    # The typed ``list``/``rename``/``delete`` above return models or run extra
    # safety round-trips. These issue exactly the request the CLI has always
    # sent and hand back the untyped rows (or the raw response, for mutations
    # whose status code the CLI branches on).
    # -------------------------------------------------------------------------

    def list_rows(
        self, project: str | None, columns: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Return raw subject rows for a project."""
        path = HierarchyService.build_subject_collection_path(project)
        if columns:
            data = self.client.get_json(path, params={"columns": columns})
        else:
            data = self.client.get_json(path)
        return HierarchyService.extract_rows(data)

    def experiment_rows(self, project: str | None, subject: str) -> builtins.list[dict[str, Any]]:
        """Return raw experiment rows for a subject."""
        path = HierarchyService.build_experiment_collection_path(project, subject)
        return HierarchyService.extract_rows(self.client.get_json(path))

    def delete_raw(self, project: str, subject_id: str) -> httpx.Response:
        """DELETE a subject and return the raw response for status inspection."""
        return self.client.delete(
            f"/data/projects/{quote_path_segment(project)}"
            f"/subjects/{quote_path_segment(subject_id)}"
        )

    def rename_raw(self, project: str, label: str, new_label: str) -> httpx.Response:
        """PUT a new label on a subject and return the raw response.

        ``new_label`` is validated by the caller (see
        ``cli/subject.py::_validate_rename_target``) before it reaches here --
        it is CLI/patterns-file-derived text (regex substitution, template
        expansion), not a value XNAT has already round-tripped.
        """
        return self.client.put(
            f"/data/projects/{quote_path_segment(project)}/subjects/{quote_path_segment(label)}",
            params={"label": new_label},
        )

    # -------------------------------------------------------------------------
    # Custom variables (the "xnat-varput" surface)
    #
    # Verified live: a subject's custom fields are read from the
    # ``fields/field`` children of its ``format=json`` document, and written
    # via PUT query keys shaped
    # ``xnat:subjectData/fields/field[name=X]/field=value`` -- multiple keys
    # in one PUT set multiple variables atomically. These are open-ended,
    # project-defined fields (no fixed schema XNAT reports), so both methods
    # here work in plain dicts rather than a typed model, per the data-flow
    # rule in AGENTS.md.
    # -------------------------------------------------------------------------

    def list_vars(
        self, subject_id: str, project: str | None = None
    ) -> builtins.list[dict[str, str]]:
        """List a subject's custom variables.

        Args:
            subject_id: Subject ID or label (project required for a label).
            project: Project ID (required when ``subject_id`` is a label).

        Returns:
            One dict per variable, each shaped ``{"name": ..., "value": ...}``.
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}"

        try:
            data = self._get(path, params={"format": "json"})
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("subject", subject_id) from e
        children = HierarchyService.extract_item_children(data, "fields/field")
        return [
            {
                "name": HierarchyService.stringify_field(c.get("name")),
                "value": HierarchyService.stringify_field(c.get("field")),
            }
            for c in children
        ]

    def set_vars(
        self,
        subject_id: str,
        fields: dict[str, str],
        project: str | None = None,
    ) -> httpx.Response:
        """Set one or more custom variables on a subject in a single request.

        Each name is validated against a plain-identifier shape before
        building the request -- see :func:`_validate_field_name`.

        Args:
            subject_id: Subject ID or label (project required for a label).
            fields: Mapping of variable name -> value; creates a variable
                that doesn't exist yet, overwrites one that does.
            project: Project ID (required when ``subject_id`` is a label).

        Returns:
            The raw PUT response.

        Raises:
            InputValidationError: A field name is empty or contains a
                character (``[``, ``]``, ``=``, ``/``, ...) outside the
                allowed identifier shape.
            ResourceNotFoundError: ``subject_id`` does not exist.
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject_id)}"
            )
        else:
            path = f"/data/subjects/{quote_path_segment(subject_id)}"

        params = {
            f"xnat:subjectData/fields/field[name={_validate_field_name(name)}]/field": value
            for name, value in fields.items()
        }

        # Existence preflight. This PUT is the same create-or-update route
        # `create()` uses (a subject-scoped PUT with no body), and verified
        # live against XNAT 1.9.2.1, a nonexistent subject_id answers 201,
        # not 404 -- the PUT silently creates an empty subject instead of
        # failing. `get()` already 404s cleanly, so call it first to reject
        # a typo'd subject_id before any write is attempted.
        #
        # This narrows the window rather than closing it: a subject deleted
        # between this GET and the PUT below is still recreated empty, and
        # no client-side check can prevent that, because the write itself
        # carries no "must exist" semantics for the server to enforce. The
        # `except ResourceNotFoundError` below is NOT a guard against that
        # race -- the race ends in a 201, not a 404. It stays for the
        # project-scoped path, where a missing *project* does 404.
        self.get(subject_id, project=project)

        try:
            return self.client.put(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("subject", subject_id) from e
