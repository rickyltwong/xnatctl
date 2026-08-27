"""Prearchive commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    confirm_destructive,
    confirm_destructive_when,
    global_options,
    handle_errors,
    list_options,
    require_auth,
    require_project_from_context,
    resolve_columns,
)
from xnatctl.core.output import OutputFormat, print_output, print_success, print_warning
from xnatctl.core.validation import validate_project_id
from xnatctl.services.prearchive import (
    PREARCHIVE_CODE_TO_MODE,
    PREARCHIVE_MODE_TO_CODE,
    PrearchiveService,
)

_PREARCHIVE_MODES = tuple(PREARCHIVE_MODE_TO_CODE)


@click.group()
def prearchive() -> None:
    """Manage XNAT prearchive sessions."""
    pass


@prearchive.command("list")
@click.option("--project", help="Filter by project ID")
@list_options
@global_options
@handle_errors
@require_auth
def prearchive_list(
    ctx: Context,
    project: str | None,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List prearchive sessions.

    \b
    Example:
        xnatctl prearchive list
        xnatctl prearchive list --project MYPROJ
        xnatctl prearchive list --filter 'status:*error*' --sort-by scan_date:desc
    """
    client = ctx.get_client()
    service = PrearchiveService(client)
    sessions = service.list(project=project)

    sessions = apply_filter(sessions, filter_expr)
    sessions = apply_sort_limit(sessions, sort_by, limit)

    if ctx.quiet:
        for s in sessions:
            path = f"{s.get('project', '')}/{s.get('timestamp', '')}/{s.get('name', '')}"
            click.echo(path)
        return

    default_columns = ["project", "timestamp", "name", "status", "scan_date", "subject"]
    print_output(
        sessions,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        title="Prearchive Sessions",
    )


@prearchive.command("settings")
@click.option(
    "--project",
    "-P",
    "project_id",
    default=None,
    help="Project ID (falls back to profile default_project).",
)
@click.option(
    "--set",
    "set_mode",
    type=click.Choice(_PREARCHIVE_MODES),
    help="Set the project's prearchive routing mode",
)
@confirm_destructive_when(
    lambda kw: kw.get("set_mode") is not None,
    "Change this project's prearchive routing mode?",
)
@global_options
@handle_errors
@require_auth
def prearchive_settings(
    ctx: Context,
    project_id: str | None,
    set_mode: str | None,
    dry_run: bool,
) -> None:
    """Get or set a project's prearchive routing mode.

    XNAT's own ``PUT`` route accepts and silently stores any integer code --
    verified live against XNAT 1.9.2.1, ``PUT .../prearchive_code/3`` and
    ``.../9`` both answer 200 -- so MODE is always a readable name here,
    never a raw integer: ``manual``, ``auto-archive``, or
    ``auto-archive-overwrite``, mapping to the server's codes 0, 4, 5 (read
    out of ``org.nrg.framework.constants.PrearchiveCode``'s static
    initializer via ``javap`` against the running server's own
    ``framework-1.9.2.jar``). A typo is rejected before any request is
    sent, by ``click.Choice`` -- not by the server.

    The read form also copes with a project that already carries an
    out-of-enum code (however it got there): it shows the raw number and
    says plainly that it is not a recognized mode, rather than crashing or
    guessing which of the three it means.

    A 403 setting a non-manual mode is XNAT site policy
    (``project.allow-auto-archive`` disabled site-wide), not a permissions
    problem -- the error message says so explicitly.

    \b
    Example:
        xnatctl prearchive settings -P MYPROJ
        xnatctl prearchive settings -P MYPROJ --set auto-archive --yes
        xnatctl prearchive settings -P MYPROJ --set manual --dry-run
    """
    project = validate_project_id(require_project_from_context(ctx, project_id))
    service = PrearchiveService(ctx.get_client())

    if set_mode is None:
        code = service.get_routing_code(project)
        mode = PREARCHIVE_CODE_TO_MODE.get(code)
        if mode is None:
            print_warning(
                f"Project {project} has prearchive_code={code}, which is not a "
                "recognized mode (expected 0=manual, 4=auto-archive, "
                "5=auto-archive-overwrite)."
            )
        print_output(
            {"project": project, "code": code, "mode": mode or f"unrecognized ({code})"},
            format=ctx.output_format,
            quiet=ctx.quiet,
            id_field="project",
        )
        return

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would set {project} prearchive routing to {set_mode} "
            f"(code {PREARCHIVE_MODE_TO_CODE[set_mode]})",
            err=True,
        )
        return

    service.set_routing_mode(project, set_mode)
    print_success(f"Set {project} prearchive routing to {set_mode}")


@prearchive.command("archive")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.option("--subject", help="Target subject ID")
@click.option("--label", help="Target session label")
@click.option("--overwrite", is_flag=True, help="Overwrite existing data")
@global_options
@handle_errors
@require_auth
def prearchive_archive(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
    subject: str | None,
    label: str | None,
    overwrite: bool,
) -> None:
    """Archive a session from prearchive.

    \b
    Example:
        xnatctl prearchive archive MYPROJ 20240115_120000 Session1
        xnatctl prearchive archive MYPROJ 20240115_120000 Session1 --subject SUB001
    """
    client = ctx.get_client()
    service = PrearchiveService(client)

    result = service.archive(
        project=project,
        timestamp=timestamp,
        session_name=session_name,
        subject=subject,
        experiment_label=label,
        overwrite=overwrite,
    )

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    else:
        print_success(f"Archived {session_name} from prearchive")


@prearchive.command("delete")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@confirm_destructive("Delete session from prearchive? This cannot be undone.")
@global_options
@handle_errors
@require_auth
def prearchive_delete(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
    dry_run: bool,
) -> None:
    """Delete a session from prearchive.

    \b
    Example:
        xnatctl prearchive delete MYPROJ 20240115_120000 Session1 --yes
        xnatctl prearchive delete MYPROJ 20240115_120000 Session1 --dry-run
    """
    if dry_run:
        click.echo(f"[DRY-RUN] Would delete {session_name} from prearchive", err=True)
        return

    client = ctx.get_client()
    service = PrearchiveService(client)

    service.delete(
        project=project,
        timestamp=timestamp,
        session_name=session_name,
    )

    print_success(f"Deleted {session_name} from prearchive")


@prearchive.command("rebuild")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@global_options
@handle_errors
@require_auth
def prearchive_rebuild(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
) -> None:
    """Rebuild/refresh a prearchive session.

    \b
    Example:
        xnatctl prearchive rebuild MYPROJ 20240115_120000 Session1
    """
    client = ctx.get_client()
    service = PrearchiveService(client)

    service.rebuild(
        project=project,
        timestamp=timestamp,
        session_name=session_name,
    )

    print_success(f"Rebuilt {session_name}")


@prearchive.command("move")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.argument("target_project")
@global_options
@handle_errors
@require_auth
def prearchive_move(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
    target_project: str,
) -> None:
    """Move a prearchive session to another project.

    \b
    Example:
        xnatctl prearchive move MYPROJ 20240115_120000 Session1 OTHERPROJ
    """
    client = ctx.get_client()
    service = PrearchiveService(client)

    service.move(
        project=project,
        timestamp=timestamp,
        session_name=session_name,
        target_project=target_project,
    )

    print_success(f"Moved {session_name} to {target_project}")
