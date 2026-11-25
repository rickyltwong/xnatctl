"""Subject commands for xnatctl."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    batch_option,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    list_options,
    require_auth,
    require_project_from_context,
    resolve_columns,
)
from xnatctl.core.exceptions import (
    ConfigurationError,
    InvalidIdentifierError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.core.logging import FILE_ONLY_ATTR
from xnatctl.core.output import print_error, print_output, print_success
from xnatctl.core.validation import (
    validate_project_id,
    validate_regex_pattern,
    validate_subject_id,
    validate_xnat_label,
)
from xnatctl.models.hierarchy import SubjectRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.subjects import SubjectService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RenameRule:
    project: str
    match: str
    to: str
    description: str | None = None


def _targets_field(expr: str | None, field: str) -> bool:
    if not expr:
        return False
    return expr.split(":", 1)[0].strip() == field


def _apply_template(*, template: str, project: str, groups: tuple[str | None, ...]) -> str:
    target = template.replace("{project}", project)
    for i, g in enumerate(groups, start=1):
        target = target.replace(f"{{{i}}}", g or "")
    return target


def _validate_rename_target(target: str) -> str | None:
    try:
        validate_xnat_label(target, "subject label")
    except InvalidIdentifierError as e:
        return str(e)
    return None


def _projects_in_patterns_file(path: str) -> set[str]:
    with open(path) as f:
        data = json.load(f)

    patterns = data.get("patterns")
    if not isinstance(patterns, list):
        return set()

    projects: set[str] = set()
    for raw in patterns:
        if not isinstance(raw, dict):
            continue
        proj = raw.get("project")
        if isinstance(proj, str):
            proj = proj.strip()
            if proj:
                projects.add(proj)

    return projects


def _load_patterns_file(
    *, path: str, project: str
) -> list[tuple[re.Pattern[str], str, str | None]]:
    with open(path) as f:
        data = json.load(f)

    patterns = data.get("patterns")
    if not isinstance(patterns, list):
        raise ValueError("patterns file must contain a top-level 'patterns' list")

    compiled: list[tuple[re.Pattern[str], str, str | None]] = []
    for raw in patterns:
        if not isinstance(raw, dict):
            continue
        rule = _RenameRule(
            project=str(raw.get("project", "")),
            match=str(raw.get("match", "")),
            to=str(raw.get("to", "")),
            description=raw.get("description"),
        )
        if not rule.project or not rule.match or not rule.to:
            continue
        if rule.project != project:
            continue
        regex = validate_regex_pattern(rule.match)
        compiled.append((regex, rule.to, rule.description))

    if not compiled:
        raise ValueError(f"no patterns found for project '{project}'")

    return compiled


def _rename_project(ctx: Context, project: str | None, patterns_file: str | None) -> str:
    if not project:
        project = default_project_from_context(ctx)
        if not project and patterns_file:
            try:
                projects = _projects_in_patterns_file(patterns_file)
            except (OSError, json.JSONDecodeError) as e:
                raise click.ClickException(f"Failed to read patterns file: {e}") from e

            if len(projects) == 1:
                project = next(iter(projects))
            elif len(projects) > 1:
                profile_name = ctx.profile_name or (
                    ctx.config.default_profile if ctx.config else "default"
                )
                raise click.ClickException(
                    "Project required. Pass --project/-P or set default_project in profile "
                    f"'{profile_name}'. Patterns file contains multiple projects: {', '.join(sorted(projects))}"
                )
        project = require_project_from_context(ctx, project)

    return validate_project_id(require_project_from_context(ctx, project))


@dataclass
class _RenameRun:
    """One ``subject rename`` run; ``current_labels`` follows the project as subjects change."""

    service: SubjectService
    project: str
    dry_run: bool
    quiet: bool
    current_labels: set[str]
    renamed: dict[str, str] = field(default_factory=dict)
    merged: dict[str, str] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def apply(self, label: str, target: str, description: str | None = None) -> None:
        invalid_reason = _validate_rename_target(target)
        if invalid_reason is not None:
            self.skipped.append((label, f"invalid target label: {invalid_reason}"))
            return

        target_exists = target in self.current_labels
        if self.dry_run:
            if target_exists:
                self.merged[label] = target
            else:
                self.renamed[label] = target
            if description and not self.quiet:
                click.echo(f"  Rule: {description}")
            return

        if target_exists:
            self._merge(label, target, description)
        else:
            self._rename(label, target, description)

    def _merge(self, label: str, target: str, description: str | None) -> None:
        try:
            result = self.service.merge_subjects(
                project=self.project,
                source_label=label,
                target_label=target,
                dry_run=False,
            )
            self.merged[label] = target
            self.current_labels.discard(label)
            if not self.quiet:
                desc = f" ({description})" if description else ""
                click.echo(
                    f"  Merged {label} -> {target} ({result['experiments_moved']} experiments){desc}"
                )
        except Exception as e:
            logger.exception(
                "Merging subject %s into %s failed", label, target, extra={FILE_ONLY_ATTR: True}
            )
            self.skipped.append((label, f"merge failed: {e}"))

    def _rename(self, label: str, target: str, description: str | None) -> None:
        resp = self.service.rename_raw(self.project, label, target)
        if resp.status_code == 200:
            self.renamed[label] = target
            self.current_labels.discard(label)
            self.current_labels.add(target)
            if description and not self.quiet:
                click.echo(f"  Renamed {label} -> {target} ({description})")
        else:
            self.skipped.append((label, f"failed: {resp.status_code}"))


def _rename_by_patterns_file(
    run: _RenameRun, subjects: list[dict[str, Any]], patterns_file: str
) -> None:
    try:
        rules = _load_patterns_file(path=patterns_file, project=run.project)
    except (OSError, json.JSONDecodeError, ValueError, ConfigurationError) as e:
        print_error(f"Failed to load patterns file: {e}")
        raise SystemExit(1) from e

    for subj in subjects:
        label = subj.get("label")
        if not isinstance(label, str) or not label:
            continue
        if label not in run.current_labels:
            continue

        match = None
        matched_to = None
        matched_desc = None
        for regex, to_tmpl, desc in rules:
            m = regex.fullmatch(label)
            if m:
                match = m
                matched_to = to_tmpl
                matched_desc = desc
                break

        if not match or not matched_to:
            continue

        target = _apply_template(template=matched_to, project=run.project, groups=match.groups())
        if target == label:
            run.skipped.append((label, "already matches"))
            continue
        run.apply(label, target, matched_desc)


def _rename_by_mapping(run: _RenameRun, mapping: str) -> None:
    with open(mapping) as f:
        rename_map = json.load(f)

    for old_label, new_label in rename_map.items():
        if old_label not in run.current_labels:
            run.skipped.append((old_label, "not found"))
            continue

        if old_label == new_label:
            run.skipped.append((old_label, "same label"))
            continue

        if not isinstance(new_label, str):
            run.skipped.append((old_label, "invalid target label: must be a string"))
            continue

        run.apply(old_label, new_label)


def _rename_by_pattern(
    run: _RenameRun, subjects: list[dict[str, Any]], pattern: str, to_template: str
) -> None:
    regex = validate_regex_pattern(pattern)

    for subj in subjects:
        label = subj["label"]
        match = regex.fullmatch(label)
        if not match:
            continue

        target = _apply_template(template=to_template, project=run.project, groups=match.groups())
        if target == label:
            run.skipped.append((label, "already matches"))
            continue
        run.apply(label, target)


def _print_rename_report(run: _RenameRun) -> None:
    prefix = "[DRY-RUN] " if run.dry_run else ""

    if run.renamed:
        click.echo(f"\n{prefix}Renamed ({len(run.renamed)}):")
        for old, new in run.renamed.items():
            click.echo(f"  {old} -> {new}")

    if run.merged:
        click.echo(f"\n{prefix}Merged ({len(run.merged)}):")
        for old, new in run.merged.items():
            click.echo(f"  {old} -> {new}")

    if run.skipped:
        click.echo(f"\nSkipped ({len(run.skipped)}):")
        for label, reason in run.skipped:
            click.echo(f"  {label}: {reason}")

    if not run.dry_run:
        print_success(
            f"Renamed: {len(run.renamed)}, Merged: {len(run.merged)}, Skipped: {len(run.skipped)}"
        )


@click.group()
def subject() -> None:
    """Manage XNAT subjects."""
    pass


@subject.command("list")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@list_options
@global_options
@handle_errors
@require_auth
def subject_list(
    ctx: Context,
    project: str | None,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List subjects in a project.

    \b
    The "sessions" column (a per-subject session count) is only computed
    for 50 or fewer subjects, to bound the extra per-subject API calls it
    costs. A --filter on id/label always narrows the working set BEFORE
    that cap is checked -- even when --sort-by targets "sessions" -- so a
    narrow --filter still gets session counts on a large project. When the
    cap is exceeded and neither --filter nor --sort-by needs "sessions",
    the column is dropped (not left blank) and a note goes to stderr;
    --filter/--sort-by ON "sessions" itself has nothing to narrow the
    project by first, so those raise an error instead if the count is still
    too big after any other filtering.

    \b
    Example:
        xnatctl subject list --project MYPROJ
        xnatctl subject list -P MYPROJ -q  # IDs only
        xnatctl subject list -P MYPROJ --filter 'label:SUB*' --sort-by label --limit 10
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    service = SubjectService(ctx.get_client())

    results = service.list_rows(project, columns="ID,label,src")

    subjects = [{"id": r.get("ID", ""), "label": r.get("label", "")} for r in results]

    filter_targets_sessions = _targets_field(filter_expr, "sessions")
    sort_targets_sessions = _targets_field(sort_by, "sessions")
    needs_sessions_for_controls = filter_targets_sessions or sort_targets_sessions
    if not filter_targets_sessions:
        subjects = apply_filter(subjects, filter_expr)

    enriched = False
    if ctx.quiet:
        if needs_sessions_for_controls:
            raise click.UsageError(
                "--filter/--sort-by 'sessions' is not available with --quiet -- "
                "the sessions column is never computed in quiet mode."
            )
    elif len(subjects) <= 50:
        for subj in subjects:
            try:
                subj["sessions"] = len(service.experiment_rows(project, subj["id"]))
            except (XNATCtlError, ValueError):
                subj["sessions"] = "?"
        enriched = True
    elif needs_sessions_for_controls:
        raise click.UsageError(
            f"--filter/--sort-by 'sessions' needs the project's subject count to be "
            f"50 or fewer after any other filtering (currently {len(subjects)}). "
            "Narrow further with --filter on id/label, or use 'subject show' per-subject."
        )
    else:
        click.echo(
            f"session counts omitted: project has {len(subjects)} subjects "
            "(> 50); narrow with --filter to see them",
            err=True,
        )

    if filter_targets_sessions:
        subjects = apply_filter(subjects, filter_expr)
    subjects = apply_sort_limit(subjects, sort_by, limit)

    if ctx.quiet:
        default_columns = ["id", "label"]
    else:
        default_columns = ["id", "label", "sessions"] if enriched else ["id", "label"]
    print_output(
        subjects,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        column_labels={"id": "ID", "label": "Label", "sessions": "Sessions"},
        quiet=ctx.quiet,
        id_field="label",
    )


@subject.command("show")
@click.argument("subject_id")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@global_options
@handle_errors
@require_auth
def subject_show(ctx: Context, subject_id: str, project: str | None) -> None:
    """Show subject details.

    \b
    Example:
        xnatctl subject show SUB001 --project MYPROJ
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    subject_id = validate_subject_id(subject_id)
    client = ctx.get_client()
    hierarchy = HierarchyService(client)
    service = SubjectService(client)

    try:
        resolved = hierarchy.resolve_subject(SubjectRef(subject=subject_id, project_id=project))
    except ResourceNotFoundError:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1) from None

    try:
        sessions = service.experiment_rows(project, resolved.subject_id)
        session_labels = [s.get("label", s.get("ID", "")) for s in sessions]
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list sessions: {exc}", err=True)
        session_labels = []

    # Projects this subject is shared into, beyond its primary one --
    # GET /data/subjects/{id}/projects returns every assigned project
    # (primary included), so the primary is filtered back out here.
    try:
        share_rows = service.list_shares(resolved.subject_id)
        shared_projects = [
            (
                f"{r.get('ID', '')} (as {r.get('label')})"
                if r.get("label") != resolved.subject_label
                else str(r.get("ID", ""))
            )
            for r in share_rows
            if r.get("ID") != (resolved.project_id or project)
        ]
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list shared projects: {exc}", err=True)
        shared_projects = []

    output = {
        "id": resolved.subject_id,
        "label": resolved.subject_label or subject_id,
        "project": resolved.project_id or project,
        "session_count": len(session_labels),
        "sessions": session_labels[:10],
        "shared_projects": shared_projects,
    }

    if len(session_labels) > 10:
        output["sessions_truncated"] = True

    print_output(
        output,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="label",
    )


@subject.command("delete")
@click.argument("subject_id", required=False)
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@batch_option
@confirm_destructive("Delete this subject and all its sessions?")
@global_options
@handle_errors
@require_auth
def subject_delete(
    ctx: Context,
    subject_id: str | None,
    project: str | None,
    batch_ids: list[str] | None,
    dry_run: bool,
) -> None:
    """Delete one or more subjects.

    \b
    Example:
        xnatctl subject delete SUB001 --project MYPROJ
        xnatctl subject delete SUB001 -P MYPROJ --dry-run
        xnatctl subject list -P MYPROJ -q | xnatctl subject delete -P MYPROJ --batch - --yes
    """
    if subject_id and batch_ids is not None:
        raise click.UsageError("provide SUBJECT_ID or --batch, not both")
    ids = batch_ids if batch_ids is not None else ([subject_id] if subject_id else None)
    if not ids:
        raise click.UsageError("provide SUBJECT_ID or --batch")

    project = validate_project_id(require_project_from_context(ctx, project))
    validated_ids = [validate_subject_id(raw_id) for raw_id in ids]
    service = SubjectService(ctx.get_client())

    isolate_failures = len(validated_ids) > 1
    failed: list[tuple[str, str]] = []
    for sid in validated_ids:
        if dry_run:
            click.echo(f"Would delete subject: {sid} from project: {project}", err=True)
            continue

        try:
            resp = service.delete_raw(project, sid)
        except Exception as e:
            if not isolate_failures:
                raise
            failed.append((sid, str(e)))
            continue

        if resp.status_code in (200, 204):
            print_success(f"Deleted subject: {sid}")
        else:
            failed.append((sid, resp.text))

    if failed:
        noun = "subject" if len(failed) == 1 else "subjects"
        print_error(f"Failed to delete {len(failed)} {noun}:")
        for sid, error in failed:
            click.echo(f"  - {sid}: {error}", err=True)
        raise SystemExit(1)


@subject.command("rename")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option(
    "--patterns-file",
    type=click.Path(exists=True),
    help="JSON file with per-project rename patterns (first match wins)",
)
@click.option(
    "--mapping", type=click.Path(exists=True), help="JSON file with old->new label mapping"
)
@click.option("--pattern", help="Regex pattern with capture groups")
@click.option("--to", "to_template", help="Template for new label (use {1}, {2} for groups)")
@click.option("--dry-run", is_flag=True, help="Preview changes without applying")
@global_options
@handle_errors
@require_auth
def subject_rename(
    ctx: Context,
    project: str | None,
    patterns_file: str | None,
    mapping: str | None,
    pattern: str | None,
    to_template: str | None,
    dry_run: bool,
) -> None:
    """Rename subjects using mapping file or pattern.

    Supports merging when target subject already exists.

    \b
    Examples:
        # Using mapping file
        xnatctl subject rename -P MYPROJ --mapping renames.json

        # Using pattern (merges SUB001_visit1, SUB001_visit2 into SUB001)
        xnatctl subject rename -P MYPROJ --pattern "^(\\w+)_visit\\d+$" --to "{1}"

        # Using a patterns file (rules are filtered by -P project)
        xnatctl subject rename -P MYPROJ --patterns-file patterns.json --dry-run
    """
    project = _rename_project(ctx, project, patterns_file)
    subject_svc = SubjectService(ctx.get_client())

    if patterns_file:
        if mapping or pattern or to_template:
            print_error("Use only one of: --patterns-file, --mapping, or --pattern/--to")
            raise SystemExit(1)
    elif not mapping and not (pattern and to_template):
        print_error("Must provide --patterns-file, --mapping file, or --pattern with --to template")
        raise SystemExit(1)

    subjects = subject_svc.list_rows(project)
    run = _RenameRun(
        service=subject_svc,
        project=project,
        dry_run=dry_run,
        quiet=ctx.quiet,
        current_labels={s["label"] for s in subjects},
    )

    if patterns_file:
        _rename_by_patterns_file(run, subjects, patterns_file)
    elif mapping:
        _rename_by_mapping(run, mapping)
    elif pattern and to_template:
        _rename_by_pattern(run, subjects, pattern, to_template)

    _print_rename_report(run)
