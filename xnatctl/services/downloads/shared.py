"""Validation and experiment-reference helpers shared across the download mixins."""

from __future__ import annotations

from pathlib import Path

from xnatctl.core.exceptions import InputValidationError, PathValidationError
from xnatctl.core.validation import validate_local_path_component
from xnatctl.models.hierarchy import ExperimentRef

from ..base import BaseService
from ..hierarchy import HierarchyService


def _safe_output_path(output_dir: Path, filename: str | None, default: str) -> Path:
    """Resolve a caller-supplied output filename, rejecting path traversal.

    ``zip_filename`` on :meth:`~xnatctl.services.downloads.resource_scan.
    _ResourceScanDownloadMixin.download_resource` and ``.download_scans`` is
    an optional override -- the CLI never exposes it as a flag, but the
    library surface does, so nothing upstream has validated it. ``None``
    means "use the default name" and is the real omitted case; an explicitly-
    supplied empty/whitespace-only STRING is a caller mistake and must not be
    silently substituted with ``default`` the same way.

    A subdirectory-bearing value (``"sub/dir.zip"``) is legal -- XNAT and
    the CLI both use ``/`` for this regardless of host OS -- but each ``/``-
    separated component is validated on its own via
    :func:`~xnatctl.core.validation.validate_local_path_component`, the same
    check every other identifier-derived local path piece in this module
    goes through. That also means a leading or trailing ``/`` (an absolute
    path, or a stray empty component) is rejected: it splits out an empty
    string component, which the component validator already refuses.
    Joining the whole thing onto ``output_dir`` unchecked would otherwise let
    a caller (or anything that echoes attacker-controlled text into it)
    escape the output directory with a name like ``../../etc/cron.d/x``, or
    -- component-by-component checking aside -- reach Windows path handling
    unvalidated the way a single-component value already does elsewhere.

    Raises:
        PathValidationError: If ``filename`` is supplied but empty or
            whitespace-only, if any ``/``-separated component fails
            :func:`~xnatctl.core.validation.validate_local_path_component`,
            or if the resolved result escapes ``output_dir``.
    """
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
    """Reject an empty/whitespace-only element in a resource include/exclude filter.

    An EMPTY tuple (the default) legitimately means "no filter" -- but an
    empty STRING inside a non-empty tuple is a different thing, and every
    caller downstream checks the tuple's truthiness (``if include_resources:``)
    or a resource label's truthiness (``if resource_label:``) to decide
    in/unfiltered scope. A stray ``""`` element would satisfy the tuple
    truthiness check (turning ON the include-filter branch) while never
    matching a real resource label -- either silently narrowing to zero
    results (nothing is ever labelled ``""``) or, worse, falling through a
    later per-item ``if resource_label:`` check into the UNFILTERED request
    for that one item. Neither is what the caller asked for, so this fails
    loudly instead.
    """
    for value in (*include_resources, *exclude_resources):
        if value.strip() == "":
            raise InputValidationError(
                "include_resources/exclude_resources cannot contain an empty or "
                "whitespace-only value",
                field="resource filter",
                value=value,
            )


class _HierarchyResolveMixin(BaseService):
    """Mixin resolving a session ID/label to a canonical experiment reference."""

    def _resolve_zip_experiment_ref(
        self,
        session_id: str,
        *,
        project: str | None = None,
        subject: str | None = None,
    ) -> ExperimentRef:
        """Resolve label-based experiment references to a canonical experiment ID."""
        # `is not None`, not truthy: `project=""` would skip a truthy check
        # entirely (treated the same as "no project"), silently using
        # session_id AS an accession ID without resolving it -- wrong if
        # it's actually a label. `is not None` routes "" into
        # ExperimentRef(project_id=""), which raises via the ref's own
        # validation instead.
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
