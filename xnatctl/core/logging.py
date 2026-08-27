"""Logging utilities for xnatctl.

Provides structured logging with audit trail support.
"""

from __future__ import annotations

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
from typing import Any

from xnatctl.core.fsutil import (
    _private_opener,
    ensure_private_dir,
    open_private_append,
    restrict_permissions,
)
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

# Diagnostics log file (--log-file / XNATCTL_LOG_FILE / config `log_file:`).
# Same single-generation rotation policy as the audit log -- anything more is
# a worse logrotate.
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 1

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
    """Reject any record marked :data:`FILE_ONLY_ATTR`.

    Attached only to the stderr handler (see :func:`setup_logging`), never to
    a diagnostics file handler, so a file-only record still reaches the file
    without ever appearing on stderr -- at any verbosity tier.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep every record except one explicitly marked file-only."""
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

    # Configure root logger. Not basicConfig: that is a no-op the moment root
    # has ANY handler -- including a diagnostics file handler installed
    # before the first setup_logging() call (the root CLI callback does
    # exactly that) -- which would leave the process with no stderr handler
    # at all. Ensure the stderr stream handler exists explicitly instead,
    # keyed on "no non-file handler present", so call order cannot matter.
    root = logging.getLogger()
    if not any(not isinstance(h, logging.FileHandler) for h in root.handlers):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(stderr_handler)

    # Logger level vs. handler level, split on purpose: a diagnostics file
    # handler (install_log_file) always wants xnatctl's full DEBUG stream,
    # independent of what stderr is showing. The ROOT logger's level gates
    # whether a record is generated at all, so it has to drop to DEBUG
    # whenever a file handler is attached -- regardless of the stderr tier --
    # or DEBUG-level records (like the per-HTTP-attempt trace) would never
    # reach any handler to begin with. Each individual HANDLER then re-gates
    # on its own level, which is what keeps stderr showing only `level` once
    # root is DEBUG. install_log_file sets root to DEBUG itself too (it may
    # run after this function, e.g. when activated from config, which loads
    # after this call in @global_options), so the check here only needs to
    # avoid a LATER setup_logging() call (verbose merge, a second invocation
    # in one process) clobbering that back down.
    has_file_handler = any(isinstance(h, _JsonLinesFileHandler) for h in root.handlers)
    root.setLevel(logging.DEBUG if has_file_handler else level)

    # basicConfig created the stderr handler with no level of its own
    # (NOTSET: it filters solely through the root logger's level). Now
    # that root's level can sit at DEBUG for a diagnostics file, the stderr
    # handler needs an explicit level so it does not start showing DEBUG
    # lines too. Every non-file handler gets this -- there is normally only
    # the one stderr StreamHandler, but this stays correct if that changes.
    # It also gets _ExcludeFileOnlyFilter, so a FILE_ONLY_ATTR record (see
    # cli/common.py's command-outcome logging) never duplicates onto stderr
    # under -v/XNATCTL_DEBUG, where this handler's own level would otherwise
    # let it straight through.
    for handler in root.handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(level)
            if not any(isinstance(f, _ExcludeFileOnlyFilter) for f in handler.filters):
                handler.addFilter(_ExcludeFileOnlyFilter())

    # Redaction (and the correlation id) are invariants of the logging path,
    # not a call-site duty; installed separately and idempotently.
    install_redaction_filter()

    # Library verbosity, tiered as described above.
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
# Diagnostics Log File
# =============================================================================
#
# ``--log-file`` / ``XNATCTL_LOG_FILE`` / config ``log_file:`` (resolved in
# ``cli/common.py``/``cli/main.py``) attach one of these to the root logger via
# :func:`install_log_file`. Reuses the redaction/rotation/permission machinery
# above and below rather than building a second logging path: the point is a
# second HANDLER, not a second instrumentation layer.


class _JsonLinesFileHandler(logging.handlers.RotatingFileHandler):
    """A 0600, single-generation-rotation ``RotatingFileHandler``.

    Subclasses the stdlib rotator rather than hand-rolling rotation (as
    :class:`AuditLogger` does) so the well-tested rename-on-rollover logic is
    reused; only the *open* step changes. ``_open`` is the stdlib's own seam
    for this: :class:`logging.FileHandler` calls it both for the initial open
    and after every rollover, so overriding it once covers both.
    """

    def _open(self) -> Any:
        """Open the log file 0600, re-tightening a pre-existing file first.

        Mirrors :meth:`AuditLogger._ensure_private`: a file that predates
        this handler (or was copied in from elsewhere) keeps whatever mode it
        had until something checks. Checked here -- at initial open and again
        on every rollover -- rather than per line, which would mean a stat()
        call per log record for a handler whose whole purpose is to carry a
        high-volume DEBUG stream.
        """
        path = Path(self.baseFilename)
        ensure_private_dir(path.parent)
        if path.exists():
            restrict_permissions(path)
        return open(  # noqa: SIM115 - handed to the base class, not closed here
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            opener=_private_opener,
        )


class _FileScopeFilter(logging.Filter):
    """Admit xnatctl's own logger stream to the diagnostics file; reject the rest.

    httpx/httpcore are admitted too, but only when the wire-trace tier is on.

    Root has to sit at DEBUG for the file handler to see anything at DEBUG at
    all (see :func:`setup_logging`'s logger/handler level split) -- which
    means every OTHER third-party logger at its own DEBUG level becomes
    eligible too: pynetdicom's internal association/PDU trace, for one, which
    ``services/upload/dicom_store.py`` pulls in and which is not vetted the
    way xnatctl's own log calls are (see ``SECURITY.md``'s PHI-in-logs
    invariant -- :class:`RedactionFilter` only scrubs secret-shaped URL
    values, nothing DICOM-shaped). Without this, a third-party dependency's
    own debug logging could land PHI in a persisted file un-redacted. httpx/
    httpcore are exempted from the blanket rejection because their OWN logger
    levels (set right below, in :func:`setup_logging`) already implement the
    same "wire trace only under XNATCTL_DEBUG" tiering the docs promise --
    this re-checks it rather than trusting that gate alone, since a filter
    that silently depended on another function never changing is itself a
    future drift risk.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Admit xnatctl.* always; admit httpx/httpcore only under the trace tier."""
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

    #: ``extra=`` keys the two transport.py call sites may attach; anything
    #: else a caller puts in ``extra`` is not surfaced (LogRecord absorbs
    #: arbitrary ``extra`` keys as plain attributes with no marker of intent,
    #: so an allowlist -- not "every non-standard attribute" -- is what keeps
    #: this from also emitting unrelated internals some other logger call
    #: happens to pass as keyword args).
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

        # The single enforcement point for "nothing secret-shaped reaches the
        # file": record.getMessage() above is already redacted by
        # RedactionFilter, and this repeats that (idempotently -- redacting
        # already-redacted text is a no-op) rather than relying on remembering
        # it happened upstream. It is also what closes the ONE gap
        # RedactionFilter's own docstring flags -- a traceback is rendered by
        # the Formatter after filters run, so `exc` above was never touched --
        # and it covers any string a future extra= call site adds (`event`,
        # or a typed field someone passes as a string instead of an int) that
        # nobody remembered to redact at the call site. Non-string values
        # (status/attempt/duration_ms are ints) pass through untouched.
        for key, value in payload.items():
            if isinstance(value, str):
                payload[key] = redact_url_query(value)

        return json.dumps(payload, default=str)


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
        # Pinned explicitly rather than left to inherit the platform/locale
        # default: JsonLinesFormatter emits ensure_ascii=True JSON today, so
        # every byte written happens to be ASCII regardless -- but the file
        # is documented as UTF-8 (docs/debugging.rst), and this is what makes
        # that true rather than incidentally true.
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(JsonLinesFormatter())
    # Root has to sit at DEBUG for xnatctl's own DEBUG stream to reach this
    # handler at all, which -- with no further gate -- would also hand every
    # OTHER at-DEBUG third-party logger (pynetdicom, etc.) a free pass into a
    # persisted file. This filter is what keeps the file scoped to xnatctl's
    # own (redacted) stream, the same way it would appear under -v, plus
    # httpx/httpcore only under the wire-trace tier. See _FileScopeFilter.
    handler.addFilter(_FileScopeFilter())
    root.addHandler(handler)

    # Independent of whatever setup_logging() last set root to: a file
    # handler always wants the full stream, and this must hold regardless of
    # whether install_log_file ran before or after the most recent
    # setup_logging() call (config-driven activation runs after it, since
    # config loads after the first setup_logging() call in @global_options).
    root.setLevel(logging.DEBUG)

    # Picks up this new handler (and re-confirms the stderr one, harmlessly).
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
        rotated = self.log_file.with_name(self.log_file.name + ".1")
        self.log_file.replace(rotated)
        # The rename preserves the source file's mode, and nothing ever
        # revisits the rotated generation -- so a log that was looser than
        # 0600 when it crossed the threshold would stay readable forever.
        # Tighten it at the one moment it is created.
        try:
            if stat.S_IMODE(rotated.stat().st_mode) & 0o077:
                restrict_permissions(rotated)
        except OSError:
            pass


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
