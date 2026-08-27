"""Command Service `wrapper` commands for xnatctl."""

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
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.core.output import OutputFormat, print_output, print_success
from xnatctl.services.commands import CommandService


@click.group()
def wrapper() -> None:
    """Manage XNAT Container Service command wrappers."""
    pass


@wrapper.command("list")
@click.option("--command", "command_id", type=int, help="Scope to one command's wrappers")
@global_options
@handle_errors
@require_auth
def wrapper_list(ctx: Context, command_id: int | None) -> None:
    """List command wrappers, derived client-side from each command's registration.

    There is no server-side wrapper-listing endpoint in this Container
    Service version, so this walks every registered command's embedded
    wrapper list (or one command's, with ``--command``). Wrappers carry no
    ``enabled`` flag in this plugin version -- none is shown.

    \b
    Example:
        xnatctl wrapper list
        xnatctl wrapper list --command 12
        xnatctl wrapper list -o json
    """
    service = CommandService(ctx.get_client())
    rows = service.list_wrappers(command_id=command_id)

    # Only table/TSV rendering may collapse the contexts list to a display
    # string -- a JSON consumer must get the real array back untouched.
    if ctx.output_format in (OutputFormat.TABLE, OutputFormat.TSV):
        rows = [
            dict(row, contexts=", ".join(str(c) for c in row["contexts"]))
            if isinstance(row.get("contexts"), list)
            else row
            for row in rows
        ]

    print_output(
        rows,
        format=ctx.output_format,
        columns=["id", "name", "command-id", "contexts"],
        column_labels={
            "id": "ID",
            "name": "Name",
            "command-id": "Command ID",
            "contexts": "Contexts",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


def _scope_label(project: str | None) -> str:
    """Human-readable description of an enable/disable/config-set scope."""
    return f"project {project}" if project is not None else "site-wide"


@wrapper.command("enable")
@click.argument("command_id", type=int)
@click.argument("wrapper_id", type=int)
@click.option("--project", "-P", help="Enable for this project instead of site-wide")
@confirm_destructive("Enable this wrapper?")
@global_options
@handle_errors
@require_auth
def wrapper_enable(
    ctx: Context,
    command_id: int,
    wrapper_id: int,
    project: str | None,
    dry_run: bool,
) -> None:
    """Enable a command wrapper, site- or project-scoped.

    \b
    Example:
        xnatctl wrapper enable 12 34 --yes
        xnatctl wrapper enable 12 34 -P MYPROJ --yes
        xnatctl wrapper enable 12 34 --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        # Same preflight `enable_wrapper` runs before its PUT -- checked
        # here too so `--dry-run` reports the same refusal execution would
        # for an unknown wrapper or, for a project-scoped call, an unknown
        # project (which the PUT itself would silently accept).
        service.check_wrapper_scope(command_id, wrapper_id, project=project)
        click.echo(
            f"[DRY-RUN] Would enable wrapper {wrapper_id} on command {command_id} "
            f"({_scope_label(project)})",
            err=True,
        )
        return

    service.enable_wrapper(command_id, wrapper_id, project=project)
    print_success(f"Enabled wrapper {wrapper_id} on command {command_id} ({_scope_label(project)})")


@wrapper.command("disable")
@click.argument("command_id", type=int)
@click.argument("wrapper_id", type=int)
@click.option("--project", "-P", help="Disable for this project instead of site-wide")
@confirm_destructive("Disable this wrapper?")
@global_options
@handle_errors
@require_auth
def wrapper_disable(
    ctx: Context,
    command_id: int,
    wrapper_id: int,
    project: str | None,
    dry_run: bool,
) -> None:
    """Disable a command wrapper, site- or project-scoped.

    \b
    Example:
        xnatctl wrapper disable 12 34 --yes
        xnatctl wrapper disable 12 34 -P MYPROJ --yes
        xnatctl wrapper disable 12 34 --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        # Same preflight `disable_wrapper` runs before its PUT -- checked
        # here too so `--dry-run` reports the same refusal execution would
        # for an unknown wrapper or, for a project-scoped call, an unknown
        # project (which the PUT itself would silently accept).
        service.check_wrapper_scope(command_id, wrapper_id, project=project)
        click.echo(
            f"[DRY-RUN] Would disable wrapper {wrapper_id} on command {command_id} "
            f"({_scope_label(project)})",
            err=True,
        )
        return

    service.disable_wrapper(command_id, wrapper_id, project=project)
    print_success(
        f"Disabled wrapper {wrapper_id} on command {command_id} ({_scope_label(project)})"
    )


@wrapper.group("config")
def wrapper_config() -> None:
    """Manage a wrapper's site- or project-scoped configuration."""
    pass


@wrapper_config.command("get")
@click.argument("wrapper_ref", metavar="WRAPPER")
@click.option("--project", "-P", help="Read the project-scoped configuration instead of site-wide")
@global_options
@handle_errors
@require_auth
def wrapper_config_get(
    ctx: Context,
    wrapper_ref: str,
    project: str | None,
) -> None:
    """Get a wrapper's configuration.

    WRAPPER is a numeric wrapper ID or a wrapper name. A name that matches
    wrappers on more than one command is rejected -- pass the numeric ID
    instead.

    \b
    Example:
        xnatctl wrapper config get 34
        xnatctl wrapper config get dcm2niix-scan
        xnatctl wrapper config get 34 -P MYPROJ
        xnatctl wrapper config get 34 -o json
    """
    service = CommandService(ctx.get_client())
    wrapper_id, _ = service.resolve_wrapper(wrapper_ref)
    data = service.get_wrapper_config(wrapper_id, project=project)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet)


@wrapper_config.command("set")
@click.argument("command_id", type=int)
@click.argument("wrapper_id", type=int)
@read_payload_argument
@click.option("--project", "-P", help="Set the project-scoped configuration instead of site-wide")
@confirm_destructive("Set this wrapper's configuration?")
@global_options
@handle_errors
@require_auth
def wrapper_config_set(
    ctx: Context,
    command_id: int,
    wrapper_id: int,
    payload: dict[str, Any],
    payload_source: str,
    project: str | None,
    dry_run: bool,
) -> None:
    """Set a wrapper's configuration from a JSON file.

    FILE is a path to a ``{"inputs": {...}, "outputs": {...}}`` document
    (the shape ``wrapper config get`` prints), or ``-`` to read it from
    stdin. This is a full replace of the configuration, the same as
    ``command update`` is for a command.

    \b
    Example:
        xnatctl wrapper config set 12 34 config.json --yes
        xnatctl wrapper config set 12 34 config.json -P MYPROJ --yes
        xnatctl wrapper config set 12 34 config.json --dry-run
    """
    service = CommandService(ctx.get_client())

    if dry_run:
        # Same preflight `set_wrapper_config` runs before its POST: confirms
        # the (command_id, wrapper_id) pair exists and reads the wrapper's
        # current enabled state. A config write silently re-enables a
        # disabled wrapper if `enable` is left to the server's default
        # (verified live -- see CommandService.set_wrapper_config), so the
        # preview must show the same enabled state execution will carry
        # forward, not just the config diff.
        enabled = service.check_wrapper_config_scope(command_id, wrapper_id, project=project)
        try:
            current = service.get_wrapper_config(wrapper_id, project=project)
        except ResourceNotFoundError:
            # No configuration exists for this wrapper yet -- execution
            # never reads current config at all, it just POSTs and the
            # server 201-creates. The preview must not fail where execution
            # succeeds, so a missing config renders as an all-additions
            # diff, the same convention `json_diff` already documents for
            # `command create`.
            current = {}
        diff = json_diff(current, payload, label=f"wrapper {wrapper_id} config")
        click.echo(
            f"[DRY-RUN] Would set wrapper {wrapper_id} configuration from "
            f"{payload_source} ({_scope_label(project)}); enabled state "
            f"({'enabled' if enabled else 'disabled'}) will be preserved:",
            err=True,
        )
        click.echo(diff if diff else "(no changes)", err=True)
        return

    service.set_wrapper_config(command_id, wrapper_id, payload, project=project)
    print_success(
        f"Set configuration for wrapper {wrapper_id} on command {command_id} "
        f"({_scope_label(project)})"
    )
