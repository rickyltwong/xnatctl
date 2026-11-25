"""Logging utilities for xnatctl.

Provides structured logging with audit trail support.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import stat
import sys
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from xnatctl.core.fsutil import (
    _private_opener,
    ensure_private_dir,
    open_private_append,
    restrict_permissions,
)
from xnatctl.core.redact import redact_session_cookies, redact_url_query

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AUDIT_LOGGER_NAME = "xnatctl.audit"

AUDIT_LOG_FILE = Path.home() / ".config" / "xnatctl" / "audit.log"
AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 1

DEBUG_ENV_VAR = "XNATCTL_DEBUG"
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


class RedactionFilter(logging.Filter):
    """Scrub secret-shaped URL values and session cookies out of every record.

    Installing this once on the root handler makes redaction an invariant of
    the logging path instead of something each call site has to remember. That
    is the precondition for verbose HTTP diagnostics, which log full request
    URLs -- and those carry query-string tokens -- and, under the httpcore
    wire trace, every response header, XNAT's ``Set-Cookie: JSESSIONID`` too.

    Scope boundary: this covers the formatted message only. Exception
    tracebacks are rendered by the Formatter *after* filters run, so a secret
    inside a traceback is not scrubbed here. The CLI's own error path redacts
    separately (``cli/common/errors.py``), and :class:`JsonLinesFormatter`
    scrubs every field of the diagnostics file, the traceback included.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite ``record`` in place when it contains something to redact."""
        try:
            message = record.getMessage()
        except (TypeError, ValueError, KeyError, OverflowError):
            # The errors %-formatting raises for arguments that do not fit the
            # format string. Raising here would escape the logging call itself.
            message = f"{record.msg} {record.args!r}"
            record.msg, record.args = message, None
        redacted = redact_session_cookies(redact_url_query(message))
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


#: The current invocation's correlation id, or ``"-"`` before one is minted.
#:
#: A plain module-level global, deliberately NOT a ``contextvars.ContextVar``:
#: xnatctl's parallel operations (uploads, downloads, transfers) run client
#: HTTP calls -- and therefore most of the DEBUG lines a diagnostics file
#: exists to capture -- on worker threads from ``ThreadPoolExecutor``, and a
#: plain ``threading.Thread`` does NOT inherit the context (and therefore the
#: contextvars) of the thread that created it. A ``ContextVar`` would make the
#: id visible on the main thread's records and silently "-" everywhere else,
#: which defeats the point of a single id per invocation. A module global,
#: read under the GIL, is shared by every thread in the process for free.
_correlation_id: str = "-"


def new_correlation_id() -> str:
    """Mint and store a fresh id for this process invocation.

    Called exactly once per ``xnatctl`` invocation, from the root CLI group
    (``cli/main.py``) -- the one call site every command passes through
    before any subcommand logic runs. Calling it again (as happens when
    running the CLI's ``cli()`` callback more than once within a single
    process, e.g. in tests) intentionally starts a new id, matching a real
    new invocation.
    """
    global _correlation_id
    _correlation_id = uuid.uuid4().hex[:12]
    return _correlation_id


class CorrelationFilter(logging.Filter):
    """Stamp every record with the current invocation's correlation id.

    Handler-level, for the same reason as :class:`RedactionFilter`: a filter
    on a handler sees every record that reaches it via propagation, not just
    ones logged directly to root. ``record.corr`` defaults to ``"-"`` for any
    record emitted before :func:`new_correlation_id` has run.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach the current correlation id to ``record`` and keep it."""
        record.corr = _correlation_id
        return True


#: LogRecord attribute a caller sets (via ``extra={FILE_ONLY_ATTR: True}``) to
#: mean "capture this in an active diagnostics file, but never print it to
#: stderr." Exists for command-outcome logging in ``cli/common.py``: the
#: stderr rendering of a CLI failure already happens via ``click.echo``/
#: ``print_error``, which do not go through the logging module at all, so a
#: *second*, logging-based emission of the same information is needed purely
#: to reach the file -- and must not also duplicate onto stderr under
#: ``-v``/``XNATCTL_DEBUG``, where the stderr handler's own level would
#: otherwise let it straight through alongside the existing click.echo text.
FILE_ONLY_ATTR = "xnatctl_file_only"


class _ExcludeFileOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, FILE_ONLY_ATTR, False)


def install_redaction_filter(logger: logging.Logger | None = None) -> None:
    """Attach a :class:`RedactionFilter` and :class:`CorrelationFilter`, once each.

    Handler-level rather than logger-level on purpose: a filter set on a logger
    only sees records logged directly to it, not records propagated up from
    child loggers, and every xnatctl logger is a child of root. Both filters
    ride this one install path so a diagnostics file handler picks up
    redaction and the correlation id the same way the stderr handler does.

    Idempotent, because ``setup_logging``/``install_log_file`` run from
    multiple call sites over the life of one invocation.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(existing, RedactionFilter) for existing in handler.filters):
            handler.addFilter(RedactionFilter())
        if not any(isinstance(existing, CorrelationFilter) for existing in handler.filters):
            handler.addFilter(CorrelationFilter())


class _CurrentStderrHandler(logging.StreamHandler[TextIO]):
    """Write to whatever ``sys.stderr`` is when a record is emitted.

    ``logging.lastResort`` does the same. A handler bound to the stream at setup
    would keep writing to a replaced one, such as an earlier in-process run's
    captured stderr.
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        raise AttributeError("this handler always writes to the current sys.stderr")

    def handleError(self, record: logging.LogRecord) -> None:
        # The default report also goes to sys.stderr, which may be the stream that failed.
        with contextlib.suppress(OSError, ValueError):
            super().handleError(record)


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

    pydicom's own log stream is never shown, on any tier.

    Independent of all three tiers: once :func:`install_log_file` has attached
    a diagnostics file handler (this invocation or a prior call in the same
    process), this function keeps the root logger at DEBUG rather than
    resetting it to ``level`` -- see the logger/handler level split below.
    The stderr tier itself never changes because of a file handler; only
    which handler sees which records does.

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

    root = logging.getLogger()
    if not any(not isinstance(h, logging.FileHandler) for h in root.handlers):
        stderr_handler = _CurrentStderrHandler()
        stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(stderr_handler)

    has_file_handler = any(isinstance(h, _JsonLinesFileHandler) for h in root.handlers)
    root.setLevel(logging.DEBUG if has_file_handler else level)

    for handler in root.handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(level)
            if not any(isinstance(f, _ExcludeFileOnlyFilter) for f in handler.filters):
                handler.addFilter(_ExcludeFileOnlyFilter())

    install_redaction_filter()

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
    # pydicom's log text can quote element values, and pydicom prints most of
    # it as Python warnings anyway. Not by level: importing pydicom, which
    # happens later, resets its level.
    logging.getLogger("pydicom").propagate = False


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance.

    Args:
        name: Logger name (typically __name__).

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)


class DetailsWithheldError(Exception):
    """Stands in for a logged exception whose text must not be recorded."""


def withhold_details(exc: BaseException) -> DetailsWithheldError:
    """Return a stand-in for ``exc`` to pass as ``exc_info=``: its frames, its type, no text.

    For a failure whose message can quote DICOM element values or a session
    token, which no log record may carry. Every handler, a library caller's
    included, then sees where it failed but not what the exception said.
    """
    return DetailsWithheldError(type(exc).__qualname__).with_traceback(exc.__traceback__)


class _JsonLinesFileHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default report prints the record's own exception chain, unredacted.
        if logging.raiseExceptions and sys.stderr:
            failure = redact_session_cookies(redact_url_query(str(sys.exc_info()[1])))
            # Nor may a closed stderr or a broken pipe end the command.
            with contextlib.suppress(OSError, ValueError):
                sys.stderr.write(f"xnatctl: could not write the diagnostics file: {failure}\n")

    def _open(self) -> Any:
        path = Path(self.baseFilename)
        ensure_private_dir(path.parent)
        if path.exists():
            restrict_permissions(path)
        return open(
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            opener=_private_opener,
        )


class _FileScopeFilter(logging.Filter):
    """Admit xnatctl's own log stream to the diagnostics file; reject the rest.

    httpx/httpcore are admitted too, but only when the wire-trace tier is on.

    This allowlist is a PHI boundary, not a noise filter. Root has to sit at
    DEBUG for the file handler to see xnatctl's DEBUG records, which makes
    every other third-party logger's DEBUG output eligible too, such as
    pynetdicom's association and PDU trace. That output is not vetted the way
    xnatctl's own log calls are (see ``SECURITY.md``, "PHI in logs and
    errors"), and :class:`RedactionFilter` scrubs secret-shaped URL values and
    session cookies, nothing DICOM-shaped. Without this filter a dependency's debug
    logging could write patient identifiers to a persisted file.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        if name == "xnatctl" or name.startswith("xnatctl."):
            return True
        if name == "httpx" or name == "httpcore" or name.startswith(("httpx.", "httpcore.")):
            return debug_env_enabled()
        return False


class JsonLinesFormatter(logging.Formatter):
    """Render one JSON object per line for :class:`_JsonLinesFileHandler`.

    Envelope: ``ts``, ``level``, ``logger``, ``corr``, ``msg``, plus an
    optional typed ``event``/``method``/``status``/``attempt``/``duration_ms``
    when a call site passed them via ``extra=`` (currently the two per-attempt
    HTTP debug lines in ``core/transport.py``), and an optional ``exc`` when
    the record carries exception info.

    The stability tier for this shape is Unstable (see ``docs/stability.rst``)
    -- it is a diagnostic artifact for humans/AI, not a scripted interface,
    which keeps freedom to add or rename fields.
    """

    _EVENT_FIELDS = ("method", "status", "attempt", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        """Return one redacted JSON line for ``record``."""
        payload: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "corr": getattr(record, "corr", "-"),
            "msg": record.getMessage(),
        }

        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
            for field_name in self._EVENT_FIELDS:
                value = getattr(record, field_name, None)
                if value is not None:
                    payload[field_name] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        for key, value in payload.items():
            if not isinstance(value, int | float):
                payload[key] = redact_session_cookies(redact_url_query(str(value)))

        return json.dumps(payload)


def install_log_file(path: str | Path) -> None:
    """Attach a JSON-lines diagnostics handler at ``path`` to the root logger.

    Always captures the full xnatctl DEBUG stream (see :func:`setup_logging`
    for how the logger/handler level split makes that independent of the
    stderr tier), redacted and correlation-stamped the same way every other
    handler is.

    Idempotent: calling this again for a path already attached (compared
    resolved/absolute, so ``PATH`` and ``./PATH`` from the same cwd match) is
    a no-op, since flag/env/config activation and the root-vs-subcommand
    ``@global_options`` merge can each reach this for the same invocation.

    At most one diagnostics file handler is ever active: if a handler for a
    DIFFERENT path is already attached (the root callback's config-tier
    resolution, about to be superseded by a subcommand's own more specific
    ``--log-file``; or a prior operation in the same process, for a library
    caller), it is removed first via :func:`remove_log_file`. Without this, a
    later, more specific resolution would just add a second handler instead
    of replacing the first, and both would keep receiving every record.
    """
    resolved = Path(path).expanduser().resolve()
    root = logging.getLogger()

    for handler in root.handlers:
        if isinstance(handler, _JsonLinesFileHandler) and Path(handler.baseFilename) == resolved:
            return

    remove_log_file()
    ensure_private_dir(resolved.parent)
    handler = _JsonLinesFileHandler(
        str(resolved),
        mode="a",
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(JsonLinesFormatter())
    handler.addFilter(_FileScopeFilter())
    root.addHandler(handler)

    root.setLevel(logging.DEBUG)

    install_redaction_filter()


def remove_log_file(path: str | Path | None = None) -> None:
    """Detach and close the diagnostics file handler(s) on the root logger.

    ``install_log_file``'s handler otherwise outlives the operation that
    requested it for the rest of the PROCESS -- harmless for the CLI itself
    (one process per invocation, reclaimed on exit), but a real footgun for a
    library caller that runs more than one xnatctl operation in one process
    (e.g. via the connect facade or by invoking the CLI programmatically more
    than once): without this, a second operation with a different
    ``--log-file`` -- or none at all -- keeps appending into the first one.

    Args:
        path: Remove only the handler for this specific (resolved) path.
            Omit to remove every diagnostics file handler currently attached.
    """
    root = logging.getLogger()
    resolved = Path(path).expanduser().resolve() if path is not None else None
    for handler in list(root.handlers):
        if not isinstance(handler, _JsonLinesFileHandler):
            continue
        if resolved is not None and Path(handler.baseFilename) != resolved:
            continue
        root.removeHandler(handler)
        handler.close()


class LogContext:
    """Context manager for structured logging with context fields."""

    def __init__(
        self,
        operation: str,
        logger: logging.Logger | None = None,
        **context: Any,
    ):
        self.operation = operation
        self.logger = logger or get_logger(__name__)
        self.context = context
        self.start_time: datetime | None = None

    def __enter__(self) -> LogContext:
        self.start_time = datetime.now()
        ctx_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
        self.logger.info("Starting %s (%s)", self.operation, ctx_str)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
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
        try:
            self._rotate_if_needed()
            ensure_private_dir(self.log_file.parent)
            with open_private_append(self.log_file) as handle:
                handle.write(json.dumps(record, default=str) + "\n")
            self._ensure_private()
        except OSError as e:
            self.logger.warning("Could not write the audit log at %s: %s", self.log_file, e)

    def _ensure_private(self) -> None:
        try:
            mode = self.log_file.stat().st_mode
        except OSError:
            return
        if stat.S_IMODE(mode) & 0o077:
            restrict_permissions(self.log_file)

    def _rotate_if_needed(self) -> None:
        try:
            size = self.log_file.stat().st_size
        except OSError:
            return
        if size < AUDIT_LOG_MAX_BYTES:
            return
        rotated = self.log_file.with_name(self.log_file.name + ".1")
        self.log_file.replace(rotated)
        try:
            if stat.S_IMODE(rotated.stat().st_mode) & 0o077:
                restrict_permissions(rotated)
        except OSError:
            pass


def _redact_value(value: Any) -> Any:
    return redact_url_query(value) if isinstance(value, str) else value


def get_audit_logger() -> AuditLogger:
    """Get the audit logger instance.

    Returns:
        AuditLogger instance.
    """
    return AuditLogger()
