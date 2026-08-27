"""Event Service `event` commands for xnatctl."""

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
from xnatctl.services.events import EventService

_LIST_COLUMNS = ["id", "name", "active", "event_type", "action-key"]
_LIST_LABELS = {
    "id": "ID",
    "name": "Name",
    "active": "Active",
    "event_type": "Event Type",
    "action-key": "Action",
}

_ACTION_COLUMNS = ["action-key", "display-name", "description"]
_ACTION_LABELS = {
    "action-key": "Action Key",
    "display-name": "Name",
    "description": "Description",
}

_EVENT_TYPE_COLUMNS = ["type", "display-name", "statuses", "event-scope"]
_EVENT_TYPE_LABELS = {
    "type": "Type",
    "display-name": "Name",
    "statuses": "Statuses",
    "event-scope": "Scope",
}


@click.group()
def event() -> None:
    """Manage XNAT Event Service subscriptions."""
    pass


@event.command("list")
@global_options
@handle_errors
@require_auth
def event_list(ctx: Context) -> None:
    """List event subscriptions.

    \b
    Example:
        xnatctl event list
        xnatctl event list -o json
        xnatctl event list -q
    """
    service = EventService(ctx.get_client())
    rows = service.list_subscriptions()

    for row in rows:
        event_filter = row.get("event-filter")
        row["event_type"] = (
            event_filter.get("event-type", "") if isinstance(event_filter, dict) else ""
        )

    print_output(
        rows,
        format=ctx.output_format,
        columns=_LIST_COLUMNS,
        column_labels=_LIST_LABELS,
        quiet=ctx.quiet,
        id_field="id",
    )


@event.command("show")
@click.argument("subscription_id", type=int)
@global_options
@handle_errors
@require_auth
def event_show(ctx: Context, subscription_id: int) -> None:
    """Show one event subscription's full definition.

    \b
    Example:
        xnatctl event show 1
        xnatctl event show 1 -o json
    """
    service = EventService(ctx.get_client())
    data = service.get_subscription(subscription_id)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@event.command("create")
@read_payload_argument
@confirm_destructive("Register this event subscription?")
@global_options
@handle_errors
@require_auth
def event_create(
    ctx: Context,
    payload: dict[str, Any],
    payload_source: str,
    dry_run: bool,
) -> None:
    """Register a new event subscription from a subscription definition FILE.

    FILE is a path to a subscription definition JSON object, or ``-`` to
    read it from stdin. Keys are kebab-case (``event-filter``,
    ``action-key``, ``act-as-event-user``) -- see ``event actions`` and
    ``event types`` for the valid ``action-key``/``event-filter.event-type``
    values. Server-validated: an unknown ``action-key`` or
    ``event-filter.event-type`` is rejected with a descriptive error. The
    server does NOT enforce an action's declared required attributes
    (verified live), so a subscription missing e.g. an Email Action's
    ``to``/``subject``/``body`` is created successfully but will not
    deliver -- check ``event actions`` for what each action expects before
    creating.

    \b
    Example:
        xnatctl event create subscription.json --yes
        cat subscription.json | xnatctl event create - --yes
        xnatctl event create subscription.json --dry-run
    """
    service = EventService(ctx.get_client())

    if dry_run:
        diff = json_diff({}, payload, label=payload_source)
        click.echo(
            f"[DRY-RUN] Would register a new event subscription from {payload_source}:", err=True
        )
        if diff:
            click.echo(diff, err=True)
        return

    subscription_id = service.create_subscription(payload)
    print_success(f"Registered event subscription {subscription_id} from {payload_source}")
    print_output({"id": subscription_id}, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@event.command("delete")
@click.argument("subscription_id", type=int)
@confirm_destructive("Delete this event subscription?")
@global_options
@handle_errors
@require_auth
def event_delete(ctx: Context, subscription_id: int, dry_run: bool) -> None:
    """Delete an event subscription.

    \b
    Example:
        xnatctl event delete 2 --yes
        xnatctl event delete 2 --dry-run
    """
    service = EventService(ctx.get_client())

    if dry_run:
        # Execution's own DELETE already 404s cleanly on an unknown id (see
        # EventService.delete_subscription), but dry-run must not mutate to
        # find that out -- this runs the identical existence check as a
        # plain read.
        service.get_subscription(subscription_id)
        click.echo(f"[DRY-RUN] Would delete event subscription {subscription_id}", err=True)
        return

    service.delete_subscription(subscription_id)
    print_success(f"Deleted event subscription {subscription_id}")


@event.command("enable")
@click.argument("subscription_id", type=int)
@confirm_destructive("Enable this event subscription?")
@global_options
@handle_errors
@require_auth
def event_enable(ctx: Context, subscription_id: int, dry_run: bool) -> None:
    """Activate (enable) an event subscription.

    \b
    Example:
        xnatctl event enable 1 --yes
        xnatctl event enable 1 --dry-run
    """
    service = EventService(ctx.get_client())

    if dry_run:
        # Same preflight activate_subscription() would otherwise 404 on
        # (see EventService) -- run it as a plain read here instead.
        service.get_subscription(subscription_id)
        click.echo(f"[DRY-RUN] Would enable event subscription {subscription_id}", err=True)
        return

    service.activate_subscription(subscription_id)
    print_success(f"Enabled event subscription {subscription_id}")


@event.command("disable")
@click.argument("subscription_id", type=int)
@confirm_destructive("Disable this event subscription?")
@global_options
@handle_errors
@require_auth
def event_disable(ctx: Context, subscription_id: int, dry_run: bool) -> None:
    """Deactivate (disable) an event subscription.

    \b
    Example:
        xnatctl event disable 1 --yes
        xnatctl event disable 1 --dry-run
    """
    service = EventService(ctx.get_client())

    if dry_run:
        service.get_subscription(subscription_id)
        click.echo(f"[DRY-RUN] Would disable event subscription {subscription_id}", err=True)
        return

    service.deactivate_subscription(subscription_id)
    print_success(f"Disabled event subscription {subscription_id}")


@event.command("actions")
@global_options
@handle_errors
@require_auth
def event_actions(ctx: Context) -> None:
    """List available Event Service actions (valid `--action-key` values).

    \b
    Example:
        xnatctl event actions
        xnatctl event actions -o json
    """
    service = EventService(ctx.get_client())
    rows = service.list_actions()

    print_output(
        rows,
        format=ctx.output_format,
        columns=_ACTION_COLUMNS,
        column_labels=_ACTION_LABELS,
        quiet=ctx.quiet,
        id_field="action-key",
    )


@event.command("types")
@global_options
@handle_errors
@require_auth
def event_types(ctx: Context) -> None:
    """List available Event Service trigger types (valid `event-filter.event-type` values).

    \b
    Example:
        xnatctl event types
        xnatctl event types -o json
    """
    service = EventService(ctx.get_client())
    rows = service.list_event_types()

    print_output(
        rows,
        format=ctx.output_format,
        columns=_EVENT_TYPE_COLUMNS,
        column_labels=_EVENT_TYPE_LABELS,
        quiet=ctx.quiet,
        id_field="type",
    )
