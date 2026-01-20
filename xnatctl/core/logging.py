"""Logging utilities for xnatctl.

Provides structured logging with audit trail support.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator, Optional

# =============================================================================
# Constants
# =============================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AUDIT_LOGGER_NAME = "xnatctl.audit"


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

    Args:
        level: Base logging level.
        quiet: If True, only show errors.
        verbose: If True, show debug messages.
    """
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG

    # Configure root logger
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        stream=sys.stderr,
    )

    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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
        logger: Optional[logging.Logger] = None,
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
        self.start_time: Optional[datetime] = None

    def __enter__(self) -> "LogContext":
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
    logger: Optional[logging.Logger] = None,
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

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize audit logger.

        Args:
            logger: Logger instance.
        """
        self.logger = logger or logging.getLogger(AUDIT_LOGGER_NAME)

    def log_operation(
        self,
        operation: str,
        *,
        project: Optional[str] = None,
        subject: Optional[str] = None,
        session: Optional[str] = None,
        user: Optional[str] = None,
        success: bool = True,
        details: Optional[dict[str, Any]] = None,
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
