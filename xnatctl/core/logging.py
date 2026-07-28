"""Logging utilities for xnatctl.

Provides structured logging with audit trail support.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from xnatctl.core.redact import redact_url_query

# =============================================================================
# Constants
# =============================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AUDIT_LOGGER_NAME = "xnatctl.audit"

DEBUG_ENV_VAR = "XNATCTL_DEBUG"
# Values that explicitly DISABLE debug output. Any other non-empty value
# enables it, but the common "falsey" spellings must NOT fail open.
_DEBUG_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def debug_env_enabled() -> bool:
    """Return True when ``XNATCTL_DEBUG`` asks for debug output.

    Mirrors ``gh``'s ``GH_DEBUG``, so diagnostics are obtainable even for
    failures that happen before ``--verbose`` is parsed. Defined here rather
    than in the CLI layer so the logging setup and the CLI traceback policy
    (MAINT-02) share one parser instead of drifting apart.
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
    is the precondition for MAINT-01: verbose HTTP diagnostics log full request
    URLs, and those carry query-string tokens (SEC-09).

    Scope boundary: this covers the formatted message only. Exception
    tracebacks are rendered by the Formatter *after* filters run, so a secret
    inside a traceback is not scrubbed here. The CLI's own traceback path
    already redacts (``cli/common.py``); a Formatter-level fix belongs with
    GAP-04's log-file work.
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

    Three tiers of HTTP visibility (MAINT-01):

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
    # (SEC-09). basicConfig above is a no-op once root already has handlers, so
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
    """Logger for audit trail of operations."""

    def __init__(self, logger: logging.Logger | None = None):
        """Initialize audit logger.

        Args:
            logger: Logger instance.
        """
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
    ) -> None:
        """Log an auditable operation.

        Args:
            operation: Name of the operation.
            project: Project ID.
            subject: Subject ID.
            session: Session ID.
            user: Username performing the operation.
            success: Whether operation succeeded.
            details: Additional details.
        """
        audit_record = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "success": success,
        }

        if project:
            audit_record["project"] = project
        if subject:
            audit_record["subject"] = subject
        if session:
            audit_record["session"] = session
        if user:
            audit_record["user"] = user
        if details:
            audit_record["details"] = details

        level = logging.INFO if success else logging.WARNING
        self.logger.log(level, "AUDIT: %s", audit_record)


def get_audit_logger() -> AuditLogger:
    """Get the audit logger instance.

    Returns:
        AuditLogger instance.
    """
    return AuditLogger()
