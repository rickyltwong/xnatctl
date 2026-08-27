"""Credential input helpers shared by password-taking commands.

Passwords are never accepted on argv (visible in ``ps``, ``/proc``, shell
history); the helpers here implement the ``--*-stdin`` contract and the
argv refusal, plus the destination-profile option set built on them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


def reject_argv_password(
    alternatives: str,
) -> Callable[[click.Context, click.Parameter, Any], None]:
    """Build a Click callback that rejects an argv-supplied password.

    A password on argv is visible in ``ps``, ``/proc/*/cmdline``, and shell
    history. The option this is wired to survives only as a deterrent that
    surfaces the secret-sourcing contract in ``--help``; supplying a value is
    unconditionally a :class:`click.UsageError` (exit 2) raised at parse time,
    before any authentication or context decorator runs. The callback never
    propagates a value into the command body.

    Args:
        alternatives: One sentence naming the supported sources, appended to
            the refusal message (e.g. "Use --password-stdin, ...").
    """

    def callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
        del ctx  # The rejection is unconditional.
        if value is not None:
            option = (param.opts or [param.name])[0]
            raise click.UsageError(
                f"Refusing to read {option} from argv (visible in ps, "
                f"/proc/*/cmdline, and shell history). {alternatives}"
            )
        return

    return callback


def read_password_stdin(flag_name: str) -> str:
    """Read one password line from stdin for a ``--*-stdin`` flag.

    ``readline`` rather than ``read`` so a trailing newline (common with
    ``echo``) is stripped without consuming any further stdin bytes -- the
    command may still need the rest of the stream.

    Raises:
        click.UsageError: If stdin yields an empty line.
    """
    secret = sys.stdin.readline()
    if secret.endswith("\n"):
        secret = secret[:-1]
    if not secret:
        raise click.UsageError(f"{flag_name} was set but stdin was empty.")
    return secret


def dest_profile_options(f: F) -> F:
    """Add destination profile options for transfer commands.

    ``--dest-pass`` is argv-rejected; the wrapper resolves
    ``--dest-pass-stdin`` here and injects the secret as ``dest_pass``, so the
    wrapped commands keep their existing signatures.
    """

    @click.option("--dest-profile", help="Destination config profile name")
    @click.option("--dest-url", hidden=True, help="Destination XNAT URL (inline)")
    @click.option("--dest-user", hidden=True, help="Destination username (inline)")
    @click.option(
        "--dest-pass",
        hidden=True,
        expose_value=False,
        is_eager=True,
        callback=reject_argv_password(
            "Use --dest-pass-stdin, or --dest-profile with stored credentials."
        ),
        help="REFUSED: use --dest-pass-stdin or --dest-profile",
    )
    @click.option(
        "--dest-pass-stdin",
        is_flag=True,
        help="Read the destination password from stdin (one line)",
    )
    @wraps(f)
    def wrapper(*args: Any, dest_pass_stdin: bool = False, **kwargs: Any) -> Any:
        kwargs["dest_pass"] = read_password_stdin("--dest-pass-stdin") if dest_pass_stdin else None
        return f(*args, **kwargs)

    return wrapper  # type: ignore
