"""Error rendering, ``handle_errors``, and the documented CLI exit-code map."""

from __future__ import annotations

import enum
import logging
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import click

from xnatctl.core.exceptions import (
    AuthenticationError,
    OperationCancelledError,
    PermissionDeniedError,
    ResourceNotFoundError,
    XNATConnectionError,
    XNATCtlError,
)
from xnatctl.core.logging import FILE_ONLY_ATTR, debug_env_enabled
from xnatctl.core.output import print_error, print_hint
from xnatctl.core.redact import redact_url_query

P = ParamSpec("P")
R = TypeVar("R")

AUDIT_ERROR_KEY = "xnatctl.audit_error"

_LOGGER_NAME = "xnatctl.cli.common"


def _debug_enabled() -> bool:
    if debug_env_enabled():
        return True
    click_ctx = click.get_current_context(silent=True)
    ctx_obj = click_ctx.obj if click_ctx is not None else None
    return bool(getattr(ctx_obj, "verbose", False))


def _log_file_only(
    message: str,
    *args: Any,
    level: int = logging.DEBUG,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    logging.getLogger(_LOGGER_NAME).log(
        level, message, *args, exc_info=exc_info, extra={FILE_ONLY_ATTR: True, **fields}
    )


def render_cli_error(exc: BaseException) -> int:
    """Render a redacted one-line error (+ optional traceback) and return its exit code.

    Shared by :func:`handle_errors` and the ``main()`` last-resort guard so the
    traceback-under-debug policy lives in exactly one place. ``main()`` needs it
    too because setup-phase failures (config load / env parsing inside
    ``global_options``) are raised OUTSIDE the ``handle_errors`` wrapper.

    Must be called from within an ``except`` block: it relies on
    ``traceback.format_exc()`` reflecting the exception currently being handled.
    """
    debug = _debug_enabled()

    _log_file_only(
        "Command failed: %s",
        exc,
        level=logging.ERROR,
        exc_info=True,
        event="command_failed",
    )

    if isinstance(exc, XNATCtlError):
        print_error(redact_url_query(str(exc)))
        if exc.hint:
            print_hint(exc.hint)
        if debug and exc.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in exc.details.items())
            click.echo(redact_url_query(f"Details: {detail_str}"), err=True)
    else:
        print_error(redact_url_query(f"Unexpected error: {exc}"))

    if debug:
        click.echo(redact_url_query(traceback.format_exc()), err=True)
    elif not isinstance(exc, XNATCtlError):
        click.echo("Run with --verbose for a full traceback.", err=True)

    return exit_code_for(exc)


def handle_errors(f: Callable[P, R]) -> Callable[P, R]:
    """Handle common errors and convert to CLI exceptions.

    Under ``--verbose`` or ``XNATCTL_DEBUG=1`` a full (redacted) traceback is
    printed to stderr before exiting; otherwise unexpected errors print a single
    line plus a hint. Tracebacks are never shown by default.
    """

    @wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return f(*args, **kwargs)
        except click.Abort:
            click.echo("Aborted!", err=True)
            _log_file_only(
                "Command aborted by user (declined confirmation)",
                level=logging.INFO,
                event="cancelled",
            )
            sys.exit(ExitCode.USER_CANCELLED)
        except KeyboardInterrupt:
            # Ctrl+C mid-operation. Caught here rather than left to Click,
            # which converts it to Abort and exits 1 -- indistinguishable from
            # a general error, despite the taxonomy defining a code for exactly
            # this. Catching it inside the command body also keeps the exit
            # code consistent with the confirmation-prompt Abort above.
            #
            # KeyboardInterrupt is not an Exception subclass, so the broad
            # handler below would never have seen it.
            click.echo("Cancelled.", err=True)
            _log_file_only(
                "Command cancelled by user (KeyboardInterrupt)",
                level=logging.INFO,
                event="cancelled",
            )
            sys.exit(ExitCode.USER_CANCELLED)
        except click.ClickException:
            raise
        except SystemExit as e:
            exit_level = logging.ERROR if e.code not in (None, 0) else logging.INFO
            _log_file_only("Command exited with code %s", e.code, level=exit_level, event="exit")
            raise
        except Exception as e:
            click_ctx = click.get_current_context(silent=True)
            if click_ctx is not None:
                click_ctx.meta[AUDIT_ERROR_KEY] = type(e).__name__
            raise SystemExit(render_cli_error(e)) from e

    return wrapper


class ExitCode(enum.IntEnum):
    """Documented, differentiated CLI exit codes.

    Code 2 is intentionally skipped: Click reserves it for usage errors, so
    reusing it would make "wrong flags" indistinguishable from an auth failure.
    Codes only ever become MORE specific than the old blanket 1, so scripts that
    test ``!= 0`` keep working.
    """

    SUCCESS = 0
    GENERAL_ERROR = 1
    # 2 is reserved for Click usage errors (do not assign it here).
    AUTH_ERROR = 3
    NETWORK_ERROR = 4
    NOT_FOUND = 5
    PERMISSION_ERROR = 6
    USER_CANCELLED = 7


def exit_code_for(exc: BaseException) -> int:
    """Map an exception to its documented CLI exit code (see :class:`ExitCode`).

    Order matters: ``PermissionDeniedError`` and ``SessionExpiredError`` subclass
    ``AuthenticationError``, so the most specific classes are checked first.
    """
    if isinstance(exc, click.Abort | OperationCancelledError):
        return ExitCode.USER_CANCELLED
    if isinstance(exc, PermissionDeniedError):
        return ExitCode.PERMISSION_ERROR
    if isinstance(exc, AuthenticationError):
        return ExitCode.AUTH_ERROR
    if isinstance(exc, ResourceNotFoundError):
        return ExitCode.NOT_FOUND
    if isinstance(exc, XNATConnectionError):
        return ExitCode.NETWORK_ERROR
    return ExitCode.GENERAL_ERROR
