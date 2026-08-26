"""``FILE|-`` payload argument: read a JSON object from a file path or stdin.

Kept as its own module, a sibling of the ``cli/common/`` package rather than
a submodule inside it, on purpose: ``cli/common/`` is a separate, actively
maintained surface and this helper's only consumers are the Container
Service command modules (``command_cmd.py``, ``wrapper.py``) that need a
``FILE|-`` positional argument for ``command create``/``command update``/
``wrapper config set``. Nothing here is Container-Service-specific by
content -- it is a generic "read a JSON object from a path or stdin, with
the same stdin/confirmation-prompt conflict ``batch_option`` handles" -- but
it has exactly one feature's worth of call sites today, so it stays local to
that feature rather than growing the shared package's surface for it.
"""

from __future__ import annotations

import difflib
import json
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


def _parse_json_object(text: str, source: str) -> dict[str, Any]:
    """Parse ``text`` as a JSON object, raising a usage error otherwise."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise click.UsageError(f"{source}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise click.UsageError(
            f"{source}: payload must be a JSON object, got {type(data).__name__}"
        )
    return data


def read_payload_argument(f: F) -> F:
    """Add a ``FILE`` positional argument, read and parsed as a JSON object.

    ``FILE`` is a path to a JSON document, or ``-`` for stdin -- the
    ``command create``/``command update``/``wrapper config set`` convention.
    Injects the parsed payload into the command's kwargs as ``payload`` (a
    ``dict``) and the original path/``-`` as ``payload_source`` (for
    dry-run/error messages).

    Mirrors ``xnatctl.cli.common.batch_option`` exactly, for the same
    reason: ``FILE -`` reads the payload from stdin, and
    ``@confirm_destructive``'s confirmation prompt also reads stdin
    (``click.confirm``) -- both cannot consume the same stream. Placed
    **above** ``@confirm_destructive``/``@confirm_destructive_when`` in the
    decorator stack (so it runs first and stdin is fully consumed here,
    before any prompt would also try to read it), it refuses outright on an
    interactive terminal (``sys.stdin.read()`` would otherwise hang waiting
    for an EOF that never comes) and requires ``--yes`` or ``--dry-run``
    when reading from stdin, so a stdin payload can never be silently eaten
    by an interactive prompt that then has nothing left to read. Checking
    ``kwargs.get("yes")``/``kwargs.get("dry_run")`` here works regardless of
    where this decorator sits in the stack relative to
    ``@confirm_destructive``, because Click resolves every declared
    parameter (across every decorator on the command) before invoking the
    fully-wrapped callable -- see ``batch_option`` for the same reasoning.
    """

    @click.argument("file", type=click.Path(exists=True, dir_okay=False, allow_dash=True))
    @wraps(f)
    def wrapper(*args: Any, file: str, **kwargs: Any) -> Any:
        """Read FILE (or stdin) and inject the parsed payload into kwargs."""
        if file == "-":
            if sys.stdin.isatty():
                raise click.UsageError(
                    "FILE - reads the payload from stdin, but stdin is an "
                    "interactive terminal. Pipe JSON in (e.g. 'cat command.json | "
                    "xnatctl command create -') or pass a file path instead."
                )
            if not kwargs.get("yes") and not kwargs.get("dry_run"):
                raise click.UsageError(
                    "FILE - requires --yes or --dry-run (stdin is consumed by the payload)"
                )
            text = sys.stdin.read()
        else:
            with open(file, encoding="utf-8") as fh:
                text = fh.read()
        kwargs["payload"] = _parse_json_object(text, file)
        kwargs["payload_source"] = file
        return f(*args, **kwargs)

    return wrapper  # type: ignore


def json_diff(before: dict[str, Any], after: dict[str, Any], *, label: str) -> str:
    """Render a unified diff between two JSON objects, for ``--dry-run`` previews.

    Both sides are serialized with sorted keys and 2-space indentation so
    the diff reflects real content changes rather than key-ordering noise.
    ``before={}`` (nothing exists on the server yet, e.g. ``command
    create``) renders as an all-additions diff, which is the honest preview
    for that case -- there is no separate "creation" rendering path.

    Args:
        before: The current server-side state (``{}`` if none exists yet).
        after: The payload that would be sent.
        label: Used to name both sides of the diff (e.g. ``"command 12"``).

    Returns:
        A unified diff as a single string (empty when there is no change).
    """
    before_lines = json.dumps(before, indent=2, sort_keys=True).splitlines(keepends=True)
    after_lines = json.dumps(after, indent=2, sort_keys=True).splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"{label} (current)",
        tofile=f"{label} (new)",
    )
    return "".join(diff)
