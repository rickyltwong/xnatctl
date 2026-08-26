"""Deprecated-flag table, its warning/alias callbacks, and ``parallel_options``."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, NamedTuple, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


class DeprecatedFlag(NamedTuple):
    """A CLI flag that still works but is scheduled for removal."""

    replacement: str
    """What to use instead. Empty when the flag simply no longer does anything."""

    removed_in: str
    """The release that deletes the flag."""

    deprecated_in: str
    """The release whose warning first named ``removed_in``. Anchors the
    two-MINOR-release survival window the policy promises."""


DEPRECATED_FLAGS: dict[str, DeprecatedFlag] = {
    "--no-parallel": DeprecatedFlag("--workers 1", "0.5.0", "0.3.0"),
    "--parallel": DeprecatedFlag("", "0.5.0", "0.3.0"),
    "--unzip": DeprecatedFlag("--extract", "0.5.0", "0.3.0"),
    "--no-unzip": DeprecatedFlag("--no-extract", "0.5.0", "0.3.0"),
    "--cleanup": DeprecatedFlag("", "0.5.0", "0.3.0"),
    "--no-cleanup": DeprecatedFlag("--extract --keep-zips", "0.5.0", "0.3.0"),
    "--include-resources": DeprecatedFlag("--session-resources", "0.5.0", "0.3.0"),
    "--session": DeprecatedFlag("--experiment", "0.5.0", "0.3.0"),
    "--gradual": DeprecatedFlag("--mode gradual", "0.5.0", "0.3.0"),
    "--archive-format": DeprecatedFlag("--mode", "0.5.0", "0.3.0"),
    "-e": DeprecatedFlag("-E", "0.7.0", "0.5.0"),
    "--file": DeprecatedFlag("--output-file", "0.7.0", "0.5.0"),
    "-f (api post/put)": DeprecatedFlag("--file", "0.7.0", "0.5.0"),
    "-f (container logs)": DeprecatedFlag("--follow", "0.7.0", "0.5.0"),
    "-s": DeprecatedFlag("--scans", "0.7.0", "0.5.0"),
}
"""Every deprecated flag, what replaces it, and when it goes away.

Registering a flag here is what dates the deprecation: the warning text names
the removal release from this table, so nobody has to read the changelog to
find out how long their script has. ``tests/test_deprecation_policy.py`` walks
the whole command tree and fails on any deprecated option missing from it,
which is what stops a flag being quietly retired without notice.

The removal release is at least two MINOR releases out from the one that
deprecated the flag (``deprecated_in``, recorded per entry so the window
stays anchored there whatever releases ship in between) -- see
``docs/stability.rst``.
"""


def deprecation_message(old_flag: str) -> str:
    """Build the stderr warning for a deprecated flag.

    Args:
        old_flag: The deprecated flag name, as registered in DEPRECATED_FLAGS.

    Returns:
        The full warning line.

    Raises:
        KeyError: If the flag is not registered.
    """
    entry = DEPRECATED_FLAGS[old_flag]
    guidance = f"use {entry.replacement} instead" if entry.replacement else "it has no effect"
    return (
        f"Warning: {old_flag} is deprecated and will be removed in {entry.removed_in}; {guidance}"
    )


def _flag_given(ctx: click.Context, param: click.Parameter) -> bool:
    """Report whether the user actually typed this option."""
    return bool(
        param.name
        and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
    )


def _make_alias_cb(
    old_flag: str,
    target_param: str,
    target_value: Any,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns on a deprecated flag and sets a fixed value.

    Args:
        old_flag: The deprecated flag name (e.g., "--unzip"). Must be registered
            in DEPRECATED_FLAGS; the lookup happens here, at import time, so an
            unregistered flag breaks the test suite instead of a user's command.
        target_param: The Click parameter name to set on ctx.params.
        target_value: The fixed value to set (NOT the raw flag value).

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if _flag_given(ctx, param):
            click.echo(message, err=True)
            ctx.params[target_param] = target_value
        return value

    return callback


def _make_forwarding_alias_cb(
    old_flag: str,
    target_param: str,
) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback that warns and forwards the user's raw value.

    Unlike ``_make_alias_cb`` which sets a fixed value, this forwards whatever
    the user provided (useful for value-taking options like ``--session LABEL``).

    Both spellings on one command line have to combine rather than one
    silently winning. Writing ``ctx.params[target_param]`` here bypasses
    Click's ``handle_parse_result``, which leaves the target's slot holding a
    value with no recorded parameter source; when the target option is then
    processed it finds an occupied slot it cannot out-rank and discards its
    own value. So ``-E NEW1 -E NEW2 -e OLD1`` would reach the command as
    ``('OLD1',)`` -- both ``-E`` values dropped, exit 0, and for
    ``admin refresh-catalogs`` that would mean experiments silently not
    refreshed.

    Merging here only works if the target has already been processed, which
    is why every REPEATABLE option a forwarding alias targets is declared
    ``is_eager=True``: Click sorts eager parameters ahead of the rest
    regardless of the order the user typed them.

    Single-valued targets deliberately stay non-eager, with the alias
    simply overwriting. Only one value survives either way, so there is
    nothing to merge, and eagerness is not free: an eager option typed
    before ``--help`` is processed first, so a validated one (say a
    ``click.Choice``) would reject the input and exit 2 instead of printing
    the command's help.

    Args:
        old_flag: The deprecated flag name. Must be registered in DEPRECATED_FLAGS.
        target_param: The Click parameter name to set on ctx.params.

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if value is None or not _flag_given(ctx, param):
            return value
        click.echo(message, err=True)

        target = next((p for p in ctx.command.params if p.name == target_param), None)
        target_given = (
            ctx.get_parameter_source(target_param) == click.core.ParameterSource.COMMANDLINE
        )

        if target is not None and getattr(target, "multiple", False):
            # Repeatable option: union both spellings, modern values first,
            # preserving the order each was typed in.
            existing = tuple(ctx.params.get(target_param) or ()) if target_given else ()
            ctx.params[target_param] = existing + tuple(v for v in value if v not in existing)
        else:
            ctx.params[target_param] = value
        return value

    # Lets a test assert the is_eager invariant across the whole CLI, so a
    # future alias whose target is not eager fails loudly instead of
    # silently dropping the modern spelling's values again.
    callback._forwarding_alias_target = target_param  # type: ignore[attr-defined]
    return callback


def _make_noop_cb(old_flag: str) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """Create a Click callback for a deprecated flag that no longer does anything.

    Accepting the flag in silence looks like it still works. Warning says so.

    Args:
        old_flag: The deprecated flag name. Must be registered in DEPRECATED_FLAGS.

    Returns:
        A Click callback function.
    """
    message = deprecation_message(old_flag)

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
        if _flag_given(ctx, param):
            click.echo(message, err=True)
        return value

    return callback


def parallel_options(f: F) -> F:
    """Add parallel execution options.

    Injects ``--workers`` (default resolved from profile or 4).
    Hidden ``--no-parallel`` alias sets workers to 1 with a deprecation warning.
    """

    @click.option(
        "--workers",
        "-w",
        type=int,
        default=None,
        show_default="4 (or profile)",
        help="Parallel workers (1 = sequential)",
    )
    @click.option(
        "--no-parallel",
        is_flag=True,
        hidden=True,
        expose_value=False,
        callback=_make_alias_cb("--no-parallel", "workers", 1),
    )
    @click.option(
        "--parallel",
        is_flag=True,
        hidden=True,
        expose_value=False,
        callback=_make_noop_cb("--parallel"),
        help="Deprecated: parallel is the default",
    )
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Pass through parallel options to the command."""
        return f(*args, **kwargs)

    return wrapper  # type: ignore
