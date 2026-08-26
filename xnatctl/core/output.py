"""Output formatting for xnatctl.

Provides consistent output in JSON, table, and quiet modes using Rich.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from enum import Enum
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from xnatctl.core.redact import redact_url_query

# =============================================================================
# Console Instances
# =============================================================================


def no_color_requested() -> bool:
    """Return True when NO_COLOR or CLICOLOR=0 asks for color to be disabled.

    NO_COLOR (https://no-color.org): any non-empty value disables color, and
    Rich's ``Console`` already honors it on its own when constructed without
    an explicit ``no_color=``. CLICOLOR=0 is a second, equally common
    convention Rich does not check. Exposed as a function (checked fresh, not
    just baked into the console singletons at import time) so the CLI's
    ``--no-color`` handling can react to it per invocation.
    """
    if os.environ.get("NO_COLOR", "") != "":
        return True
    return os.environ.get("CLICOLOR", "") == "0"


def _make_console(*, stderr: bool = False) -> Console:
    """Build a console with NO_COLOR/CLICOLOR=0 wired in explicitly.

    Passing ``no_color=`` here (rather than leaving it to Rich's own env
    detection) is what makes the starting state testable and gives
    :func:`set_no_color` a value to flip later. ``_color_system`` is blanked
    too when color is disabled from the start -- see :func:`set_no_color` for
    why ``no_color`` alone is not enough to suppress every ANSI escape.
    """
    con = Console(stderr=stderr, no_color=no_color_requested())
    if con.no_color:
        _set_color_system(con, no_color=True)
    return con


def _set_color_system(con: Console, *, no_color: bool) -> None:
    """Blank or re-detect a console's color system.

    Private Rich API (``_color_system``/``_detect_color_system``), stable
    across the pinned 13.x-15.x range; if a future Rich moves it, degrade to
    ``no_color``-only suppression (colors gone, attribute escapes may leak)
    rather than crashing at startup.
    """
    try:
        con._color_system = None if no_color else con._detect_color_system()
    except AttributeError:  # pragma: no cover - future-Rich fallback
        pass


console = _make_console()
err_console = _make_console(stderr=True)


def set_no_color(no_color: bool) -> None:
    """Reconfigure the module-level consoles' ``no_color`` flag in place.

    ``console``/``err_console`` are singletons imported by reference
    throughout the CLI, so honoring ``--no-color`` (or a fresh NO_COLOR/
    CLICOLOR=0 check at invocation time) means mutating the existing
    instances rather than rebinding these module names to new ones.

    Rich's own ``no_color`` only blanks the color component of a style --
    bold/dim/underline (e.g. this module's table header style) still render
    as ANSI escapes with ``no_color=True`` alone. Blanking ``_color_system``
    too suppresses all of it, which is what NO_COLOR/CLICOLOR=0/--no-color
    are for: a fully plain-text stream, not merely an uncolored one.
    Re-enabling re-detects the color system from the live terminal rather
    than caching the value from console construction.
    """
    for con in (console, err_console):
        con.no_color = no_color
        _set_color_system(con, no_color=no_color)


# Module-level header toggle, mutated per invocation exactly like the
# ``no_color`` state above: commands never pass it through print_output, the
# CLI's --no-headers plumbing sets it once and every table/TSV render honors
# it. JSON and quiet output have no header line, so the flag is meaningless
# there and silently ignored (consistent with --columns).
_no_headers = False


def set_no_headers(no_headers: bool) -> None:
    """Set whether table/TSV output suppresses its header line/row.

    Called once per CLI invocation by the ``--no-headers`` plumbing (root
    group and ``@global_options``), unconditionally, so repeated in-process
    invocations never inherit a previous run's value.
    """
    global _no_headers
    _no_headers = no_headers


# =============================================================================
# Output Format
# =============================================================================


class OutputFormat(Enum):
    """Output format options."""

    JSON = "json"
    TABLE = "table"
    TSV = "tsv"

    @classmethod
    def from_string(cls, value: str) -> OutputFormat:
        """Create from string value."""
        return cls(value.lower())


# =============================================================================
# Table Output
# =============================================================================


def print_table(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str],
    *,
    title: str | None = None,
    column_labels: dict[str, str] | None = None,
) -> None:
    """Print data as a Rich table.

    Args:
        rows: List of dictionaries with data.
        columns: Column keys to display.
        title: Optional table title.
        column_labels: Optional mapping of column keys to display labels.
    """
    if not rows:
        # Stderr so an empty `-o table` pipe stays clean. Scripts
        # should use `-o json` for emptiness checks, not parse this line.
        err_console.print("[dim]No results[/dim]")
        return

    table = Table(title=title, show_header=not _no_headers, header_style="bold")

    # Add columns with optional custom labels
    labels = column_labels or {}
    for col in columns:
        label = labels.get(col, col.replace("_", " ").title())
        table.add_column(label)

    # Add rows
    for row in rows:
        values = []
        for col in columns:
            val = row.get(col, "")
            if val is None:
                val = ""
            elif isinstance(val, bool):
                val = "Yes" if val else "No"
            elif isinstance(val, (list, dict)):
                val = json.dumps(val)
            values.append(str(val))
        table.add_row(*values)

    console.print(table)


def print_key_value(
    data: dict[str, Any],
    *,
    title: str | None = None,
    key_labels: dict[str, str] | None = None,
) -> None:
    """Print key-value pairs in a formatted way.

    Args:
        data: Dictionary of key-value pairs.
        title: Optional title.
        key_labels: Optional mapping of keys to display labels.
    """
    if title:
        console.print(f"[bold]{title}[/bold]")

    labels = key_labels or {}
    max_key_len = max(len(labels.get(k, k)) for k in data) if data else 0

    for key, value in data.items():
        label = labels.get(key, key.replace("_", " ").title())
        if value is None:
            value = "[dim]-[/dim]"
        elif isinstance(value, bool):
            value = "[green]Yes[/green]" if value else "[red]No[/red]"
        elif isinstance(value, (list, dict)):
            value = json.dumps(value, indent=2)

        console.print(f"  {label:<{max_key_len}}  {value}")


# =============================================================================
# TSV Output
# =============================================================================


# Every C0 control character (0x00-0x1f) plus DEL (0x7f), EXCEPT tab/LF/CR --
# those three are handled first, by collapsing to a space rather than being
# dropped outright, so a value that happened to contain a newline still reads
# as prose. Everything else in this range (ESC included, for stray ANSI/CSI
# sequences) is stripped entirely: a script piping ``-o tsv`` into a terminal
# or a log file must never receive a raw control byte.
_TSV_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _tsv_cell(value: Any) -> str:
    """Render one value as a single, control-byte-free TSV field.

    Scalars use the same tokens ``-o json`` would (``true``/``false`` for
    booleans, empty for ``None``, compact JSON for nested lists/dicts), so a
    script moving between the two formats greps for the same strings --
    deliberately NOT the table's ``Yes``/``No`` presentation. Embedded tabs,
    newlines, and carriage returns each collapse to a single space so one
    record is always exactly one line with exactly one tab per field boundary.
    Every other C0 control character and DEL (e.g. a stray ``\\x1b[31m`` ANSI
    escape in upstream data) is stripped outright, not collapsed to a space --
    see :data:`_TSV_CONTROL_RE`.
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (list, dict)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    text = text.replace("\r\n", " ").replace("\t", " ").replace("\n", " ").replace("\r", " ")
    return _TSV_CONTROL_RE.sub("", text)


def print_tsv(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    """Print rows as tab-separated lines via plain ``print()``.

    Never routed through Rich: no color, no ANSI escapes, no width-dependent
    layout, even on a forced terminal -- the whole point of the format is
    byte-stable ``awk``/``cut`` input. The header line holds the raw column
    KEYS (not the table's display labels), because those are the names
    ``--columns`` accepts and ``-o json`` emits. :func:`set_no_headers`
    suppresses the header. Zero rows print the header alone (or nothing under
    ``--no-headers``) with no stderr notice: an absent data line IS the
    emptiness signal in this format.

    Args:
        rows: Row dicts to print.
        columns: Column keys, in output order.
    """
    if not columns:
        return
    if not _no_headers:
        print("\t".join(_tsv_cell(col) for col in columns))
    for row in rows:
        print("\t".join(_tsv_cell(row.get(col)) for col in columns))


def _print_tsv_output(data: Any, columns: Sequence[str] | None) -> None:
    """Dispatch arbitrary command data to :func:`print_tsv`.

    Shape decisions (all deliberate; see also :func:`print_output`):

    - A list without ``columns`` derives them from the union of row keys in
      first-seen order. The table branch falls back to JSON here, but TSV
      exists precisely to be line/field-parseable, so it must always emit
      TSV rather than silently switching formats.
    - A single dict prints one header plus one data row -- NOT key/value
      lines -- so the shape ("line 1 = field names, one record per line")
      is identical whether a command returns one record or many, and
      ``cut -f2`` means the same thing either way.
    - A list containing non-dict items, or bare scalar data, prints one
      sanitized value per line with no header (there are no field names to
      put in one).
    """
    if isinstance(data, list):
        if all(isinstance(item, dict) for item in data):
            cols = list(columns) if columns else list(dict.fromkeys(k for row in data for k in row))
            print_tsv(data, cols)
        else:
            for item in data:
                print(_tsv_cell(item))
    elif isinstance(data, dict):
        print_tsv([data], list(columns) if columns else list(data.keys()))
    else:
        print(_tsv_cell(data))


# =============================================================================
# JSON Output
# =============================================================================


def print_json(data: Any, *, indent: int = 2) -> None:
    """Print data as JSON.

    Args:
        data: Data to print.
        indent: Indentation level.
    """
    print(json.dumps(data, indent=indent, default=str))


# =============================================================================
# Unified Output
# =============================================================================


def print_output(
    data: Any,
    *,
    format: OutputFormat = OutputFormat.TABLE,
    columns: Sequence[str] | None = None,
    column_labels: dict[str, str] | None = None,
    title: str | None = None,
    quiet: bool = False,
    id_field: str = "id",
) -> None:
    """Print data in the specified format.

    ``quiet`` wins over every format, TSV included -- the quiet check runs
    before the format dispatch, exactly as it always has for JSON/table.
    ``column_labels`` and ``title`` are table presentation only; TSV ignores
    both (its header is the raw column keys -- see :func:`print_tsv`), and
    the header row itself is suppressed via :func:`set_no_headers` for both
    table and TSV. See :func:`_print_tsv_output` for the TSV shape decisions.

    Args:
        data: Data to print (dict, list, or scalar).
        format: Output format.
        columns: Columns for table/TSV format.
        column_labels: Labels for columns.
        title: Optional title.
        quiet: If True, only print IDs.
        id_field: Field to use for IDs in quiet mode.
    """
    if quiet:
        # Quiet mode: just IDs, one per line
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    # Try common ID fields
                    id_val = (
                        item.get(id_field)
                        or item.get("ID")
                        or item.get("label")
                        or item.get("name")
                        or ""
                    )
                    print(id_val)
                else:
                    print(item)
        elif isinstance(data, dict):
            id_val = (
                data.get(id_field) or data.get("ID") or data.get("label") or data.get("name") or ""
            )
            print(id_val)
        else:
            print(data)
        return

    if format == OutputFormat.JSON:
        print_json(data)
        return

    if format == OutputFormat.TSV:
        _print_tsv_output(data, columns)
        return

    # Table format
    if isinstance(data, list) and columns:
        print_table(data, columns, title=title, column_labels=column_labels)
    elif isinstance(data, dict):
        if columns:
            print_table([data], columns, title=title, column_labels=column_labels)
        else:
            print_key_value(data, title=title, key_labels=column_labels)
    else:
        # Fallback to JSON
        print_json(data)


# =============================================================================
# Status Messages
# =============================================================================


def print_error(message: str) -> None:
    """Print error message to stderr.

    The message is routed through :func:`redact_url_query` so that URLs
    embedded in the error never leak secret-shaped query values, and escaped so
    that square brackets in it are shown rather than parsed as Rich markup
    instead of being silently deleted.
    """
    err_console.print(f"[red]Error:[/red] {escape(redact_url_query(message))}")


def print_warning(message: str) -> None:
    """Print warning message to stderr.

    The message is routed through :func:`redact_url_query` so that URLs
    embedded in the warning never leak secret-shaped query values, and escaped
    so that square brackets survive (see :func:`print_error`).
    """
    err_console.print(f"[yellow]Warning:[/yellow] {escape(redact_url_query(message))}")


def print_hint(hint: str) -> None:
    """Print a dimmed next-step line to stderr, below an error.

    Dimmed and prefixed so it reads as guidance rather than a second failure,
    and on stderr so it travels with the error it belongs to rather than
    contaminating piped data.
    """
    err_console.print(f"[dim]Try: {escape(redact_url_query(hint))}[/dim]")


def print_success(message: str) -> None:
    """Print success message to stderr.

    Stderr so it never interleaves with data being piped from stdout:
    a success line is commentary about the run, not part of its output.
    """
    err_console.print(f"[green]\u2713[/green] {escape(message)}")


def print_info(message: str) -> None:
    """Print info message to stderr (commentary, not data)."""
    err_console.print(f"[blue]Info:[/blue] {escape(message)}")


# =============================================================================
# Progress
# =============================================================================


def create_progress() -> Progress:
    """Create a Rich progress bar.

    Returns:
        Progress instance.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        # Stderr, so `xnatctl session download ... > log` still shows a live
        # bar: Rich disables live display when its console is a non-tty, and
        # a stdout console loses the bar under redirection even though
        # stderr is an interactive terminal.
        console=err_console,
    )


def create_spinner() -> Progress:
    """Create a spinner for indeterminate progress.

    Returns:
        Progress instance with spinner only.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=err_console,
    )
