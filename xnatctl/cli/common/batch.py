"""``--batch`` option: load IDs from a file, a JSON array, or stdin."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


def _parse_batch_text(text: str) -> list[str]:
    """Parse batch IDs from one-per-line text or a JSON array of strings.

    Blank lines (one-per-line form) are skipped. A leading ``[`` or ``{``
    selects the JSON form; anything else is treated as one-per-line.

    Returns:
        The parsed IDs, in order. Empty input yields an empty list.

    Raises:
        click.UsageError: The text looks like JSON (starts with ``[`` or
            ``{``) but is not valid JSON, is valid JSON but not an array, or
            the array contains a non-string element.
    """
    stripped = text.strip()
    if not stripped:
        return []
    if not stripped.startswith(("[", "{")):
        return [line.strip() for line in stripped.splitlines() if line.strip()]

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise click.UsageError(f"--batch: invalid JSON ({e})") from e
    if not isinstance(parsed, list):
        raise click.UsageError(
            f"--batch: JSON input must be an array of strings, got {type(parsed).__name__}"
        )
    if not all(isinstance(item, str) for item in parsed):
        raise click.UsageError("--batch: JSON array must contain only strings")
    return [item.strip() for item in parsed if item.strip()]


def batch_option(f: F) -> F:
    """Add a ``--batch`` option for bulk operations.

    Reads a file of IDs (one per line, or a JSON array of strings) and
    injects the parsed list into the command's kwargs as ``batch_ids``.
    ``batch_ids`` is ``None`` when ``--batch`` was not given, and otherwise
    always a NON-EMPTY list -- an explicitly-given batch that parses to no
    IDs (blank file, whitespace-only stdin, ...) is rejected here with a
    ``click.UsageError`` rather than passed through as ``[]``, so a command
    body can always tell "batch given" from "batch absent" with
    ``batch_ids is not None`` and never misread an empty-but-given batch as
    absent. ``--batch -`` reads from stdin, enabling
    ``xnatctl ... list -q | xnatctl ... delete --batch -``; it is refused
    outright on an interactive terminal (stdin.read() would otherwise hang
    waiting for an EOF that never comes).

    Runs before the command body -- including before
    :func:`confirm_destructive`'s prompt when this decorator is placed
    above it -- so stdin is fully consumed here first and a piped ``-``
    batch is never partially eaten by an interactive confirmation prompt.
    """

    @click.option(
        "--batch",
        type=click.Path(exists=True, dir_okay=False, allow_dash=True),
        help="File with IDs (one per line) or JSON array; use '-' for stdin",
    )
    @wraps(f)
    def wrapper(*args: Any, batch: str | None, **kwargs: Any) -> Any:
        """Load batch IDs from file/stdin and inject into kwargs."""
        batch_ids: list[str] | None = None
        if batch is not None:
            if batch == "-":
                # Checked before any flag/prompt logic and before the read
                # itself: on an interactive terminal, sys.stdin.read() blocks
                # waiting for EOF (Ctrl-D) that never comes, hanging the
                # command even with --yes given. A file redirect or pipe is
                # not a tty, so this never fires for the intended usage.
                if sys.stdin.isatty():
                    raise click.UsageError(
                        "--batch - reads IDs from stdin, but stdin is an "
                        "interactive terminal. Pipe IDs in (e.g. 'xnatctl ... "
                        "-q | xnatctl ... --batch -') or pass a file path instead."
                    )
                if not kwargs.get("yes") and not kwargs.get("dry_run"):
                    raise click.UsageError(
                        "--batch - requires --yes or --dry-run "
                        "(stdin is consumed by the batch list)"
                    )
                text = sys.stdin.read()
            else:
                with open(batch) as file:
                    text = file.read()
            batch_ids = _parse_batch_text(text)
            # --batch was explicitly given -- an empty/whitespace-only file or
            # stdin is its own clear error, not silent "no --batch given"
            # behavior. Command bodies rely on this: they distinguish "batch
            # given" from "batch absent" by `batch_ids is not None`, which
            # only holds if a given-but-empty batch never reaches them as [].
            if not batch_ids:
                raise click.UsageError("--batch produced no IDs")
        kwargs["batch_ids"] = batch_ids
        return f(*args, **kwargs)

    return wrapper  # type: ignore
