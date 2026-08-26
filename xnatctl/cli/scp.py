"""DICOM SCP receiver commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.exceptions import ResourceExistsError
from xnatctl.core.output import print_output, print_success
from xnatctl.core.validation import validate_ae_title, validate_port
from xnatctl.services.scp import DicomScpService

_LIST_COLUMNS = ["id", "aeTitle", "port", "identifier", "enabled", "label"]
_LIST_LABELS = {
    "id": "ID",
    "aeTitle": "AE Title",
    "port": "Port",
    "identifier": "Identifier",
    "enabled": "Enabled",
    "label": "Label",
}


@click.group()
def scp() -> None:
    """Manage XNAT DICOM SCP receivers."""
    pass


@scp.command("list")
@global_options
@handle_errors
@require_auth
def scp_list(ctx: Context) -> None:
    """List registered DICOM SCP receivers.

    \b
    Example:
        xnatctl scp list
        xnatctl scp list -o json
        xnatctl scp list -q
    """
    service = DicomScpService(ctx.get_client())
    rows = service.list_scps()

    print_output(
        rows,
        format=ctx.output_format,
        columns=_LIST_COLUMNS,
        column_labels=_LIST_LABELS,
        quiet=ctx.quiet,
        id_field="id",
    )


@scp.command("show")
@click.argument("scp_id", type=int)
@global_options
@handle_errors
@require_auth
def scp_show(ctx: Context, scp_id: int) -> None:
    """Show one DICOM SCP receiver's full definition.

    \b
    Example:
        xnatctl scp show 1
        xnatctl scp show 1 -o json
    """
    service = DicomScpService(ctx.get_client())
    data = service.get_scp(scp_id)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@scp.command("create")
@click.option("--ae-title", required=True, help="DICOM AE title for the new receiver")
@click.option("--port", required=True, type=int, help="TCP port for the receiver to listen on")
@click.option(
    "--identifier",
    help=(
        "DICOM object identifier to use (see `xapi/dicomscp/identifiers`); "
        "defaults to the only one registered if there is exactly one"
    ),
)
@confirm_destructive("Create this DICOM SCP receiver?")
@global_options
@handle_errors
@require_auth
def scp_create(
    ctx: Context,
    ae_title: str,
    port: int,
    identifier: str | None,
    dry_run: bool,
) -> None:
    """Register a new DICOM SCP receiver.

    The server does not validate ``--port`` at all -- 0 and a port already
    bound by another receiver are both accepted silently (verified live) --
    so this command validates the range client-side and also checks the
    existing receivers for one already on the same port. Two receivers
    cannot independently bind one listening socket, so creating a second one
    on a used port silently breaks whichever receiver loses the race, with
    XNAT reporting success either way.

    \b
    Example:
        xnatctl scp create --ae-title MYSCP --port 8105 --yes
        xnatctl scp create --ae-title MYSCP --port 8105 --identifier dicomObjectIdentifier --yes
        xnatctl scp create --ae-title MYSCP --port 8105 --dry-run
    """
    ae_title = validate_ae_title(ae_title)
    validated_port = validate_port(port)
    assert (
        validated_port is not None
    )  # port is a required option; allow_none=False never returns None
    port = validated_port

    service = DicomScpService(ctx.get_client())
    # Same lookup resolve_identifier() would run inside create_scp() -- a
    # dry run must reject an unknown/ambiguous --identifier exactly as
    # execution would, not silently accept it and skip the check.
    resolved_identifier = service.resolve_identifier(identifier)

    # The server accepts a duplicate port silently (see docstring) -- check
    # for a collision ourselves so a dry run catches it too, not just a real
    # create.
    conflict = next((r for r in service.list_scps() if r.get("port") == port), None)
    if conflict is not None:
        raise ResourceExistsError(
            "DICOM SCP receiver on port",
            f"{port} (existing receiver: aeTitle={conflict.get('aeTitle')!r}, "
            f"id={conflict.get('id')})",
        )

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would create DICOM SCP receiver aeTitle={ae_title!r} "
            f"port={port} identifier={resolved_identifier!r}",
            err=True,
        )
        return

    created = service.create_scp(ae_title, port, resolved_identifier)
    print_success(f"Created DICOM SCP receiver {created.get('id')} ({ae_title}:{port})")
    print_output(created, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@scp.command("delete")
@click.argument("scp_id", type=int)
@confirm_destructive("Delete this DICOM SCP receiver?")
@global_options
@handle_errors
@require_auth
def scp_delete(ctx: Context, scp_id: int, dry_run: bool) -> None:
    """Delete a DICOM SCP receiver.

    \b
    Example:
        xnatctl scp delete 2 --yes
        xnatctl scp delete 2 --dry-run
    """
    service = DicomScpService(ctx.get_client())

    if dry_run:
        # Execution's own DELETE already 404s cleanly on an unknown id (see
        # DicomScpService.delete_scp), but dry-run must not mutate to find
        # that out -- this runs the identical existence check as a plain read.
        service.get_scp(scp_id)
        click.echo(f"[DRY-RUN] Would delete DICOM SCP receiver {scp_id}", err=True)
        return

    service.delete_scp(scp_id)
    print_success(f"Deleted DICOM SCP receiver {scp_id}")


@scp.command("enable")
@click.argument("scp_id", type=int)
@confirm_destructive("Enable this DICOM SCP receiver?")
@global_options
@handle_errors
@require_auth
def scp_enable(ctx: Context, scp_id: int, dry_run: bool) -> None:
    """Enable a DICOM SCP receiver.

    \b
    Example:
        xnatctl scp enable 2 --yes
        xnatctl scp enable 2 --dry-run
    """
    service = DicomScpService(ctx.get_client())

    if dry_run:
        # Same preflight set_enabled() runs before its PUT (a nonexistent id
        # answers a raw 500 there, not a clean 404 -- see DicomScpService).
        service.get_scp(scp_id)
        click.echo(f"[DRY-RUN] Would enable DICOM SCP receiver {scp_id}", err=True)
        return

    service.set_enabled(scp_id, True)
    print_success(f"Enabled DICOM SCP receiver {scp_id}")


@scp.command("disable")
@click.argument("scp_id", type=int)
@confirm_destructive("Disable this DICOM SCP receiver?")
@global_options
@handle_errors
@require_auth
def scp_disable(ctx: Context, scp_id: int, dry_run: bool) -> None:
    """Disable a DICOM SCP receiver.

    \b
    Example:
        xnatctl scp disable 2 --yes
        xnatctl scp disable 2 --dry-run
    """
    service = DicomScpService(ctx.get_client())

    if dry_run:
        service.get_scp(scp_id)
        click.echo(f"[DRY-RUN] Would disable DICOM SCP receiver {scp_id}", err=True)
        return

    service.set_enabled(scp_id, False)
    print_success(f"Disabled DICOM SCP receiver {scp_id}")
