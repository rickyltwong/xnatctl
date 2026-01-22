"""Output formatting for xnatctl.

Provides consistent output in JSON, table, and quiet modes using Rich.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import Enum
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

# =============================================================================
# Console Instances
# =============================================================================

console = Console()
err_console = Console(stderr=True)


# =============================================================================
# Output Format
# =============================================================================


class OutputFormat(Enum):
    """Output format options."""

    JSON = "json"
    TABLE = "table"

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
        console.print("[dim]No results[/dim]")
        return

    table = Table(title=title, show_header=True, header_style="bold")

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
    max_key_len = max(len(labels.get(k, k)) for k in data.keys()) if data else 0

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

    Args:
        data: Data to print (dict, list, or scalar).
        format: Output format.
        columns: Columns for table format.
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
    """Print error message to stderr."""
    err_console.print(f"[red]Error:[/red] {message}")


def print_warning(message: str) -> None:
    """Print warning message to stderr."""
    err_console.print(f"[yellow]Warning:[/yellow] {message}")


def print_success(message: str) -> None:
    """Print success message."""
    console.print(f"[green]\u2713[/green] {message}")


def print_info(message: str) -> None:
    """Print info message."""
    console.print(f"[blue]Info:[/blue] {message}")


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
        console=console,
    )


def create_spinner() -> Progress:
    """Create a spinner for indeterminate progress.

    Returns:
        Progress instance with spinner only.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    )
