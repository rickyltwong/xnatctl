"""Logging utilities for xnatctl.

Provides structured logging with audit trail support.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from xnatctl.core.fsutil import ensure_private_dir, open_private_append, restrict_permissions
from xnatctl.core.redact import redact_url_query

# =============================================================================
# Constants
# =============================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AUDIT_LOGGER_NAME = "xnatctl.audit"

# Audit trail. Defined here rather than imported from core.config so
# this module stays free of the config import chain.
AUDIT_LOG_FILE = Path.home() / ".config" / "xnatctl" / "audit.log"
AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024

DEBUG_ENV_VAR = "XNATCTL_DEBUG"
# Values that explicitly DISABLE debug output. Any other non-empty value
# enables it, but the common "falsey" spellings must NOT fail open.
_DEBUG_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def debug_env_enabled() -> bool:
    """Return True when ``XNATCTL_DEBUG`` asks for debug output.

    Mirrors ``gh``'s ``GH_DEBUG``, so diagnostics are obtainable even for
    failures that happen before ``--verbose`` is parsed. Defined here rather
    than in the CLI layer so the logging setup and the CLI traceback policy
    share one parser instead of drifting apart.
    """
    raw = os.environ.get(DEBUG_ENV_VAR)
    return raw is not None and raw.strip().lower() not in _DEBUG_OFF_VALUES


# =============================================================================
# Redaction
# =============================================================================


class RedactionFilter(logging.Filter):
    """Scrub secret-shaped URL values out of every record a handler emits.

    Installing this once on the root handler makes redaction an invariant of
    the logging path instead of something each call site has to remember. That
    is the precondition for verbose HTTP diagnostics, which log full request
    URLs -- and those carry query-string tokens.

    Scope boundary: this covers the formatted message only. Exception
    tracebacks are rendered by the Formatter *after* filters run, so a secret
    inside a traceback is not scrubbed here. The CLI's own traceback path
    already redacts (``cli/common.py``); a Formatter-level fix belongs with
    future log-file work.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite ``record`` in place when it contains something to redact."""
        message = record.getMessage()
        redacted = redact_url_query(message)
        if redacted != message:
            # Only collapse to the interpolated text when we actually changed
            # something, so untouched records keep their lazy %-args for any
            # other handler.
            record.msg = redacted
            record.args = None
        return True


def install_redaction_filter(logger: logging.Logger | None = None) -> None:
    """Attach a :class:`RedactionFilter` to each of ``logger``'s handlers, once.

    Handler-level rather than logger-level on purpose: a filter set on a logger
    only sees records logged directly to it, not records propagated up from
    child loggers, and every xnatctl logger is a child of root.

    Idempotent, because ``setup_logging`` runs from both the CLI root group and
    ``@global_options``.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(existing, RedactionFilter) for existing in handler.filters):
            handler.addFilter(RedactionFilter())


# =============================================================================
# Logger Setup
# =============================================================================


def setup_logging(
    level: int = logging.INFO,
    *,
    quiet: bool = False,
    verbose: bool = False,
) -> None:
    """Configure logging for xnatctl.

    Three tiers of HTTP visibility:

    * default -- httpx/httpcore stay at WARNING, so normal runs are quiet;
    * ``--verbose`` -- xnatctl's own DEBUG lines plus httpx at INFO, which is
      one line per request;
    * ``XNATCTL_DEBUG=1`` -- adds httpcore at DEBUG for a full wire trace.
      Deliberately not on ``--verbose``: httpcore DEBUG is per-socket-event and
      drowns the diagnostics people actually came for.

    ``--quiet`` wins over both: an explicit flag beats an ambient env var.

    Args:
        level: Base logging level.
        quiet: If True, only show errors.
        verbose: If True, show debug messages.
    """
    trace = debug_env_enabled()

    if quiet:
        level = logging.ERROR
    elif verbose or trace:
        level = logging.DEBUG

    # Configure root logger
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        stream=sys.stderr,
    )
    # basicConfig is a no-op when handlers already exist, so an earlier default
    # call must not pin a later --verbose run to the old level.
    logging.getLogger().setLevel(level)

    # Redaction is an invariant of the logging path, not a call-site duty
    # . basicConfig above is a no-op once root already has handlers, so
    # this is installed separately and idempotently.
    install_redaction_filter()

    # Library verbosity, tiered as described above. This used to be an
    # unconditional WARNING, which meant -v could never show wire activity.
    if quiet:
        http_level = logging.WARNING
    elif trace:
        http_level = logging.DEBUG
    elif verbose:
        http_level = logging.INFO
    else:
        http_level = logging.WARNING
    logging.getLogger("httpx").setLevel(http_level)
    logging.getLogger("httpcore").setLevel(
        logging.DEBUG if trace and not quiet else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance.

    Args:
        name: Logger name (typically __name__).

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)


# =============================================================================
# Log Context
# =============================================================================


class LogContext:
    """Context manager for structured logging with context fields."""

    def __init__(
        self,
        operation: str,
        logger: logging.Logger | None = None,
        **context: Any,
    ):
        """Initialize log context.

        Args:
            operation: Name of the operation.
            logger: Logger instance.
            **context: Additional context fields.
        """
        self.operation = operation
        self.logger = logger or get_logger(__name__)
        self.context = context
        self.start_time: datetime | None = None

    def __enter__(self) -> LogContext:
        """Enter context and log start."""
        self.start_time = datetime.now()
        ctx_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
        self.logger.info("Starting %s (%s)", self.operation, ctx_str)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context and log completion."""
        duration = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0

        if exc_type:
            self.logger.error(
                "%s failed after %.2fs: %s",
                self.operation,
                duration,
                exc_val,
            )
        else:
            self.logger.info("%s completed in %.2fs", self.operation, duration)

    def log(self, level: int, message: str, *args: Any) -> None:
        """Log a message with context.

        Args:
            level: Log level.
            message: Message format string.
            *args: Format arguments.
        """
        ctx_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
        full_message = f"[{self.operation}] {message} ({ctx_str})"
        self.logger.log(level, full_message, *args)

    def info(self, message: str, *args: Any) -> None:
        """Log info message."""
        self.log(logging.INFO, message, *args)

    def warning(self, message: str, *args: Any) -> None:
        """Log warning message."""
        self.log(logging.WARNING, message, *args)

    def error(self, message: str, *args: Any) -> None:
        """Log error message."""
        self.log(logging.ERROR, message, *args)

    def debug(self, message: str, *args: Any) -> None:
        """Log debug message."""
        self.log(logging.DEBUG, message, *args)


@contextmanager
def log_context(
    operation: str,
    logger: logging.Logger | None = None,
    **context: Any,
) -> Generator[LogContext, None, None]:
    """Context manager for structured logging.

    Args:
        operation: Name of the operation.
        logger: Logger instance.
        **context: Additional context fields.

    Yields:
        LogContext instance.
    """
    ctx = LogContext(operation, logger, **context)
    with ctx:
        yield ctx


# =============================================================================
# Audit Logger
# =============================================================================


class AuditLogger:
    """Append-only local record of destructive operations.

    Why keep a client-side trail at all: xnatctl deletes subjects, sessions and
    scans, and XNAT's own audit log is server-side and frequently not readable
    by the person who ran the command. "Who deleted SUB001 from this
    workstation, when, and against which server" should be answerable locally.

    Why it is on by default: the identifiers recorded here are the ones the
    user just typed, which their shell history already holds -- so this adds no
    new category of exposure -- and an audit log that has to be switched on
    before the incident is useless. The file is created 0600 like the session
    cache.

    Writes are best-effort: an unwritable audit log warns, it never aborts the
    operation being audited.
    """

    def __init__(
        self,
        log_file: Path | None = None,
        logger: logging.Logger | None = None,
    ):
        """Initialize audit logger.

        Args:
            log_file: Destination JSON-lines file.
            logger: Logger used for write failures (not for the records).
        """
        self.log_file = log_file if log_file is not None else AUDIT_LOG_FILE
        self.logger = logger or logging.getLogger(AUDIT_LOGGER_NAME)

    def log_operation(
        self,
        operation: str,
        *,
        project: str | None = None,
        subject: str | None = None,
        session: str | None = None,
        user: str | None = None,
        success: bool = True,
        details: dict[str, Any] | None = None,
        server: str | None = None,
        profile: str | None = None,
        command: str | None = None,
        dry_run: bool = False,
        error: str | None = None,
    ) -> None:
        """Append one auditable operation to the log.

        Args:
            operation: Name of the operation.
            project: Project ID.
            subject: Subject ID.
            session: Session ID.
            user: Username performing the operation.
            success: Whether operation succeeded.
            details: Additional details (values are redacted).
            server: XNAT server URL (redacted before writing).
            profile: Active profile name.
            command: Full command path, e.g. "xnatctl subject delete".
            dry_run: Whether this was a preview rather than a real change.
            error: Exception class name when the operation failed.
        """
        record: dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "operation": operation,
            "success": success,
        }

        optional: dict[str, Any] = {
            "command": command,
            "profile": profile,
            "server": server,
            "user": user,
            "project": project,
            "subject": subject,
            "session": session,
            "error": error,
        }
        for key, value in optional.items():
            if value:
                record[key] = _redact_value(value)

        if dry_run:
            record["dry_run"] = True
        if details:
            record["details"] = {k: _redact_value(v) for k, v in details.items()}

        self._append(record)

    def _append(self, record: dict[str, Any]) -> None:
        """Write one JSON line, rotating first if the log has grown too large."""
        try:
            self._rotate_if_needed()
            ensure_private_dir(self.log_file.parent)
            with open_private_append(self.log_file) as handle:
                handle.write(json.dumps(record, default=str) + "\n")
            self._ensure_private()
        except OSError as e:
            # Never let bookkeeping break the operation being audited.
            self.logger.warning("Could not write the audit log at %s: %s", self.log_file, e)

    def _ensure_private(self) -> None:
        """Tighten a log that others can read.

        ``open_private_append``'s mode only applies to files it creates, so a
        log written before this code existed -- or copied in from elsewhere --
        keeps whatever mode it had. An append-only file cannot be swapped in
        atomically the way the session cache is, so the mode is checked on each
        write instead: one ``stat``, and only on destructive operations.
        """
        try:
            mode = self.log_file.stat().st_mode
        except OSError:
            return
        if stat.S_IMODE(mode) & 0o077:
            restrict_permissions(self.log_file)

    def _rotate_if_needed(self) -> None:
        """Roll the log over once it passes :data:`AUDIT_LOG_MAX_BYTES`.

        A single generation is deliberate: anything more is a worse reimple-
        mentation of logrotate, which is the right tool once a user cares.
        """
        try:
            size = self.log_file.stat().st_size
        except OSError:
            return
        if size < AUDIT_LOG_MAX_BYTES:
            return
        self.log_file.replace(self.log_file.with_name(self.log_file.name + ".1"))


def _redact_value(value: Any) -> Any:
    """Redact a value destined for the audit log.

    URLs are the realistic leak here -- a server URL with embedded credentials,
    or a path carrying a query string.
    """
    return redact_url_query(value) if isinstance(value, str) else value


def get_audit_logger() -> AuditLogger:
    """Get the audit logger instance.

    Returns:
        AuditLogger instance.
    """
    return AuditLogger()
