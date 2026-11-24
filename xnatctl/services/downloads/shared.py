"""Validation and experiment-reference helpers shared across the download mixins."""

from __future__ import annotations

from pathlib import Path

from xnatctl.core.exceptions import InputValidationError, PathValidationError
from xnatctl.core.validation import validate_local_path_component
from xnatctl.models.hierarchy import ExperimentRef

from ..base import BaseService
from ..hierarchy import HierarchyService


def _safe_output_path(output_dir: Path, filename: str | None, default: str) -> Path:
    if filename is not None:
        if filename.strip() == "":
            raise PathValidationError(
                filename, "output filename cannot be empty or whitespace-only"
            )
        for part in filename.split("/"):
            validate_local_path_component(part, "output filename component")
        effective_name = filename
    else:
        effective_name = default

    candidate = output_dir / effective_name
    resolved_dir = output_dir.resolve()
    if not candidate.resolve().is_relative_to(resolved_dir):
        raise PathValidationError(
            str(filename), "output filename must not escape the output directory"
        )
    return candidate


def _reject_empty_resource_filter_values(
    include_resources: tuple[str, ...], exclude_resources: tuple[str, ...]
) -> None:
    for value in (*include_resources, *exclude_resources):
        if value.strip() == "":
            raise InputValidationError(
                "include_resources/exclude_resources cannot contain an empty or "
                "whitespace-only value",
                field="resource filter",
                value=value,
            )


class _HierarchyResolveMixin(BaseService):
    def _resolve_zip_experiment_ref(
        self,
        session_id: str,
        *,
        project: str | None = None,
        subject: str | None = None,
    ) -> ExperimentRef:
        if project is not None and not session_id.startswith("XNAT_E"):
            source_ref = ExperimentRef(
                experiment=session_id,
                project_id=project,
                subject=subject,
                experiment_is_label=True,
                subject_is_label=subject is not None,
            )
            resolved = HierarchyService.parse_resolved_experiment(
                source_ref,
                self._get(
                    HierarchyService.build_experiment_path(source_ref),
                    params={"format": "json"},
                ),
            )
            return ExperimentRef(experiment=resolved.experiment_id)

        return ExperimentRef(experiment=session_id)
