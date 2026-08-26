"""Saved-search commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    confirm_destructive,
    global_options,
    handle_errors,
    list_options,
    require_auth,
    resolve_columns,
)
from xnatctl.core.output import OutputFormat, print_json, print_output, print_success, print_table
from xnatctl.services.search import SearchService

_LIST_COLUMNS = ["id", "root_element_name", "brief_description", "secure", "users"]
_LIST_LABELS = {
    "id": "ID",
    "root_element_name": "Root Element",
    "brief_description": "Description",
    "secure": "Secure",
    "users": "Users",
}


@click.group()
def search() -> None:
    """Manage XNAT saved (stored) searches."""
    pass


@search.command("list")
@list_options
@global_options
@handle_errors
@require_auth
def search_list(
    ctx: Context,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List saved searches.

    \b
    Example:
        xnatctl search list
        xnatctl search list -o json
        xnatctl search list -q
    """
    service = SearchService(ctx.get_client())
    rows = service.list_searches()

    rows = apply_filter(rows, filter_expr)
    rows = apply_sort_limit(rows, sort_by, limit)

    print_output(
        rows,
        format=ctx.output_format,
        columns=resolve_columns(_LIST_COLUMNS, columns),
        column_labels=_LIST_LABELS,
        quiet=ctx.quiet,
        id_field="id",
    )


@search.command("show")
@click.argument("search_id")
@global_options
@handle_errors
@require_auth
def search_show(ctx: Context, search_id: str) -> None:
    """Show a saved search's XML definition.

    Verified live against XNAT 1.9.2.1: this route has NO JSON
    representation -- only its XML definition is available, on both the
    default (no ``format``) and explicit ``?format=xml`` requests. ``-o
    json`` here wraps that same XML text in a small JSON envelope; it does
    not mean the server answered JSON.

    \b
    Example:
        xnatctl search show my_search
        xnatctl search show my_search -o json
    """
    service = SearchService(ctx.get_client())
    definition_xml = service.get_definition(search_id)

    print_output(
        {"id": search_id, "definition_xml": definition_xml},
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="id",
    )


@search.command("run")
@click.argument("search_id")
@list_options
@global_options
@handle_errors
@require_auth
def search_run(
    ctx: Context,
    search_id: str,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """Execute a saved search and print its result rows.

    A search's result columns are entirely dynamic -- whatever fields it
    was built with -- so there is no fixed default column list the way
    other listings have one. Run once without ``--columns`` to see what a
    given search returns; this follows the same approach ``xsync list``
    uses for its own deployment-varying row shape.

    \b
    Example:
        xnatctl search run my_search
        xnatctl search run my_search -o json
        xnatctl search run my_search --columns ID,label
    """
    service = SearchService(ctx.get_client())
    rows = service.run(search_id)

    rows = apply_filter(rows, filter_expr)
    rows = apply_sort_limit(rows, sort_by, limit)

    if ctx.quiet:
        print_output(rows, quiet=True, id_field="id")
        return

    if ctx.output_format == OutputFormat.JSON:
        print_json(rows)
        return

    if not rows:
        # No fixed column list exists to hand print_output() for an empty
        # list -- see the docstring above -- so print the same "No results"
        # notice print_table() would, directly, for table format; TSV's own
        # "zero rows -> zero output" behavior needs no equivalent call.
        if ctx.output_format != OutputFormat.TSV:
            print_table([], [])
        return

    default_columns = list(dict.fromkeys(key for row in rows for key in row))
    print_output(
        rows,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
    )


@search.command("delete")
@click.argument("search_id")
@confirm_destructive("Delete this saved search?")
@global_options
@handle_errors
@require_auth
def search_delete(ctx: Context, search_id: str, dry_run: bool) -> None:
    """Delete a saved search.

    Verified live against XNAT 1.9.2.1: ``DELETE`` succeeds (200) even for
    an unknown SEARCH_ID -- delete is idempotent-succeeds here (confirmed by
    creating, deleting, and re-listing/re-showing a real saved search).
    Unlike a plain idempotent DELETE, this command still refuses an unknown
    SEARCH_ID: ``SearchService.delete`` preflights with the same GET
    ``search show`` uses (confirmed live to 404 on an unknown id), so a typo
    is reported rather than silently accepted as "deleted".

    \b
    Example:
        xnatctl search delete my_search --yes
        xnatctl search delete my_search --dry-run
    """
    service = SearchService(ctx.get_client())

    if dry_run:
        # Execution refuses an unknown search_id via delete()'s own
        # get_definition() preflight (DELETE answers 200 even for a
        # nonexistent id). Dry-run must run the same check rather than
        # reporting success for a call execution would refuse.
        service.get_definition(search_id)
        click.echo(f"[DRY-RUN] Would delete saved search {search_id}", err=True)
        return

    service.delete(search_id)
    print_success(f"Deleted saved search {search_id}")
