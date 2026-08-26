"""Diagnostics log file: ``--log-file`` / ``XNATCTL_LOG_FILE`` / config ``log_file:``.

Covers the JSON-lines handler/formatter machinery in ``core/logging.py``
directly, then the CLI wiring (flag/env/config precedence, the stderr-tier
split, append-across-invocations, and a failing command still producing an
artifact) through the real ``cli`` root group -- following the hidden-probe
pattern already used by ``tests/test_global_options.py``.

What this module deliberately does NOT re-test: retry/backoff numerics
(``test_core_client_retry.py``), redaction of a plain log *message*
(``test_logging_redaction.py``) -- only the traceback-redaction gap this
feature closes is new here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner
from conftest import config_seam

from xnatctl.cli.common import Context, ExitCode, global_options, handle_errors
from xnatctl.cli.main import cli
from xnatctl.core.client import XNATClient
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ServerUnreachableError
from xnatctl.core.fsutil import POSIX_PERMISSIONS
from xnatctl.core.logging import (
    LOG_FILE_BACKUP_COUNT,
    JsonLinesFormatter,
    _JsonLinesFileHandler,
    install_log_file,
    new_correlation_id,
    remove_log_file,
)

SECRET_URL = "https://xnat.example.org/data?token=s3cret"


def read_lines(path: Path) -> list[dict]:
    """Parse a JSON-lines diagnostics file into a list of records."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _strip_timestamps(text: str) -> str:
    """Normalize stderr's ``LOG_DATE_FORMAT`` timestamps before comparing.

    Two invocations a moment apart can straddle a wall-clock second, which
    would otherwise make an equality check on raw output flaky for no reason
    related to what is actually being tested.
    """
    return _TIMESTAMP_RE.sub("<TS>", text)


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Snapshot and restore the root logger, closing any handler this test added."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# =============================================================================
# JsonLinesFormatter -- shape and redaction, in isolation
# =============================================================================


def _make_record(
    msg: str,
    *,
    level: int = logging.DEBUG,
    exc_info: tuple | None = None,
    corr: str = "abc123",
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord("xnatctl.core.transport", level, __file__, 1, msg, (), exc_info)
    record.corr = corr  # type: ignore[attr-defined]
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


class TestJsonLinesFormatterShape:
    def test_envelope_has_the_documented_fields(self) -> None:
        record = _make_record("GET /data/projects -> 200")
        payload = json.loads(JsonLinesFormatter().format(record))

        assert payload["level"] == "DEBUG"
        assert payload["logger"] == "xnatctl.core.transport"
        assert payload["corr"] == "abc123"
        assert payload["msg"] == "GET /data/projects -> 200"
        assert "ts" in payload
        assert "exc" not in payload
        assert "event" not in payload

    def test_missing_corr_defaults_to_dash(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
        payload = json.loads(JsonLinesFormatter().format(record))
        assert payload["corr"] == "-"

    def test_typed_event_fields_are_carried(self) -> None:
        record = _make_record(
            "GET /data/projects -> 200 in 12ms (attempt 1/4)",
            extra={
                "event": "http_request",
                "method": "GET",
                "status": 200,
                "attempt": 1,
                "duration_ms": 12,
            },
        )
        payload = json.loads(JsonLinesFormatter().format(record))

        assert payload["event"] == "http_request"
        assert payload["method"] == "GET"
        assert payload["status"] == 200
        assert payload["attempt"] == 1
        assert payload["duration_ms"] == 12

    def test_no_event_means_no_typed_fields(self) -> None:
        """A plain message-only record (no extra=) must stay a bare envelope."""
        record = _make_record("plain line")
        payload = json.loads(JsonLinesFormatter().format(record))
        assert "method" not in payload
        assert "status" not in payload


class TestJsonLinesFormatterTracebackRedaction:
    """The gap RedactionFilter's own docstring flags as deferred: tracebacks."""

    def test_traceback_secret_is_redacted(self) -> None:
        try:
            raise RuntimeError(f"fetch failed: {SECRET_URL}")
        except RuntimeError:
            record = _make_record("Operation failed", level=logging.ERROR, exc_info=sys.exc_info())

        payload = json.loads(JsonLinesFormatter().format(record))

        assert "s3cret" not in payload["exc"]
        assert "token=***" in payload["exc"]

    def test_traceback_without_secrets_is_left_readable(self) -> None:
        try:
            raise RuntimeError("plain failure, no urls here")
        except RuntimeError:
            record = _make_record("Operation failed", level=logging.ERROR, exc_info=sys.exc_info())

        payload = json.loads(JsonLinesFormatter().format(record))
        assert "plain failure, no urls here" in payload["exc"]

    def test_no_exc_info_means_no_exc_key(self) -> None:
        record = _make_record("Operation failed", level=logging.ERROR)
        payload = json.loads(JsonLinesFormatter().format(record))
        assert "exc" not in payload


class TestJsonLinesFormatterExtraFieldRedaction:
    """RedactionFilter only ever rewrites record.msg -- a string a call site
    hands to the formatter through ``extra=`` (an ``event`` name, or a typed
    field someone passes as a string) never goes through that filter at all.
    The formatter has to enforce the "nothing secret-shaped reaches the
    file" invariant itself, for every string-valued field, not rely on the
    upstream filter alone.
    """

    def test_a_secret_bearing_event_string_is_redacted(self) -> None:
        record = _make_record("plain message", extra={"event": SECRET_URL})
        payload = json.loads(JsonLinesFormatter().format(record))

        assert "s3cret" not in payload["event"]
        assert "token=***" in payload["event"]

    def test_a_secret_bearing_typed_field_is_redacted(self) -> None:
        record = _make_record(
            "plain message",
            extra={"event": "http_request", "method": SECRET_URL},
        )
        payload = json.loads(JsonLinesFormatter().format(record))

        assert "s3cret" not in payload["method"]
        assert "token=***" in payload["method"]

    def test_non_string_typed_fields_pass_through_untouched(self) -> None:
        record = _make_record(
            "plain message",
            extra={"event": "http_request", "status": 200, "attempt": 1, "duration_ms": 12},
        )
        payload = json.loads(JsonLinesFormatter().format(record))

        assert payload["status"] == 200
        assert payload["attempt"] == 1
        assert payload["duration_ms"] == 12


@pytest.mark.usefixtures("clean_root")
class TestCliCommonLoggerNamePinned:
    """xnatctl/cli/common/errors.py's diagnostics logger must stay named
    ``xnatctl.cli.common``, regardless of which submodule under the
    ``cli/common/`` package the call happens to live in. A bare
    ``logging.getLogger(__name__)`` there would silently rename it to
    ``xnatctl.cli.common.errors``, an observable change to the JSON-lines
    diagnostics artifact's ``logger`` field from a pure file-layout detail.
    """

    def test_log_file_only_uses_the_pinned_logger_name(self, tmp_path: Path) -> None:
        from xnatctl.cli.common.errors import _log_file_only

        path = tmp_path / "diag.log"
        install_log_file(path)
        try:
            _log_file_only("test message", event="test")
        finally:
            remove_log_file(path)

        (entry,) = read_lines(path)
        assert entry["logger"] == "xnatctl.cli.common"


# =============================================================================
# install_log_file -- handler wiring, permissions, rotation, level split
# =============================================================================


@pytest.mark.usefixtures("clean_root")
class TestInstallLogFile:
    def test_file_receives_debug_lines_end_to_end(self, tmp_path: Path) -> None:
        path = tmp_path / "diag.log"
        install_log_file(path)

        logging.getLogger("xnatctl.test.log_file").debug("hello %s", "world")

        (entry,) = read_lines(path)
        assert entry["msg"] == "hello world"
        assert entry["level"] == "DEBUG"

    def test_second_call_for_the_same_path_does_not_duplicate_the_handler(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "diag.log"
        install_log_file(path)
        install_log_file(path)
        install_log_file(str(path))  # same path, different spelling

        logging.getLogger("xnatctl.test.log_file").debug("once")

        assert len(read_lines(path)) == 1

    def test_relative_path_spelling_resolves_to_the_same_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotency has to survive a genuinely different STRING, not just
        ``Path`` vs. ``str`` of the identical absolute value -- a relative
        path only resolves to the same file once ``Path.resolve()`` runs,
        which is the exact mechanism this pins.
        """
        path = tmp_path / "diag.log"
        monkeypatch.chdir(tmp_path)
        install_log_file(path)  # absolute
        install_log_file("diag.log")  # relative, same cwd -- same file

        logging.getLogger("xnatctl.test.log_file").debug("once")

        assert len(read_lines(path)) == 1

    @pytest.mark.skipif(
        not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
    )
    def test_file_is_created_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "diag.log"
        install_log_file(path)
        logging.getLogger("xnatctl.test.log_file").debug("hello")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(
        not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
    )
    def test_preexisting_world_readable_file_is_tightened(self, tmp_path: Path) -> None:
        path = tmp_path / "diag.log"
        path.write_text("")
        os.chmod(path, 0o644)

        install_log_file(path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_rotation_triggers_at_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.core.logging as logging_module

        monkeypatch.setattr(logging_module, "LOG_FILE_MAX_BYTES", 200)
        path = tmp_path / "diag.log"
        install_log_file(path)

        logger = logging.getLogger("xnatctl.test.log_file")
        for i in range(50):
            logger.debug("padding line number %d to grow the file past the cap", i)

        assert LOG_FILE_BACKUP_COUNT == 1
        backup = path.with_name(path.name + ".1")
        assert backup.exists()

        if POSIX_PERMISSIONS:
            # The rotated-away file inherits whatever mode it already had
            # (0600, from _open()'s own opener) via the rename -- and the
            # freshly reopened base file goes through that same opener
            # again. Both must stay 0600, not just the one that happens to
            # get checked elsewhere.
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_root_level_drops_to_debug_while_stderr_handler_stays_at_its_tier(
        self, tmp_path: Path
    ) -> None:
        from xnatctl.core.logging import setup_logging

        setup_logging(quiet=True)  # stderr handler pinned to ERROR
        stderr_handlers_before = [
            h for h in logging.getLogger().handlers if not isinstance(h, logging.FileHandler)
        ]
        assert all(h.level == logging.ERROR for h in stderr_handlers_before)

        install_log_file(tmp_path / "diag.log")

        assert logging.getLogger().level == logging.DEBUG
        # The stderr handler's own level is untouched by activating the file.
        stderr_handlers_after = [
            h for h in logging.getLogger().handlers if not isinstance(h, logging.FileHandler)
        ]
        assert all(h.level == logging.ERROR for h in stderr_handlers_after)

        # A later setup_logging() call (e.g. the subcommand's @global_options,
        # running after root-level activation) must not undo the DEBUG drop.
        setup_logging(quiet=True)
        assert logging.getLogger().level == logging.DEBUG
        assert all(h.level == logging.ERROR for h in stderr_handlers_after)

    def test_handler_encoding_is_pinned_utf8(self, tmp_path: Path) -> None:
        install_log_file(tmp_path / "diag.log")
        (handler,) = [
            h for h in logging.getLogger().handlers if isinstance(h, _JsonLinesFileHandler)
        ]
        assert handler.encoding == "utf-8"


@pytest.mark.usefixtures("clean_root")
class TestFileScopeFilter:
    """Root has to sit at DEBUG for the file handler to see anything, which
    would otherwise hand every OTHER at-DEBUG logger -- not just xnatctl's
    own, PHI-vetted stream -- a free pass into a persisted file. Simulates a
    third-party dependency (pynetdicom) directly, since it is not a project
    dependency of the test env.
    """

    def test_third_party_logger_is_excluded_from_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "diag.log"
        install_log_file(path)
        try:
            logging.getLogger("pynetdicom.assoc").debug("ASSOCIATE request received from 1.2.3.4")
            logging.getLogger("xnatctl.test.log_file").debug("xnatctl line")
        finally:
            remove_log_file(path)

        entries = read_lines(path)
        assert len(entries) == 1
        assert entries[0]["logger"] == "xnatctl.test.log_file"

    def test_xnatctl_root_logger_itself_is_admitted(self, tmp_path: Path) -> None:
        """The bare ``"xnatctl"`` logger name (not just ``xnatctl.*``) counts too."""
        path = tmp_path / "diag.log"
        install_log_file(path)
        try:
            logging.getLogger("xnatctl").debug("bare xnatctl logger")
        finally:
            remove_log_file(path)

        entries = read_lines(path)
        assert len(entries) == 1
        assert entries[0]["logger"] == "xnatctl"

    def test_httpx_reaches_the_file_only_under_the_trace_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
        path = tmp_path / "diag.log"
        install_log_file(path)

        # This test targets _FileScopeFilter's OWN gating, isolated from
        # setup_logging()'s httpx/httpcore LOGGER-level tiering, which an
        # earlier test in the session may have left at WARNING (Logger
        # objects are process-wide singletons keyed by name, unaffected by
        # clean_root, which only resets root's handlers/level). Both loggers
        # are forced open here so a debug() call is guaranteed to reach the
        # filter under test either way.
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        saved_levels = (httpx_logger.level, httpcore_logger.level)
        httpx_logger.setLevel(logging.DEBUG)
        httpcore_logger.setLevel(logging.DEBUG)
        try:
            httpx_logger.debug("httpx debug line, no trace")
            assert read_lines(path) == []

            monkeypatch.setenv("XNATCTL_DEBUG", "1")
            httpcore_logger.debug("httpcore debug line, trace on")
        finally:
            httpx_logger.setLevel(saved_levels[0])
            httpcore_logger.setLevel(saved_levels[1])
            remove_log_file(path)

        entries = read_lines(path)
        assert any(e["logger"] == "httpcore" for e in entries)


@pytest.mark.usefixtures("clean_root")
class TestRemoveLogFile:
    def test_detaches_and_closes_the_handler(self, tmp_path: Path) -> None:
        path = tmp_path / "diag.log"
        install_log_file(path)
        assert any(isinstance(h, _JsonLinesFileHandler) for h in logging.getLogger().handlers)

        remove_log_file(path)

        assert not any(isinstance(h, _JsonLinesFileHandler) for h in logging.getLogger().handlers)
        logging.getLogger("xnatctl.test.log_file").debug("after removal")
        assert read_lines(path) == []

    def test_without_a_path_removes_every_diagnostics_handler(self, tmp_path: Path) -> None:
        install_log_file(tmp_path / "a.log")
        install_log_file(tmp_path / "b.log")

        remove_log_file()

        assert not any(isinstance(h, _JsonLinesFileHandler) for h in logging.getLogger().handlers)

    def test_a_specific_path_leaves_others_attached(self, tmp_path: Path) -> None:
        path_a = tmp_path / "a.log"
        path_b = tmp_path / "b.log"
        install_log_file(path_a)
        install_log_file(path_b)
        try:
            remove_log_file(path_a)

            handlers = [
                h for h in logging.getLogger().handlers if isinstance(h, _JsonLinesFileHandler)
            ]
            assert len(handlers) == 1
            assert Path(handlers[0].baseFilename) == path_b.resolve()
        finally:
            remove_log_file(path_b)


# =============================================================================
# Correlation id
# =============================================================================


class TestCorrelationId:
    def test_id_is_constant_across_records_until_regenerated(self) -> None:
        """Every record stamped between two ``new_correlation_id()`` calls
        carries the same id -- this is what "one id per invocation" means in
        practice, since a single invocation's log lines span many calls.
        """
        import io

        from xnatctl.core.logging import install_redaction_filter

        first = new_correlation_id()

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("xnatctl.test.corr")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        install_redaction_filter(logger)
        handler.setFormatter(logging.Formatter("%(corr)s"))
        try:
            logger.debug("one")
            logger.debug("two")
        finally:
            logger.handlers = []

        lines = stream.getvalue().splitlines()
        assert lines == [first, first]

    def test_regenerating_changes_the_id(self) -> None:
        first = new_correlation_id()
        second = new_correlation_id()
        assert first != second

    def test_visible_from_a_threadpoolexecutor_worker(self) -> None:
        """The whole reason _correlation_id is a plain module global and not
        a contextvars.ContextVar (see its docstring): most of the DEBUG
        stream a diagnostics file exists to capture -- HTTP calls from
        parallel uploads/downloads -- runs on ThreadPoolExecutor worker
        threads, which do NOT inherit a ContextVar from the thread that set
        it. This is the test that would have caught that mistake.
        """
        import io
        from concurrent.futures import ThreadPoolExecutor

        from xnatctl.core.logging import install_redaction_filter

        expected = new_correlation_id()

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("xnatctl.test.worker_corr")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        install_redaction_filter(logger)
        handler.setFormatter(logging.Formatter("%(corr)s"))

        def _log_from_worker() -> None:
            logger.debug("from a worker thread")

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(_log_from_worker).result()
        finally:
            logger.handlers = []

        assert stream.getvalue().strip() == expected


# =============================================================================
# Real HTTP trace -- the two enriched transport.py DEBUG call sites
# =============================================================================


@pytest.mark.usefixtures("clean_root")
class TestTransportHttpTraceReachesTheFile:
    """core/transport.py's per-attempt DEBUG line, end to end into the file."""

    def test_real_request_produces_a_typed_redacted_json_line(self, tmp_path: Path) -> None:
        from xnatctl.core.logging import setup_logging

        setup_logging()
        install_log_file(tmp_path / "diag.log")

        client = XNATClient(
            base_url="https://xnat.example.org",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True})),
        )
        client.get("/data/projects", params={"token": "s3cret", "format": "json"})

        entries = [e for e in read_lines(tmp_path / "diag.log") if e.get("event") == "http_request"]
        (entry,) = entries
        assert entry["method"] == "GET"
        assert entry["status"] == 200
        assert entry["attempt"] == 1
        assert isinstance(entry["duration_ms"], int)
        assert "s3cret" not in entry["msg"]
        assert "token=***" in entry["msg"]
        assert "format=json" in entry["msg"]  # non-secret params stay readable


# =============================================================================
# core.config.Config -- the additive `log_file` field
# =============================================================================


class TestConfigField:
    def test_defaults_to_none(self) -> None:
        assert Config().log_file is None

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        target = str(tmp_path / "diag.log")
        config = Config(
            profiles={"default": Profile(url="https://xnat.example.org", name="default")}
        )
        config.log_file = target
        config.save(path)

        assert Config.load(path).log_file == target

    def test_old_config_without_the_key_loads_with_none(self, tmp_path: Path) -> None:
        """Additive under version 1: no migration, no forced default path."""
        path = tmp_path / "config.yaml"
        path.write_text("version: 1\ndefault_profile: default\nprofiles: {}\n")
        assert Config.load(path).log_file is None


# =============================================================================
# CLI wiring: --log-file / XNATCTL_LOG_FILE / config `log_file:`
# =============================================================================
#
# Hidden probe commands registered on the real `cli` root group, following
# the pattern tests/test_global_options.py already established for exercising
# @global_options through the real inheritance/precedence machinery instead
# of a hand-rolled standalone command.

_PROBE_LOGGER = logging.getLogger("xnatctl.test.log_file_probe")


@cli.command("__probe_log_file__", hidden=True)
@global_options
@handle_errors
def _probe_log_file(ctx: Context) -> None:
    """Hidden probe: emits one DEBUG line carrying a secret-shaped URL."""
    del ctx
    _PROBE_LOGGER.debug("GET %s -> 200", SECRET_URL)
    click.echo("probe done")


@cli.command("__probe_log_file_fail__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_fail(ctx: Context) -> None:
    """Hidden probe: raises an unexpected exception WITHOUT logging it itself.

    Deliberately does not call ``logging`` at all -- the point is proving
    ``render_cli_error``/``handle_errors`` put the failure in the diagnostics
    file on their own, not that a command can choose to.
    """
    del ctx
    raise RuntimeError(f"boom while fetching {SECRET_URL}")


@cli.command("__probe_log_file_xnat_error__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_xnat_error(ctx: Context) -> None:
    """Hidden probe: raises a typed XNATCtlError, again without self-logging."""
    del ctx
    raise ServerUnreachableError(SECRET_URL)


@cli.command("__probe_log_file_system_exit__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_system_exit(ctx: Context) -> None:
    """Hidden probe: an early ``SystemExit``, mirroring ``whoami``'s
    "not authenticated" -> ``ExitCode.AUTH_ERROR`` path (never routed through
    ``except Exception``, since ``SystemExit`` is a ``BaseException``).
    """
    del ctx
    click.echo("Not authenticated.", err=True)
    raise SystemExit(ExitCode.AUTH_ERROR)


@cli.command("__probe_log_file_keyboard_interrupt__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_keyboard_interrupt(ctx: Context) -> None:
    """Hidden probe: simulates Ctrl+C mid-operation."""
    del ctx
    raise KeyboardInterrupt()


@cli.command("__probe_log_file_abort__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_abort(ctx: Context) -> None:
    """Hidden probe: simulates a declined destructive-op confirmation."""
    del ctx
    raise click.Abort()


@cli.command("__probe_log_file_undecorated__", hidden=True)
def _probe_log_file_undecorated() -> None:
    """Hidden probe carrying NEITHER @global_options NOR @handle_errors.

    Proves the root callback's own activation (cli/main.py) -- not
    @global_options -- is what makes --log-file/XNATCTL_LOG_FILE reach the
    16 real commands that carry no @global_options at all: config init/use-
    context/current-context/add-profile/remove-profile/set-password, every
    dicom subcommand, every completion subcommand, project transfer-init,
    local extract.
    """
    _PROBE_LOGGER.debug("GET %s -> 200", SECRET_URL)
    click.echo("probe done")


@cli.command("__probe_log_file_tiers__", hidden=True)
@global_options
@handle_errors
def _probe_log_file_tiers(ctx: Context) -> None:
    """Hidden probe: logs at DEBUG and WARNING, for stderr-parity checks.

    A DEBUG line only shows on stderr under -v/XNATCTL_DEBUG; a WARNING line
    shows at every tier except --quiet (which raises the stderr floor to
    ERROR). Between the two, every verbosity tier this feature must leave
    untouched has SOME stderr content to compare.
    """
    del ctx
    _PROBE_LOGGER.debug("debug tier line")
    _PROBE_LOGGER.warning("warning tier line")
    click.echo("probe done")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.usefixtures("clean_root")
class TestCliActivation:
    def test_subcommand_flag_activates_the_file(self, tmp_path: Path, runner: CliRunner) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path)])

        assert result.exit_code == 0, result.output
        lines = read_lines(path)
        assert any("token=***" in e["msg"] for e in lines)
        assert "s3cret" not in json.dumps(lines)

    def test_root_flag_activates_the_file(self, tmp_path: Path, runner: CliRunner) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["--log-file", str(path), "__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert read_lines(path)

    def test_env_var_activates_the_file(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "diag.log"
        monkeypatch.setenv("XNATCTL_LOG_FILE", str(path))
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert read_lines(path)

    def test_config_key_activates_the_file(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XNATCTL_LOG_FILE", raising=False)
        path = tmp_path / "diag.log"
        with config_seam(Config(log_file=str(path))):
            result = runner.invoke(cli, ["__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert read_lines(path)

    def test_flag_wins_over_env_and_config(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag is given at the SUBCOMMAND level here. Click parses a
        subcommand's own arguments only AFTER the root callback has already
        run to completion, so the root callback -- which has to resolve
        something for undecorated commands -- necessarily resolves and
        activates the env tier first, not yet knowing an override is coming;
        install_log_file() then replaces it with the subcommand's flag before
        anything is logged. The content precedence is exactly right (nothing
        ever reaches env_path); an empty env_path may transiently exist as
        that resolution's side effect, which is why this asserts on content
        (read_lines), not existence. test_root_flag_beats_env_for_a_decorated_
        command below covers the stronger, side-effect-free case: a root-level
        flag never even causes the env path to be looked at.
        """
        flag_path = tmp_path / "flag.log"
        env_path = tmp_path / "env.log"
        config_path = tmp_path / "config.log"
        monkeypatch.setenv("XNATCTL_LOG_FILE", str(env_path))
        with config_seam(Config(log_file=str(config_path))):
            result = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(flag_path)])

        assert result.exit_code == 0, result.output
        assert read_lines(flag_path)
        assert read_lines(env_path) == []
        assert not config_path.exists()

    def test_root_flag_beats_env_for_a_decorated_command(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The precedence-inversion regression: a ROOT-level --log-file must
        win over XNATCTL_LOG_FILE outright -- Click itself already prefers a
        COMMANDLINE-sourced option value over its envvar for that SAME
        option, so the root callback never even resolves env_path here (this
        is the side-effect-free case; contrast with test_flag_wins_over_env_
        and_config above, where the flag is given at the subcommand level
        instead).
        """
        flag_path = tmp_path / "flag.log"
        env_path = tmp_path / "env.log"
        monkeypatch.setenv("XNATCTL_LOG_FILE", str(env_path))
        with config_seam(Config()):
            result = runner.invoke(cli, ["--log-file", str(flag_path), "__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert read_lines(flag_path)
        assert not env_path.exists()

    def test_env_wins_over_config(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_path = tmp_path / "env.log"
        config_path = tmp_path / "config.log"
        monkeypatch.setenv("XNATCTL_LOG_FILE", str(env_path))
        with config_seam(Config(log_file=str(config_path))):
            result = runner.invoke(cli, ["__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert read_lines(env_path)
        assert not config_path.exists()

    def test_no_source_means_no_file(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XNATCTL_LOG_FILE", raising=False)
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file__"])

        assert result.exit_code == 0, result.output
        assert not path.exists()


@pytest.mark.usefixtures("clean_root")
class TestCliBehavior:
    def test_quiet_mode_keeps_stderr_quiet_while_file_gets_debug(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["--quiet", "__probe_log_file__", "--log-file", str(path)])

        assert result.exit_code == 0, result.output
        # CliRunner mixes stdout/stderr by default; the probe's DEBUG line's
        # request line must not have reached stderr under --quiet.
        assert "GET " not in result.output
        assert "probe done" in result.output

        lines = read_lines(path)
        assert any(e["level"] == "DEBUG" and "GET " in e["msg"] for e in lines)

    def test_appends_across_invocations_with_distinct_correlation_ids(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            first = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path)])
            second = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path)])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output

        lines = read_lines(path)
        assert len(lines) == 2
        assert lines[0]["corr"] != lines[1]["corr"]

    def test_failing_command_still_writes_the_artifact(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """The probe raises without logging anything itself -- the whole
        entry below has to come from handle_errors/render_cli_error's own
        automatic logging, or this fails. Also covers "an early raise
        produces a non-empty file": the probe's only statement is the raise.
        """
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file_fail__", "--log-file", str(path)])

        assert result.exit_code != 0
        lines = read_lines(path)
        error_entries = [e for e in lines if e["level"] == "ERROR"]
        assert error_entries
        entry = error_entries[0]
        assert entry["event"] == "command_failed"
        assert "exc" in entry
        assert "s3cret" not in json.dumps(entry)
        assert "token=***" in entry["exc"]
        # Pinned regardless of which submodule under cli/common/ the record
        # was logged from -- see xnatctl/cli/common/errors.py's _LOGGER_NAME.
        assert entry["logger"] == "xnatctl.cli.common"

    def test_xnatctl_error_is_captured_without_any_self_logging(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file_xnat_error__", "--log-file", str(path)])

        assert result.exit_code != 0
        lines = read_lines(path)
        error_entries = [
            e for e in lines if e["level"] == "ERROR" and e.get("event") == "command_failed"
        ]
        assert error_entries
        assert "s3cret" not in json.dumps(error_entries[0])

    def test_system_exit_with_a_nonzero_code_is_captured(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """Mirrors whoami's early ``raise SystemExit(ExitCode.AUTH_ERROR)`` --
        a path that never reaches ``except Exception``/``render_cli_error`` at
        all, so it needs its own coverage.
        """
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file_system_exit__", "--log-file", str(path)])

        assert result.exit_code == ExitCode.AUTH_ERROR
        lines = read_lines(path)
        exit_entries = [e for e in lines if e.get("event") == "exit"]
        assert exit_entries
        assert exit_entries[0]["level"] == "ERROR"
        assert str(int(ExitCode.AUTH_ERROR)) in exit_entries[0]["msg"]

    def test_keyboard_interrupt_is_captured(self, tmp_path: Path, runner: CliRunner) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(
                cli, ["__probe_log_file_keyboard_interrupt__", "--log-file", str(path)]
            )

        assert result.exit_code == ExitCode.USER_CANCELLED
        lines = read_lines(path)
        cancelled = [e for e in lines if e.get("event") == "cancelled"]
        assert cancelled
        assert cancelled[0]["level"] == "INFO"

    def test_abort_is_captured(self, tmp_path: Path, runner: CliRunner) -> None:
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["__probe_log_file_abort__", "--log-file", str(path)])

        assert result.exit_code == ExitCode.USER_CANCELLED
        lines = read_lines(path)
        cancelled = [e for e in lines if e.get("event") == "cancelled"]
        assert cancelled
        assert cancelled[0]["level"] == "INFO"

    def test_verbose_mode_does_not_duplicate_the_traceback_on_stderr(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """The file-only command-outcome record must never ALSO print to
        stderr, even under -v where its DEBUG/ERROR level would otherwise
        clear the stderr handler's threshold -- render_cli_error's own
        click.echo(traceback...) is already the one and only stderr copy.
        """
        path = tmp_path / "diag.log"
        with config_seam(Config()):
            result = runner.invoke(cli, ["-v", "__probe_log_file_fail__", "--log-file", str(path)])

        assert result.exit_code != 0
        assert result.output.count("Traceback (most recent call last):") == 1
        assert read_lines(path)  # the file still got its own copy


# =============================================================================
# Commands with no @global_options at all (config init/use-context/...,
# every dicom subcommand, every completion subcommand, project
# transfer-init, local extract)
# =============================================================================


@pytest.mark.usefixtures("clean_root")
class TestUndecoratedCommands:
    def test_undecorated_command_still_gets_full_capture(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        path = tmp_path / "diag.log"
        result = runner.invoke(cli, ["--log-file", str(path), "__probe_log_file_undecorated__"])

        assert result.exit_code == 0, result.output
        assert "probe done" in result.output
        lines = read_lines(path)
        assert any("token=***" in e["msg"] for e in lines)
        assert "s3cret" not in json.dumps(lines)

    def test_a_real_undecorated_command_activates_the_file(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """`completion bash` carries no @global_options/@handle_errors at
        all -- this is the literal repro from the review finding
        ("xnatctl --log-file diag.log completion bash succeeds and creates
        nothing"), on a real command rather than a probe.
        """
        path = tmp_path / "diag.log"
        result = runner.invoke(cli, ["--log-file", str(path), "completion", "bash"])

        assert result.exit_code == 0, result.output
        assert path.exists()


# =============================================================================
# Handler lifecycle across more than one invocation in the same process
# (library callers driving the CLI programmatically, or repeated
# CliRunner.invoke() -- production is one process per invocation, so this
# only matters there, but "only matters there" still means it matters)
# =============================================================================


@pytest.mark.usefixtures("clean_root")
class TestHandlerLifecycleAcrossInvocations:
    def test_a_then_b_only_b_is_written_to(self, tmp_path: Path, runner: CliRunner) -> None:
        path_a = tmp_path / "a.log"
        path_b = tmp_path / "b.log"
        with config_seam(Config()):
            first = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path_a)])
            second = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path_b)])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert len(read_lines(path_a)) == 1
        assert len(read_lines(path_b)) == 1

    def test_a_then_none_leaves_a_untouched_and_writes_nowhere(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XNATCTL_LOG_FILE", raising=False)
        path_a = tmp_path / "a.log"
        with config_seam(Config()):
            first = runner.invoke(cli, ["__probe_log_file__", "--log-file", str(path_a)])
            second = runner.invoke(cli, ["__probe_log_file__"])  # no source at all

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        # Invocation 2's own DEBUG line went nowhere -- a.log holds only
        # invocation 1's single line, not two.
        assert len(read_lines(path_a)) == 1


# =============================================================================
# Stderr parity: --log-file must never change a single byte of what stderr
# already shows, at any verbosity tier -- it only adds a second, independent
# destination.
# =============================================================================


@pytest.mark.usefixtures("clean_root")
class TestStderrParity:
    @pytest.mark.parametrize(
        "extra_args",
        [[], ["-v"], ["--quiet"]],
        ids=["default", "verbose", "quiet"],
    )
    def test_stderr_is_unchanged_with_or_without_log_file(
        self,
        tmp_path: Path,
        runner: CliRunner,
        extra_args: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("XNATCTL_DEBUG", raising=False)
        with config_seam(Config()):
            without = runner.invoke(cli, [*extra_args, "__probe_log_file_tiers__"])
            with_file = runner.invoke(
                cli,
                [
                    *extra_args,
                    "__probe_log_file_tiers__",
                    "--log-file",
                    str(tmp_path / "diag.log"),
                ],
            )

        assert without.exit_code == 0, without.output
        assert with_file.exit_code == 0, with_file.output
        assert _strip_timestamps(without.output) == _strip_timestamps(with_file.output)

    def test_stderr_is_unchanged_under_xnatctl_debug(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XNATCTL_DEBUG", "1")
        with config_seam(Config()):
            without = runner.invoke(cli, ["__probe_log_file_tiers__"])
            with_file = runner.invoke(
                cli, ["__probe_log_file_tiers__", "--log-file", str(tmp_path / "diag.log")]
            )

        assert without.exit_code == 0, without.output
        assert with_file.exit_code == 0, with_file.output
        assert _strip_timestamps(without.output) == _strip_timestamps(with_file.output)
