"""Command Service `command` commands for xnatctl.

Named ``command_cmd.py`` (not ``command.py``) to match the ``cli/config_cmd.py``
/ ``cli/dicom_cmd.py`` precedent -- the group itself is still named ``command``.
"""

from __future__ import annotations

from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.cli.payload import json_diff, read_payload_argument
from xnatctl.core.output import print_output, print_success
from xnatctl.services.commands import CommandService, wrappers_of


@click.group()
def command() -> None:
    """Manage XNAT Container Service commands."""
    pass


@command.command("list")
@global_options
@handle_errors
@require_auth
def command_list(ctx: Context) -> None:
    """List registered Container Service commands.

    \b
    Example:
        xnatctl command list
        xnatctl command list -o json
        xnatctl command list -q
    """
    service = CommandService(ctx.get_client())
    rows = service.list_commands()

    for row in rows:
        # Same reader the service uses, deliberately: `len(row.get("xnat", [])
        # or [])` would count a malformed shape as zero wrappers and print a
        # convincing, wrong, successful table.
        row["wrapper_count"] = len(wrappers_of(row))

    print_output(
        rows,
        format=ctx.output_format,
        columns=["id", "name", "image", "version", "wrapper_count"],
        column_labels={
            "id": "ID",
            "name": "Name",
            "image": "Image",
            "version": "Version",
            "wrapper_count": "Wrappers",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@command.command("show")
@click.argument("command_id", type=int)
@global_options
@handle_errors
@require_auth
def command_show(ctx: Context, command_id: int) -> None:
    """Show one command's full definition.

    \b
    Example:
        xnatctl command show 12
        xnatctl command show 12 -o json
    """
    service = CommandService(ctx.get_client())
    data = service.get_command(command_id)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@command.command("create")
@read_payload_argument
@confirm_destructive("Register this command?")
@global_options
@handle_errors
@require_auth
def command_create(
    ctx: Context,
    payload: dict[str, Any],
    payload_source: str,
    dry_run: bool,
) -> None:
    """Register a new Container Service command from a command.json file.

    FILE is a path to a command.json object, or ``-`` to read it from
    stdin. Server-validated: a malformed payload (e.g. a blank name or
    image) is rejected with a descriptive error.

    \b
    Example:
        xnatctl command create command.json --yes
        cat command.json | xnatctl command create - --yes
        xnatctl command create command.json --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        diff = json_diff({}, payload, label=payload_source)
        click.echo(f"[DRY-RUN] Would register a new command from {payload_source}:", err=True)
        if diff:
            click.echo(diff, err=True)
        return

    command_id = service.create_command(payload)
    print_success(f"Registered command {command_id} from {payload_source}")
    print_output({"id": command_id}, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@command.command("update")
@click.argument("command_id", type=int)
@read_payload_argument
@confirm_destructive("Replace this command's full definition?")
@global_options
@handle_errors
@require_auth
def command_update(
    ctx: Context,
    command_id: int,
    payload: dict[str, Any],
    payload_source: str,
    dry_run: bool,
) -> None:
    """Replace a command's full definition from a command.json file.

    FILE is a path to a command.json object, or ``-`` to read it from
    stdin. This is a FULL REPLACE, not a merge: omitting ``xnat`` from FILE
    wipes every wrapper registered on the command. To keep existing
    wrappers, include the command's current ``xnat`` array (from
    ``command show COMMAND_ID -o json``) in FILE.

    \b
    Example:
        xnatctl command update 12 command.json --yes
        xnatctl command update 12 command.json --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        # Diff against the SAME body update_command() will POST -- built by
        # the one shared helper both branches call, not the raw payload.
        # Execution strips stale wrapper ids (see prepare_update_body's
        # docstring); diffing the raw payload here would show a modified
        # wrapper keeping an id execution actually removes.
        current, body = service.prepare_update_body(command_id, payload)
        diff = json_diff(current, body, label=f"command {command_id}")
        click.echo(f"[DRY-RUN] Would replace command {command_id} from {payload_source}:", err=True)
        click.echo(diff if diff else "(no changes)", err=True)
        return

    service.update_command(command_id, payload)
    print_success(f"Updated command {command_id} from {payload_source}")


@command.command("delete")
@click.argument("command_id", type=int)
@confirm_destructive("Delete this command?")
@global_options
@handle_errors
@require_auth
def command_delete(ctx: Context, command_id: int, dry_run: bool) -> None:
    """Delete a Container Service command.

    \b
    Example:
        xnatctl command delete 12 --yes
        xnatctl command delete 12 --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        # Execution refuses an unknown command_id via delete_command()'s own
        # get_command() preflight (DELETE answers 204 even for a nonexistent
        # ID, so that preflight is the only thing standing between a typo
        # and a silent no-op). Dry-run must run the same check rather than
        # reporting success for a call execution would refuse.
        service.get_command(command_id)
        click.echo(f"[DRY-RUN] Would delete command {command_id}", err=True)
        return

    service.delete_command(command_id)
    print_success(f"Deleted command {command_id}")
