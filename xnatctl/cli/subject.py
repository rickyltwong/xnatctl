"""Subject commands for xnatctl."""

from __future__ import annotations

import re
from dataclasses import dataclass

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import print_error, print_output, print_success


@dataclass(frozen=True)
class _RenameRule:
    """A single subject rename rule loaded from a patterns JSON file."""

    project: str
    match: str
    to: str
    description: str | None = None


def _apply_template(*, template: str, project: str, groups: tuple[str | None, ...]) -> str:
    """Apply {project} and {1}/{2}/... substitutions to a template string."""

    target = template.replace("{project}", project)
    for i, g in enumerate(groups, start=1):
        target = target.replace(f"{{{i}}}", g or "")
    return target


def _load_patterns_file(
    *, path: str, project: str
) -> list[tuple[re.Pattern[str], str, str | None]]:
    """Load and compile rename rules for a single project.

    Returns:
        List of (compiled_regex, to_template, description) tuples.
    """
    import json

    from xnatctl.core.validation import validate_regex_pattern

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
@click.option("--filter", "filter_expr", help="Filter expression (e.g., 'label:SUB*')")
@global_options
@require_auth
@handle_errors
def subject_list(ctx: Context, project: str | None, filter_expr: str | None) -> None:
    """List subjects in a project.

    Example:
        xnatctl subject list --project MYPROJ
        xnatctl subject list -P MYPROJ -q  # IDs only
    """
    from xnatctl.core.validation import validate_project_id

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    client = ctx.get_client()

    # Get subjects
    resp = client.get_json(
        f"/data/projects/{project}/subjects",
        params={"columns": "ID,label,src"},
    )
    results = resp.get("ResultSet", {}).get("Result", [])

    # Transform for output
    subjects = []
    for r in results:
        label = r.get("label", "")

        # Apply filter if provided
        if filter_expr and ":" in filter_expr:
            field, pattern = filter_expr.split(":", 1)
            if field == "label":
                import fnmatch

                if not fnmatch.fnmatch(label, pattern):
                    continue

        subjects.append(
            {
                "id": r.get("ID", ""),
                "label": label,
            }
        )

    # Get session counts (if not too many subjects)
    if len(subjects) <= 50 and not ctx.quiet:
        for subj in subjects:
            try:
                sess_resp = client.get_json(
                    f"/data/projects/{project}/subjects/{subj['id']}/experiments"
                )
                subj["sessions"] = len(sess_resp.get("ResultSet", {}).get("Result", []))
            except Exception:
                subj["sessions"] = "?"

    print_output(
        subjects,
        format=ctx.output_format,
        columns=["id", "label", "sessions"] if not ctx.quiet else ["id", "label"],
        column_labels={"id": "ID", "label": "Label", "sessions": "Sessions"},
        quiet=ctx.quiet,
        id_field="label",
    )


@subject.command("show")
@click.argument("subject_id")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@global_options
@require_auth
@handle_errors
def subject_show(ctx: Context, subject_id: str, project: str | None) -> None:
    """Show subject details.

    Example:
        xnatctl subject show SUB001 --project MYPROJ
    """
    from xnatctl.core.validation import validate_project_id, validate_subject_id

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    subject_id = validate_subject_id(subject_id)
    client = ctx.get_client()

    # Get subject details
    resp = client.get_json(f"/data/projects/{project}/subjects/{subject_id}")
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1)

    subject_data = results[0]

    # Get sessions
    try:
        sess_resp = client.get_json(f"/data/projects/{project}/subjects/{subject_id}/experiments")
        sessions = sess_resp.get("ResultSet", {}).get("Result", [])
        session_labels = [s.get("label", s.get("ID", "")) for s in sessions]
    except Exception:
        session_labels = []

    output = {
        "id": subject_data.get("ID", ""),
        "label": subject_data.get("label", ""),
        "project": project,
        "session_count": len(session_labels),
        "sessions": session_labels[:10],  # Limit to first 10
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
@click.argument("subject_id")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@confirm_destructive("Delete this subject and all its sessions?")
@global_options
@require_auth
@handle_errors
def subject_delete(ctx: Context, subject_id: str, project: str | None, dry_run: bool) -> None:
    """Delete a subject.

    Example:
        xnatctl subject delete SUB001 --project MYPROJ
        xnatctl subject delete SUB001 -P MYPROJ --dry-run
    """
    from xnatctl.core.validation import validate_project_id, validate_subject_id

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    subject_id = validate_subject_id(subject_id)
    client = ctx.get_client()

    if dry_run:
        click.echo(f"Would delete subject: {subject_id} from project: {project}")
        return

    # Delete subject
    resp = client.delete(f"/data/projects/{project}/subjects/{subject_id}")

    if resp.status_code in (200, 204):
        print_success(f"Deleted subject: {subject_id}")
    else:
        print_error(f"Failed to delete subject: {resp.text}")
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
@require_auth
@handle_errors
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

    Examples:
        # Using mapping file
        xnatctl subject rename -P MYPROJ --mapping renames.json

        # Using pattern (merges SUB001_visit1, SUB001_visit2 into SUB001)
        xnatctl subject rename -P MYPROJ --pattern "^(\\w+)_visit\\d+$" --to "{1}"

        # Using a patterns file (rules are filtered by -P project)
        xnatctl subject rename -P MYPROJ --patterns-file patterns.json --dry-run
    """
    import json

    from xnatctl.core.validation import validate_project_id, validate_regex_pattern
    from xnatctl.services.subjects import SubjectService

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    client = ctx.get_client()

    if patterns_file:
        if mapping or pattern or to_template:
            print_error("Use only one of: --patterns-file, --mapping, or --pattern/--to")
            raise SystemExit(1)
    elif not mapping and not (pattern and to_template):
        print_error("Must provide --patterns-file, --mapping file, or --pattern with --to template")
        raise SystemExit(1)

    # Get current subjects
    resp = client.get_json(f"/data/projects/{project}/subjects")
    subjects = resp.get("ResultSet", {}).get("Result", [])
    current_labels = {s["label"] for s in subjects}

    renamed = {}
    merged = {}
    skipped = []

    if patterns_file:
        # Load per-project rules and apply the first matching rule per subject.
        try:
            rules = _load_patterns_file(path=patterns_file, project=project)
        except Exception as e:
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
                subject_svc = SubjectService(ctx.get_client())
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
                except Exception as e:
                    skipped.append((label, f"merge failed: {e}"))
            else:
                resp = client.put(
                    f"/data/projects/{project}/subjects/{label}",
                    params={"label": target},
                )
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
                    subject_svc = SubjectService(ctx.get_client())
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
                    except Exception as e:
                        skipped.append((old_label, f"merge failed: {e}"))
                else:
                    resp = client.put(
                        f"/data/projects/{project}/subjects/{old_label}",
                        params={"label": new_label},
                    )
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

            target_exists = target in current_labels

            if dry_run:
                if target_exists:
                    merged[label] = target
                else:
                    renamed[label] = target
            else:
                if target_exists:
                    # Merge: move all experiments from source to target
                    subject_svc = SubjectService(ctx.get_client())
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
                    except Exception as e:
                        skipped.append((label, f"merge failed: {e}"))
                else:
                    resp = client.put(
                        f"/data/projects/{project}/subjects/{label}",
                        params={"label": target},
                    )
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
