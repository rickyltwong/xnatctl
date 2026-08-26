"""Error rendering, ``handle_errors``, and the documented CLI exit-code map."""

from __future__ import annotations

import enum
import logging
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

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

F = TypeVar("F", bound=Callable[..., Any])

# Click context key used to pass the real exception class from handle_errors
# to the audit record, across the SystemExit that separates them.
AUDIT_ERROR_KEY = "xnatctl.audit_error"

# Hardcoded rather than `logging.getLogger(__name__)`: records from this
# module carry the stable name `xnatctl.cli.common`, not the module path.
# Every cancellation, direct exit, and rendered failure writes a `logger`
# field into the JSON-lines diagnostics artifact (see _log_file_only), and
# anyone filtering that artifact by logger name gets one stable name
# regardless of how the package is laid out on disk.
_LOGGER_NAME = "xnatctl.cli.common"


# =============================================================================
# Error Handling
# =============================================================================


def _debug_enabled() -> bool:
    """Return True when tracebacks should be surfaced.

    Two independent opt-ins: the ``--verbose`` flag (stashed on the shared
    :class:`Context` as ``verbose``) and the ``XNATCTL_DEBUG`` env var (mirrors
    ``gh``'s ``GH_DEBUG``, so a traceback is obtainable even for failures that
    occur before ``--verbose`` is parsed). ``XNATCTL_DEBUG=0``/``false``/``off``
    counts as OFF -- an explicit falsey value must not enable tracebacks.

    The env var's spelling is parsed by :func:`debug_env_enabled` so this policy
    and the logging tiers it turns on cannot drift apart.
    """
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
    """Log a record meant for an active diagnostics file, never stderr.

    stderr's rendering of a command's outcome goes through ``click.echo``/
    ``print_error`` below, which never touch the logging module at all -- so
    without a call like this, a diagnostics file would carry no record of
    *why* a command failed or stopped early, which is exactly the moment the
    file is most valuable. ``FILE_ONLY_ATTR`` keeps it off stderr regardless
    of ``level`` -- even under ``-v``/``XNATCTL_DEBUG``, where the level alone
    would otherwise let it through -- via ``_ExcludeFileOnlyFilter``, so it
    never duplicates the traceback ``render_cli_error`` already prints there
    in its own format.

    Args:
        message: %-style log message.
        *args: %-style interpolation args for ``message``.
        level: Logging level for this record within the file (stderr
            visibility is governed by FILE_ONLY_ATTR, not this).
        exc_info: Passed straight to the logger -- True to attach the
            currently-handled exception's traceback (only valid from within
            an ``except`` block).
        **fields: Extra structured fields (e.g. ``event="cancelled"``),
            surfaced by :class:`~xnatctl.core.logging.JsonLinesFormatter`.
    """
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

    # Always land the full (redacted) traceback in an active diagnostics
    # file, independent of the stderr tier below -- this is the ONE call
    # that gives XNATCtlError and unexpected-Exception failures a trace in
    # the file at all; see _log_file_only for why click.echo/print_error
    # alone cannot do this.
    _log_file_only(
        "Command failed: %s",
        exc,
        level=logging.ERROR,
        exc_info=True,
        event="command_failed",
    )

    if isinstance(exc, XNATCtlError):
        # Defensive: print_error already redacts, but we redact here too so
        # future direct callers cannot bypass the invariant.
        print_error(redact_url_query(str(exc)))
        if exc.hint:
            print_hint(exc.hint)
        if debug and exc.details:
            # The details dict is real diagnostic data, but it belongs
            # behind --verbose, where the reader asked for it -- never glued
            # onto the message itself.
            detail_str = ", ".join(f"{k}={v}" for k, v in exc.details.items())
            click.echo(redact_url_query(f"Details: {detail_str}"), err=True)
    else:
        print_error(redact_url_query(f"Unexpected error: {exc}"))

    if debug:
        # Redact the whole traceback: its final line echoes the exception
        # message, which may carry a URL query string.
        click.echo(redact_url_query(traceback.format_exc()), err=True)
    elif not isinstance(exc, XNATCtlError):
        # A clean XNATCtlError is already actionable; only the opaque
        # "Unexpected error" line benefits from the verbose hint.
        click.echo("Run with --verbose for a full traceback.", err=True)

    return exit_code_for(exc)


def handle_errors(f: F) -> F:
    """Handle common errors and convert to CLI exceptions.

    Under ``--verbose`` or ``XNATCTL_DEBUG=1`` a full (redacted) traceback is
    printed to stderr before exiting; otherwise unexpected errors print a single
    line plus a hint. Tracebacks are never shown by default.
    """

    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Capture errors and exit with consistent messaging."""
        try:
            return f(*args, **kwargs)
        except click.Abort:
            # User declined a destructive-op confirmation (Ctrl+C / "n").
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
            # A command that exits directly with a specific code (e.g.
            # whoami's "not authenticated" -> ExitCode.AUTH_ERROR path) never
            # reaches `except Exception` below -- SystemExit is a
            # BaseException, not an Exception -- so without this, that
            # outcome would leave no trace in an active diagnostics file at
            # all. Purely observational: always re-raised unchanged, so the
            # exit code a caller sees is untouched. ERROR for a nonzero exit
            # (e.g. whoami's AUTH_ERROR) -- that IS the failure signal for a
            # command that never raised an Exception to carry one; INFO for a
            # clean 0/None exit, which is not a failure worth flagging as one.
            exit_level = logging.ERROR if e.code not in (None, 0) else logging.INFO
            _log_file_only("Command exited with code %s", e.code, level=exit_level, event="exit")
            raise
        except Exception as e:  # noqa: BLE001 -- this is @handle_errors, the CLI's top-level last-resort handler
            # Stash the real class before collapsing to SystemExit, so the
            # audit record names the actual failure.
            click_ctx = click.get_current_context(silent=True)
            if click_ctx is not None:
                click_ctx.meta[AUDIT_ERROR_KEY] = type(e).__name__
            sys.exit(render_cli_error(e))

    return wrapper  # type: ignore


# =============================================================================
# Exit Codes
# =============================================================================


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
    if isinstance(exc, AuthenticationError):  # incl. SessionExpiredError
        return ExitCode.AUTH_ERROR
    if isinstance(exc, ResourceNotFoundError):
        return ExitCode.NOT_FOUND
    if isinstance(exc, XNATConnectionError):
        # NetworkError, RequestTimeoutError, RetryExhaustedError, ServerUnreachableError
        return ExitCode.NETWORK_ERROR
    # ClientRequestError/ServerError ("server said no") and everything else.
    return ExitCode.GENERAL_ERROR
