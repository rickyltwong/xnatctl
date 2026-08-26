"""Subject commands for xnatctl."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _RenameRule:
    """A single subject rename rule loaded from a patterns JSON file."""

    project: str
    match: str
    to: str
    description: str | None = None


def _targets_field(expr: str | None, field: str) -> bool:
    """Whether a ``--filter``/``--sort-by`` expression names *field*.

    A lightweight pre-check (field name only, no validation of the rest of
    the expression) used to decide filter/enrichment ORDER in ``subject
    list`` before the real, validating :func:`apply_filter`/
    :func:`apply_sort_limit` run.
    """
    if not expr:
        return False
    return expr.split(":", 1)[0].strip() == field


def _apply_template(*, template: str, project: str, groups: tuple[str | None, ...]) -> str:
    """Apply {project} and {1}/{2}/... substitutions to a template string."""
    target = template.replace("{project}", project)
    for i, g in enumerate(groups, start=1):
        target = target.replace(f"{{{i}}}", g or "")
    return target


def _validate_rename_target(target: str) -> str | None:
    """Reject a rename target that is not a legal XNAT label.

    Every rename target here is derived text -- a regex substitution, a
    template expansion, or a raw value from a user-supplied mapping/patterns
    JSON file -- none of it XNAT has validated yet. Returns an error message
    if invalid, or ``None`` if the target is safe to use.
    """
    try:
        validate_xnat_label(target, "subject label")
    except InvalidIdentifierError as e:
        return str(e)
    return None


def _projects_in_patterns_file(path: str) -> set[str]:
    """Extract the unique project IDs referenced by a patterns JSON file."""
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
    """Load and compile rename rules for a single project.

    Returns:
        List of (compiled_regex, to_template, description) tuples.
    """
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

    # Get subjects
    results = service.list_rows(project, columns="ID,label,src")

    # Transform for output
    subjects = [{"id": r.get("ID", ""), "label": r.get("label", "")} for r in results]

    # A --filter targeting "sessions" cannot run on id/label alone -- there
    # is nothing to narrow the working set by until enrichment (below) has
    # happened, so it is deferred. Everything else narrows NOW, regardless
    # of what --sort-by (a separate option) happens to target -- a --filter
    # on id/label must narrow the set before the enrichment cap is checked
    # even when --sort-by is what's asking for "sessions".
    filter_targets_sessions = _targets_field(filter_expr, "sessions")
    sort_targets_sessions = _targets_field(sort_by, "sessions")
    needs_sessions_for_controls = filter_targets_sessions or sort_targets_sessions
    if not filter_targets_sessions:
        subjects = apply_filter(subjects, filter_expr)

    # Get session counts (if not too many subjects, after the narrowing
    # above). Quiet mode never computes it (its output is id/label lines
    # only), which is an independent reason to skip enrichment from the
    # size cap below -- conflating the two would misreport "too many
    # subjects" for a small, quiet-mode project that was never going to get
    # the column anyway.
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
            except Exception:  # noqa: BLE001  # per-subject isolation while enriching list with session counts
                subj["sessions"] = "?"
        enriched = True
    elif needs_sessions_for_controls:
        raise click.UsageError(
            f"--filter/--sort-by 'sessions' needs the project's subject count to be "
            f"50 or fewer after any other filtering (currently {len(subjects)}). "
            "Narrow further with --filter on id/label, or use 'subject show' per-subject."
        )
    else:
        # Neither control needs it, but the project is still too big to
        # enrich -- say so once rather than rendering silent blanks for a
        # column the caller never explicitly asked to filter/sort by.
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

    # Get subject details
    try:
        resolved = hierarchy.resolve_subject(SubjectRef(subject=subject_id, project_id=project))
    except ResourceNotFoundError:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1) from None

    # Get sessions
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
        "sessions": session_labels[:10],  # Limit to first 10
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
    # `batch_ids is not None` is presence ("--batch was given"), not
    # truthiness of the parsed list -- @batch_option already refuses an
    # empty-but-given batch, but these checks stay presence-based rather
    # than relying on that guarantee, so a positional SUBJECT_ID can never
    # slip past mutual exclusion just because the batch happened to be empty.
    if subject_id and batch_ids is not None:
        raise click.UsageError("provide SUBJECT_ID or --batch, not both")
    ids = batch_ids if batch_ids is not None else ([subject_id] if subject_id else None)
    if not ids:
        raise click.UsageError("provide SUBJECT_ID or --batch")

    project = validate_project_id(require_project_from_context(ctx, project))
    # Validate every ID up front, before any mutation: a malformed ID
    # anywhere in the batch must abort the whole command, not surface only
    # after earlier IDs in the same batch have already been deleted.
    validated_ids = [validate_subject_id(raw_id) for raw_id in ids]
    service = SubjectService(ctx.get_client())

    # Every non-2xx status arrives here as a typed exception, not as a
    # response to inspect: the client layer raises on 404/403/5xx and only
    # ever returns 2xx. So one bad ID in the middle of a list has to be
    # caught per subject, otherwise it aborts the run and silently leaves
    # every later ID untouched. The status check below still stands for the
    # 2xx-but-not-200/204 case.
    #
    # Isolation applies only when there IS a rest of the list. With a single
    # subject the exception propagates to @handle_errors, which renders the
    # real cause ("Permission denied on SUB001") and records that class in
    # the audit trail -- swallowing it here would replace both with a
    # generic failure count.
    isolate_failures = len(validated_ids) > 1
    failed: list[tuple[str, str]] = []
    for sid in validated_ids:
        if dry_run:
            click.echo(f"Would delete subject: {sid} from project: {project}", err=True)
            continue

        try:
            resp = service.delete_raw(project, sid)
        except Exception as e:  # noqa: BLE001  # per-subject isolation across a multi-subject list
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
def subject_rename(  # noqa: C901  # pre-existing; see pyproject
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

    project = validate_project_id(require_project_from_context(ctx, project))
    subject_svc = SubjectService(ctx.get_client())

    if patterns_file:
        if mapping or pattern or to_template:
            print_error("Use only one of: --patterns-file, --mapping, or --pattern/--to")
            raise SystemExit(1)
    elif not mapping and not (pattern and to_template):
        print_error("Must provide --patterns-file, --mapping file, or --pattern with --to template")
        raise SystemExit(1)

    # Get current subjects
    subjects = subject_svc.list_rows(project)
    current_labels = {s["label"] for s in subjects}

    renamed = {}
    merged = {}
    skipped = []

    if patterns_file:
        # Load per-project rules and apply the first matching rule per subject.
        try:
            rules = _load_patterns_file(path=patterns_file, project=project)
        except (OSError, json.JSONDecodeError, ValueError, ConfigurationError) as e:
            print_error(f"Failed to load patterns file: {e}")
            raise SystemExit(1) from e

        for subj in subjects:
            label = subj.get("label")
            if not isinstance(label, str) or not label:
                continue
            if label not in current_labels:
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

            target = _apply_template(template=matched_to, project=project, groups=match.groups())
            if target == label:
                skipped.append((label, "already matches"))
                continue

            invalid_reason = _validate_rename_target(target)
            if invalid_reason is not None:
                skipped.append((label, f"invalid target label: {invalid_reason}"))
                continue

            target_exists = target in current_labels
            if dry_run:
                if target_exists:
                    merged[label] = target
                else:
                    renamed[label] = target
                if matched_desc and not ctx.quiet:
                    click.echo(f"  Rule: {matched_desc}")
                continue

            if target_exists:
                try:
                    result = subject_svc.merge_subjects(
                        project=project,
                        source_label=label,
                        target_label=target,
                        dry_run=False,
                    )
                    merged[label] = target
                    current_labels.discard(label)
                    if not ctx.quiet:
                        desc = f" ({matched_desc})" if matched_desc else ""
                        click.echo(
                            f"  Merged {label} -> {target} ({result['experiments_moved']} experiments){desc}"
                        )
                except Exception as e:  # noqa: BLE001  # per-subject isolation in a patterns/mapping merge loop
                    skipped.append((label, f"merge failed: {e}"))
            else:
                resp = subject_svc.rename_raw(project, label, target)
                if resp.status_code == 200:
                    renamed[label] = target
                    current_labels.discard(label)
                    current_labels.add(target)
                    if matched_desc and not ctx.quiet:
                        click.echo(f"  Renamed {label} -> {target} ({matched_desc})")
                else:
                    skipped.append((label, f"failed: {resp.status_code}"))

    elif mapping:
        # Load mapping from file
        with open(mapping) as f:
            rename_map = json.load(f)

        for old_label, new_label in rename_map.items():
            if old_label not in current_labels:
                skipped.append((old_label, "not found"))
                continue

            if old_label == new_label:
                skipped.append((old_label, "same label"))
                continue

            if not isinstance(new_label, str):
                skipped.append((old_label, "invalid target label: must be a string"))
                continue

            invalid_reason = _validate_rename_target(new_label)
            if invalid_reason is not None:
                skipped.append((old_label, f"invalid target label: {invalid_reason}"))
                continue

            target_exists = new_label in current_labels

            if dry_run:
                if target_exists:
                    merged[old_label] = new_label
                else:
                    renamed[old_label] = new_label
            else:
                # Execute rename/merge
                if target_exists:
                    # Merge: move all experiments from source to target
                    try:
                        result = subject_svc.merge_subjects(
                            project=project,
                            source_label=old_label,
                            target_label=new_label,
                            dry_run=False,
                        )
                        merged[old_label] = new_label
                        current_labels.discard(old_label)
                        if not ctx.quiet:
                            click.echo(
                                f"  Merged {old_label} -> {new_label} ({result['experiments_moved']} experiments)"
                            )
                    except Exception as e:  # noqa: BLE001  # per-subject isolation in a patterns/mapping merge loop
                        skipped.append((old_label, f"merge failed: {e}"))
                else:
                    resp = subject_svc.rename_raw(project, old_label, new_label)
                    if resp.status_code == 200:
                        renamed[old_label] = new_label
                        current_labels.discard(old_label)
                        current_labels.add(new_label)
                    else:
                        skipped.append((old_label, f"failed: {resp.status_code}"))

    elif pattern and to_template:
        # Pattern-based rename
        regex = validate_regex_pattern(pattern)

        for subj in subjects:
            label = subj["label"]
            match = regex.fullmatch(label)
            if not match:
                continue

            # Build target name from template
            groups = match.groups()
            target = to_template.replace("{project}", project)
            for i, g in enumerate(groups, start=1):
                target = target.replace(f"{{{i}}}", g or "")

            if target == label:
                skipped.append((label, "already matches"))
                continue

            invalid_reason = _validate_rename_target(target)
            if invalid_reason is not None:
                skipped.append((label, f"invalid target label: {invalid_reason}"))
                continue

            target_exists = target in current_labels

            if dry_run:
                if target_exists:
                    merged[label] = target
                else:
                    renamed[label] = target
            else:
                if target_exists:
                    # Merge: move all experiments from source to target
                    try:
                        result = subject_svc.merge_subjects(
                            project=project,
                            source_label=label,
                            target_label=target,
                            dry_run=False,
                        )
                        merged[label] = target
                        current_labels.discard(label)
                        if not ctx.quiet:
                            click.echo(
                                f"  Merged {label} -> {target} ({result['experiments_moved']} experiments)"
                            )
                    except Exception as e:  # noqa: BLE001  # per-subject isolation in a patterns/mapping merge loop
                        skipped.append((label, f"merge failed: {e}"))
                else:
                    resp = subject_svc.rename_raw(project, label, target)
                    if resp.status_code == 200:
                        renamed[label] = target
                        current_labels.discard(label)
                        current_labels.add(target)
                    else:
                        skipped.append((label, f"failed: {resp.status_code}"))

    # Output results
    prefix = "[DRY-RUN] " if dry_run else ""

    if renamed:
        click.echo(f"\n{prefix}Renamed ({len(renamed)}):")
        for old, new in renamed.items():
            click.echo(f"  {old} -> {new}")

    if merged:
        click.echo(f"\n{prefix}Merged ({len(merged)}):")
        for old, new in merged.items():
            click.echo(f"  {old} -> {new}")

    if skipped:
        click.echo(f"\nSkipped ({len(skipped)}):")
        for label, reason in skipped:
            click.echo(f"  {label}: {reason}")

    if not dry_run:
        print_success(f"Renamed: {len(renamed)}, Merged: {len(merged)}, Skipped: {len(skipped)}")
