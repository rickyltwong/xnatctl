"""List controls: client-side ``--filter``/``--sort-by``/``--limit``/``--columns``."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence
from typing import Any, TypeVar, overload

import click

F = TypeVar("F", bound=Callable[..., Any])


def apply_filter(rows: list[dict[str, Any]], expr: str | None) -> list[dict[str, Any]]:
    """Filter *rows* by a ``field:glob`` expression, case-insensitively.

    Args:
        rows: Row dicts to filter.
        expr: A ``field:glob`` expression (e.g. ``"label:SUB*"``), or ``None``
            for no filtering.

    Returns:
        The rows whose ``field`` value matches ``glob`` (fnmatch, case folded
        on both sides).

    Raises:
        click.UsageError: ``expr`` has no ``:`` separator, has an empty
            field, or names a field that is not a key on any row (a typo'd
            field would otherwise silently compare against ``""`` and match
            every row -- see the "Available" list in the error for the
            field names this command actually has).
    """
    if not expr:
        return rows
    if ":" not in expr:
        raise click.UsageError(
            f"Invalid --filter '{expr}': expected 'field:glob' syntax, e.g. --filter 'label:SUB*'"
        )
    field, _, pattern = expr.partition(":")
    field = field.strip()
    if not field:
        raise click.UsageError(
            f"Invalid --filter '{expr}': expected 'field:glob' syntax, e.g. --filter 'label:SUB*'"
        )
    if rows:
        # Rows are not required to be uniform (xsync's payload varies by
        # deployment), so "known fields" is the union across every row, not
        # just the first.
        available = dict.fromkeys(key for row in rows for key in row)
        if field not in available:
            raise click.UsageError(
                f"Unknown filter field '{field}'. Available: {', '.join(available)}"
            )
    pattern = pattern.strip().casefold()
    return [row for row in rows if fnmatch.fnmatch(str(row.get(field, "")).casefold(), pattern)]


def _numeric_or_none(value: Any) -> float | None:
    """Parse *value* as a float, or ``None`` if it isn't numeric-looking."""
    if isinstance(value, bool):  # bool is an int subclass; not a "numeric column" value
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def apply_sort_limit(
    rows: list[dict[str, Any]],
    sort_by: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Sort and/or truncate *rows*, client-side.

    Args:
        rows: Row dicts to sort/truncate.
        sort_by: ``field`` or ``field:desc`` (``field:asc`` is also accepted,
            and is the default direction).
        limit: Maximum rows to keep, applied after sorting. ``0`` yields no
            rows; ``None`` keeps everything.

    Returns:
        The sorted/truncated rows (a new list; the input is not mutated).

        Rows missing ``field`` (or holding ``None``) always sort LAST,
        regardless of direction -- "wherever the field exists, sorted;
        whatever doesn't have it, at the bottom" holds either way, rather
        than flipping to "at the top" under ``:desc``. Present values are
        compared numerically when every one of them parses as a number
        (``int``/``float``, or a numeric string); otherwise as case-folded
        strings, so ``"2"`` sorts before ``"10"`` in a numeric column
        instead of after it.

    Raises:
        click.UsageError: ``sort_by`` names a direction other than
            ``asc``/``desc``.
    """
    if sort_by:
        field, _, direction = sort_by.partition(":")
        field = field.strip()
        direction = direction.strip().lower()
        if direction and direction not in ("asc", "desc"):
            raise click.UsageError(
                f"Invalid --sort-by '{sort_by}': direction must be 'asc' or 'desc'"
            )
        reverse = direction == "desc"

        with_value = [row for row in rows if row.get(field) is not None]
        missing = [row for row in rows if row.get(field) is None]

        numeric_values = [_numeric_or_none(row.get(field)) for row in with_value]
        all_numeric = bool(with_value) and all(v is not None for v in numeric_values)

        if all_numeric:
            numeric_pairs: list[tuple[float, dict[str, Any]]] = []
            for numeric_value, row in zip(numeric_values, with_value, strict=True):
                assert numeric_value is not None  # guaranteed by all_numeric, above
                numeric_pairs.append((numeric_value, row))
            with_value = [
                row for _, row in sorted(numeric_pairs, key=lambda pair: pair[0], reverse=reverse)
            ]
        else:
            with_value = sorted(
                with_value,
                key=lambda row: str(row.get(field, "")).casefold(),
                reverse=reverse,
            )

        rows = with_value + missing

    if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
        rows = rows[:limit]

    return rows


def resolve_columns(available: Sequence[str], requested: str | None) -> list[str]:
    """Resolve a ``--columns a,b,c`` value against the command's known columns.

    Args:
        available: The command's full/default column set, in display order.
        requested: Comma-separated column names, or ``None`` to keep the
            default set unchanged.

    Returns:
        ``list(available)`` when ``requested`` is ``None``; otherwise the
        requested columns, in the order given.

    Raises:
        click.UsageError: ``requested`` is not ``None`` but contains no
            actual column name (``""``, ``","``, or ``"  "``) -- silently
            resolving that to an empty list would make ``print_output``
            treat ``columns`` as falsy and fall through to JSON regardless
            of the requested table/quiet format. An explicit ``--columns ''``
            is a caller mistake, not "use the defaults" -- only omitting the
            flag entirely (``None``) does that. Also raised if any requested
            column is not in ``available``.
    """
    if requested is None:
        return list(available)
    columns = [c.strip() for c in requested.split(",") if c.strip()]
    if not columns:
        raise click.UsageError(
            f"Invalid --columns '{requested}': no column names found. "
            f"Available: {', '.join(available)}"
        )
    unknown = [c for c in columns if c not in available]
    if unknown:
        raise click.UsageError(
            f"Unknown column(s): {', '.join(unknown)}. Available: {', '.join(available)}"
        )
    return columns


@overload
def list_options(_func: F) -> F: ...
@overload
def list_options(_func: None = None, *, include_limit: bool = True) -> Callable[[F], F]: ...
def list_options(_func: F | None = None, *, include_limit: bool = True) -> F | Callable[[F], F]:
    """Add uniform list controls: ``--filter``, ``--sort-by``, ``--columns``.

    ``--limit`` is included unless ``include_limit=False`` -- for the few list
    commands (``pipeline jobs``, ``admin audit``) that already forward their
    own ``--limit`` to the server, so this does not double up the flag.

    This decorator only declares the Click options; it does not touch the
    command's return value. Every command applying it is responsible for
    calling :func:`apply_filter` and :func:`apply_sort_limit` on its own row
    list, and :func:`resolve_columns` before handing columns to
    ``print_output``/``print_table``. Filtering, sorting, and ``--limit``
    (where added by this decorator) are always CLIENT-side, applied to
    whatever the server already returned. ``pipeline jobs`` and
    ``admin audit`` keep their own PRE-EXISTING ``--limit``, which IS
    forwarded to the server -- but only when neither ``--filter`` nor
    ``--sort-by`` is given; with either of those, the command must drop the
    server-side limit for that request (fetch the full/unbounded result set)
    and apply ``--limit`` client-side afterward instead, or filtering/sorting
    would silently miss whatever fell outside the small server window.
    ``--columns`` affects TABLE output only -- it never narrows what
    ``-o json`` prints, matching every command's existing JSON contract.

    Usable as ``@list_options`` or ``@list_options(include_limit=False)``.

    \b
    Filter syntax:
        --filter 'field:glob'   e.g. --filter 'label:SUB*'
        --sort-by field[:desc]  e.g. --sort-by date:desc
        --columns a,b,c         table output only; must name columns the
                                 command already prints
    """

    def decorator(f: F) -> F:
        """Attach the shared list-control options to *f*."""
        f = click.option(
            "--columns", help="Comma-separated columns to display (table output only)"
        )(f)
        f = click.option("--sort-by", help="Sort by FIELD, or FIELD:desc for descending")(f)
        if include_limit:
            f = click.option(
                "--limit",
                type=click.IntRange(min=0),
                default=None,
                help="Maximum results (default: all)",
            )(f)
        return click.option("--filter", "filter_expr", help="Filter expression, e.g. 'label:SUB*'")(
            f
        )

    if _func is not None:
        return decorator(_func)
    return decorator
