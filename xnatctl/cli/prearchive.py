"""Prearchive commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import Context, global_options, handle_errors, require_auth
from xnatctl.core.output import OutputFormat, print_output, print_success
from xnatctl.services.prearchive import PrearchiveService


@click.group()
def prearchive() -> None:
    """Manage XNAT prearchive sessions."""
    pass


@prearchive.command("list")
@click.option("--project", help="Filter by project ID")
@global_options
@require_auth
@handle_errors
def prearchive_list(
    ctx: Context,
    project: str | None,
) -> None:
    """List prearchive sessions.

    Example:
        xnatctl prearchive list
        xnatctl prearchive list --project MYPROJ
    """
    client = ctx.get_client()
    service = PrearchiveService(client)
    sessions = service.list(project=project)

    if ctx.quiet:
        for s in sessions:
            path = f"{s.get('project', '')}/{s.get('timestamp', '')}/{s.get('name', '')}"
            click.echo(path)
        return

    columns = ["project", "timestamp", "name", "status", "scan_date", "subject"]
    print_output(sessions, format=ctx.output_format, columns=columns, title="Prearchive Sessions")


@prearchive.command("archive")
@click.argument("project")
@click.argument("timestamp")
@click.argument("session_name")
@click.option("--subject", help="Target subject ID")
@click.option("--label", help="Target session label")
@click.option("--overwrite", is_flag=True, help="Overwrite existing data")
@global_options
@require_auth
@handle_errors
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
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@global_options
@require_auth
@handle_errors
def prearchive_delete(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
    yes: bool,
) -> None:
    """Delete a session from prearchive.

    Example:
        xnatctl prearchive delete MYPROJ 20240115_120000 Session1 --yes
    """
    if not yes:
        click.confirm(
            f"Delete {session_name} from prearchive? This cannot be undone.",
            abort=True,
        )

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
@require_auth
@handle_errors
def prearchive_rebuild(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
) -> None:
    """Rebuild/refresh a prearchive session.

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
@require_auth
@handle_errors
def prearchive_move(
    ctx: Context,
    project: str,
    timestamp: str,
    session_name: str,
    target_project: str,
) -> None:
    """Move a prearchive session to another project.

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
