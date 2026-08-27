"""Tests for the update-availability notice.

``xnatctl.core.update_check`` is designed so a normal command can never wait
on the network: :func:`check_for_update` only reads a local cache file, and
:func:`refresh_cache_async` launches a detached child process (rather than a
thread -- a thread is killed before a DNS lookup or TLS handshake can finish
when the parent process exits quickly) to do the GET and rewrite the cache.
No test here ever actually spawns that child or touches the real network:
``_spawn_refresh_subprocess`` is tested by mocking ``subprocess.Popen``, the
TTL-gating in ``refresh_cache_async`` is tested with an injected ``launch``
that never spawns anything, and the child's own logic (``main()``) is
exercised directly with an injected ``fetch``.

There is no autouse fixture in ``conftest.py`` isolating this particular
cache file the way the session cache / audit log are isolated, so
:data:`isolate_cache` below (autouse, but scoped to just this module) is what
keeps these tests off the developer's real ``~/.config/xnatctl/.update-check``.
For the same reason, ``update_check.update_check_disabled()`` treats "running
under pytest" as an implicit opt-out (see its docstring) -- tests that
exercise the notify path past that guard monkeypatch it back off.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import AuthenticatedCLI, config_seam

from xnatctl.cli import main as cli_main
from xnatctl.cli.main import cli
from xnatctl.core import update_check
from xnatctl.core.config import Config, Profile


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's cache file at a throwaway path for every test here."""
    path = tmp_path / ".update-check"
    monkeypatch.setattr(update_check, "UPDATE_CACHE_FILE", path)
    return path


def _seed_cache(path: Path, *, latest: str, last_check: float) -> None:
    path.write_text(json.dumps({"last_check": last_check, "latest": latest}))


def _inline_launch(fetch: Callable[[], str | None]) -> Callable[[], None]:
    """A ``launch`` replacement that runs the refresh synchronously in-process."""

    def launch() -> None:
        update_check._refresh(fetch=fetch)

    return launch


# =============================================================================
# check_for_update: version comparison + normalization, never touches network
# =============================================================================


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("1.2.3", "1.2.0", "1.2.3"),  # ahead
        ("1.2.0", "1.2.0", None),  # equal
        ("1.1.0", "1.2.0", None),  # behind
        ("2.0.0.dev0", "2.0.0", None),  # a dev release of the same base
        ("0.5.0rc1", "0.4.0", None),  # a pre-release with a HIGHER base
    ],
)
def test_check_for_update_compares_versions(
    isolate_cache: Path, latest: str, current: str, expected: str | None
) -> None:
    _seed_cache(isolate_cache, latest=latest, last_check=time.time())
    assert update_check.check_for_update(current) == expected


def test_check_for_update_normalizes_the_cached_version_string(isolate_cache: Path) -> None:
    """A cached value with a stray newline must not leak into the notice."""
    _seed_cache(isolate_cache, latest="99.0\n", last_check=time.time())
    result = update_check.check_for_update("1.0.0")
    assert result == "99.0"
    assert "\n" not in result


def test_check_for_update_missing_cache_returns_none(isolate_cache: Path) -> None:
    assert not isolate_cache.exists()
    assert update_check.check_for_update("0.1.0") is None


def test_check_for_update_malformed_json_returns_none(isolate_cache: Path) -> None:
    isolate_cache.write_text("{not valid json")
    assert update_check.check_for_update("0.1.0") is None


def test_check_for_update_non_string_latest_returns_none(isolate_cache: Path) -> None:
    isolate_cache.write_text(json.dumps({"last_check": time.time(), "latest": 123}))
    assert update_check.check_for_update("0.1.0") is None


def test_check_for_update_invalid_version_string_returns_none(isolate_cache: Path) -> None:
    _seed_cache(isolate_cache, latest="not-a-version", last_check=time.time())
    assert update_check.check_for_update("0.1.0") is None


# =============================================================================
# _write_cache: normalizes on write too
# =============================================================================


@pytest.mark.parametrize("raw", ["1.2.3 ", "1.2.3\n"])
def test_write_cache_normalizes_the_stored_version(isolate_cache: Path, raw: str) -> None:
    update_check._write_cache(raw)
    data = json.loads(isolate_cache.read_text())
    assert data["latest"] == "1.2.3"


def test_write_cache_unparsable_version_is_not_cached(isolate_cache: Path) -> None:
    update_check._write_cache("not-a-version")
    assert not isolate_cache.exists()


# =============================================================================
# _spawn_refresh_subprocess: argv/kwargs, never actually spawns in tests
# =============================================================================


def test_spawn_refresh_subprocess_detaches_and_closes_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr(update_check.subprocess, "Popen", _FakePopen)

    result = update_check._spawn_refresh_subprocess()

    assert result is not None
    assert len(calls) == 1
    (argv,), kwargs = calls[0]
    assert argv == [update_check.sys.executable, "-m", "xnatctl.core.update_check"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True


def test_spawn_refresh_subprocess_failure_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_popen(*args: object, **kwargs: object) -> None:
        raise OSError("simulated: fork/exec failed")

    monkeypatch.setattr(update_check.subprocess, "Popen", raising_popen)

    assert update_check._spawn_refresh_subprocess() is None


def test_spawn_refresh_subprocess_frozen_reinvokes_self_with_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a frozen (PyInstaller) build, ``sys.executable`` IS the xnatctl
    binary, not a python interpreter -- ``-m xnatctl.core.update_check``
    means nothing to it (Click rejects the unrecognized "-m" argument, exit
    2, into the closed stdio the child never surfaces). Before the fix, the
    child was spawned identically to the non-frozen case regardless of
    ``sys.frozen``, so this asserts the frozen branch drops ``-m`` and signals
    itself via the environment instead.
    """
    monkeypatch.setattr(update_check.sys, "frozen", True, raising=False)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr(update_check.subprocess, "Popen", _FakePopen)

    result = update_check._spawn_refresh_subprocess()

    assert result is not None
    assert len(calls) == 1
    (argv,), kwargs = calls[0]
    assert argv == [update_check.sys.executable]
    assert "-m" not in argv
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env[update_check.FROZEN_REFRESH_ENV_VAR] == "1"
    # One-file build (see xnatctl.spec): without this the child inherits the
    # parent's unpack directory and loses it when the parent exits moments
    # later, which would defeat the refresh a second way.
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


# =============================================================================
# refresh_cache_async: TTL gating (never spawns in these tests)
# =============================================================================


def test_refresh_cache_async_fresh_cache_skips_launch(isolate_cache: Path) -> None:
    """A cache within its 24h TTL is never refreshed -- launch is never called."""
    _seed_cache(isolate_cache, latest="1.0.0", last_check=time.time())
    calls = []

    result = update_check.refresh_cache_async(launch=lambda: calls.append(1) or None)

    assert result is None
    assert calls == []


def test_refresh_cache_async_stale_cache_calls_launch(isolate_cache: Path) -> None:
    stale = time.time() - update_check.CACHE_TTL_SECONDS - 1
    _seed_cache(isolate_cache, latest="1.0.0", last_check=stale)
    calls = []

    update_check.refresh_cache_async(launch=lambda: calls.append(1) or None)

    assert calls == [1]


def test_refresh_cache_async_missing_cache_calls_launch(isolate_cache: Path) -> None:
    """No cache at all is treated the same as a stale one."""
    assert not isolate_cache.exists()
    calls = []

    update_check.refresh_cache_async(launch=lambda: calls.append(1) or None)

    assert calls == [1]


def test_refresh_cache_async_future_last_check_is_treated_as_stale(isolate_cache: Path) -> None:
    """A corrupt/clock-skewed future timestamp must not suppress refresh forever."""
    _seed_cache(isolate_cache, latest="1.0.0", last_check=time.time() + 1e20)
    calls = []

    update_check.refresh_cache_async(launch=lambda: calls.append(1) or None)

    assert calls == [1]


def test_refresh_cache_async_returns_launch_result(isolate_cache: Path) -> None:
    sentinel = object()
    result = update_check.refresh_cache_async(launch=lambda: sentinel)
    assert result is sentinel


def test_refresh_cache_async_inline_launch_writes_cache(isolate_cache: Path) -> None:
    """The documented inline-launch pattern tests use in place of a real spawn."""
    stale = time.time() - update_check.CACHE_TTL_SECONDS - 1
    _seed_cache(isolate_cache, latest="1.0.0", last_check=stale)

    update_check.refresh_cache_async(launch=_inline_launch(lambda: "2.0.0"))

    data = json.loads(isolate_cache.read_text())
    assert data["latest"] == "2.0.0"


# =============================================================================
# _refresh: broad exception boundary around the injectable `fetch`
# =============================================================================


def test_refresh_writes_cache_on_success(isolate_cache: Path) -> None:
    update_check._refresh(fetch=lambda: "5.0.0")
    data = json.loads(isolate_cache.read_text())
    assert data["latest"] == "5.0.0"


def test_refresh_fetch_returning_none_leaves_cache_untouched(isolate_cache: Path) -> None:
    update_check._refresh(fetch=lambda: None)
    assert not isolate_cache.exists()


def test_refresh_fetch_raising_is_silent(isolate_cache: Path) -> None:
    """A misbehaving fetch (e.g. http.client.HTTPException from a bad proxy)
    must not propagate -- this is the gap a returning-None fetch doesn't cover.
    """

    def boom() -> str | None:
        raise RuntimeError("simulated: fetch blew up instead of returning None")

    update_check._refresh(fetch=boom)  # must not raise

    assert not isolate_cache.exists()


def test_refresh_write_failure_is_silent(
    isolate_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_atomic_write(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated read-only cache dir")

    monkeypatch.setattr(update_check, "atomic_private_write", raising_atomic_write)

    update_check._refresh(fetch=lambda: "9.9.9")  # must not raise
    assert not isolate_cache.exists()


# =============================================================================
# _fetch_latest_version: silent on network error / malformed JSON / bad shape
# =============================================================================


def test_fetch_latest_version_network_error_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def raising_urlopen(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", raising_urlopen)

    assert update_check._fetch_latest_version() is None


def test_fetch_latest_version_http_exception_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """http.client.HTTPException is not an OSError -- confirm it's still caught."""
    import http.client

    def raising_urlopen(*args: object, **kwargs: object) -> None:
        raise http.client.BadStatusLine("simulated malformed status line")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", raising_urlopen)

    assert update_check._fetch_latest_version() is None


def test_fetch_latest_version_malformed_json_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

    assert update_check._fetch_latest_version() is None


def test_fetch_latest_version_missing_field_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response that parses but doesn't have the expected shape."""
    import io

    class _FakeResponse:
        def __enter__(self) -> io.BytesIO:
            return io.BytesIO(json.dumps({"unexpected": "shape"}).encode())

        def __exit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(update_check.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

    assert update_check._fetch_latest_version() is None


# =============================================================================
# _cleanup_stale_tmp_files: opportunistic hygiene for killed-mid-write litter
# =============================================================================


def test_cleanup_stale_tmp_files_removes_orphans(isolate_cache: Path) -> None:
    orphan = isolate_cache.parent / f".{isolate_cache.name}.12345.tmp"
    orphan.write_text("leftover")
    unrelated = isolate_cache.parent / "unrelated.tmp"
    unrelated.write_text("keep me")

    update_check._cleanup_stale_tmp_files()

    assert not orphan.exists()
    assert unrelated.exists()


def test_cleanup_stale_tmp_files_missing_dir_is_silent(
    isolate_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        update_check, "UPDATE_CACHE_FILE", isolate_cache.parent / "nope" / ".update-check"
    )
    update_check._cleanup_stale_tmp_files()  # must not raise


def test_cleanup_stale_tmp_files_unlink_failure_is_silent(
    isolate_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = isolate_cache.parent / f".{isolate_cache.name}.12345.tmp"
    orphan.write_text("leftover")
    real_unlink = Path.unlink

    def raising_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == orphan.name:
            raise PermissionError("simulated")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", raising_unlink)

    update_check._cleanup_stale_tmp_files()  # must not raise


# =============================================================================
# main(): the refresh subprocess's entry point, exercised without spawning
# =============================================================================


def test_main_cleans_orphans_and_writes_cache(
    isolate_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = isolate_cache.parent / f".{isolate_cache.name}.999.tmp"
    orphan.write_text("leftover")
    monkeypatch.setattr(update_check, "_fetch_latest_version", lambda: "7.0.0")

    update_check.main()

    assert not orphan.exists()
    data = json.loads(isolate_cache.read_text())
    assert data["latest"] == "7.0.0"


def test_main_never_raises_when_fetch_blows_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_fetch() -> str | None:
        raise RuntimeError("simulated total failure")

    monkeypatch.setattr(update_check, "_fetch_latest_version", raising_fetch)

    update_check.main()  # must not raise


def test_main_never_raises_when_cleanup_blows_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_cleanup() -> None:
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(update_check, "_cleanup_stale_tmp_files", raising_cleanup)
    monkeypatch.setattr(update_check, "_fetch_latest_version", lambda: "1.0.0")

    update_check.main()  # must not raise


# =============================================================================
# update_check_disabled: env opt-outs + the implicit pytest opt-out
# =============================================================================


def test_update_check_disabled_under_pytest_by_default() -> None:
    """No conftest fixture isolates this cache, so pytest itself opts out."""
    assert update_check.update_check_disabled() is True


@pytest.mark.parametrize("var", ["NO_UPDATE_NOTIFIER", "XNAT_NO_UPDATE_CHECK", "CI"])
def test_update_check_disabled_env_vars(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setattr(update_check, "_running_under_pytest", lambda: False)
    assert update_check.update_check_disabled(env={var: "1"}) is True


def test_update_check_not_disabled_with_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "_running_under_pytest", lambda: False)
    assert update_check.update_check_disabled(env={}) is False


# =============================================================================
# CLI wiring: notice conditions, stdout cleanliness, never breaks the command
# =============================================================================


@pytest.fixture
def notify_path_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``update_check_disabled()`` return False no matter the ambient shell.

    A dev shell or a real CI runner may already have ``NO_UPDATE_NOTIFIER``,
    ``XNAT_NO_UPDATE_CHECK``, or (especially) ``CI`` set -- clearing them here
    is what makes the notify-path tests deterministic regardless of where
    they run, rather than only "usually pass."
    """
    monkeypatch.setattr(update_check, "_running_under_pytest", lambda: False)
    for var in ("NO_UPDATE_NOTIFIER", "XNAT_NO_UPDATE_CHECK", "CI"):
        monkeypatch.delenv(var, raising=False)


def _minimal_config() -> Config:
    """A loaded, valid config -- enough to make a @global_options command's
    ``ctx.obj.config`` non-None, which is now the whole gate for the notice
    and background refresh (see ``_maybe_notify_update``'s docstring).
    """
    return Config(
        default_profile="default",
        profiles={"default": Profile(url="https://xnat.example.org", name="default")},
    )


@pytest.fixture
def force_notify_path(
    notify_path_reachable: None, monkeypatch: pytest.MonkeyPatch, isolate_cache: Path
) -> None:
    """Make the notify path reachable and report a newer version available.

    ``refresh_cache_async`` is stubbed to a no-op here too -- these tests
    are about the notice, not the refresh, and a stub keeps them from ever
    touching ``subprocess.Popen``.
    """
    monkeypatch.setattr(update_check, "refresh_cache_async", lambda **kwargs: None)
    _seed_cache(isolate_cache, latest="99.0.0", last_check=time.time())


def test_notice_prints_on_tty_success(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    runner = CliRunner()

    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["config", "show"])

    assert result.exit_code == 0
    assert "A new xnatctl release is available:" in result.stderr
    assert "-> 99.0.0" in result.stderr


def test_notice_suppressed_on_non_tty(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: False)
    runner = CliRunner()

    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["config", "show"])

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr


def test_notice_suppressed_in_quiet_mode(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    runner = CliRunner()

    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["-q", "config", "show"])

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr


def test_notice_suppressed_when_opted_out(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    runner = CliRunner()

    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["config", "show"], env={"NO_UPDATE_NOTIFIER": "1"})

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr


def test_undecorated_command_never_notifies_or_refreshes(
    notify_path_reachable: None, monkeypatch: pytest.MonkeyPatch, isolate_cache: Path
) -> None:
    """Ratified decision: the whole notice+refresh path is gated on a loaded
    config being present on ``ctx.obj`` -- i.e. the command carries
    ``@global_options``. ``completion bash`` carries neither, so even a real
    tty and a newer-version, STALE cache (which would normally also trigger
    the background refresh for a decorated command) must produce neither a
    notice nor a spawned refresh: the command touches no server and loads no
    config, so there is nowhere it could even have read an opt-out from.
    """
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)
    stale = time.time() - update_check.CACHE_TTL_SECONDS - 1
    _seed_cache(isolate_cache, latest="99.0.0", last_check=stale)
    refresh_calls: list[int] = []
    monkeypatch.setattr(
        update_check, "refresh_cache_async", lambda **kwargs: refresh_calls.append(1)
    )
    runner = CliRunner()

    result = runner.invoke(cli, ["completion", "bash"])

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr
    assert refresh_calls == []


def test_notice_suppressed_and_stdout_stays_pure_json(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch, authenticated_cli: AuthenticatedCLI
) -> None:
    """The real ``-o json`` case: a subcommand that actually emits JSON."""
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)

    result = authenticated_cli.invoke(["whoami", "-o", "json"])

    assert result.exit_code == 0
    assert "xnatctl release is available" not in result.stderr
    parsed = json.loads(result.stdout)  # raises if the notice leaked into stdout
    assert parsed["username"]


def test_update_check_failure_never_breaks_the_command(
    notify_path_reachable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken update-check path must not change a command's exit code or output."""
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: True)

    def raising_check_for_update(current: str) -> str | None:
        raise RuntimeError("simulated failure in the update check")

    monkeypatch.setattr(update_check, "check_for_update", raising_check_for_update)

    runner = CliRunner()
    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["config", "show"])

    assert result.exit_code == 0
    assert "Configuration" in result.stdout  # the real command output still rendered
    assert "Traceback" not in result.stderr
    assert "RuntimeError" not in result.stderr


def test_refresh_cache_async_always_kicked_even_without_notice(
    force_notify_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background refresh fires even when the notice itself is suppressed,
    given a decorated command (one that loaded a config -- see
    ``test_undecorated_command_never_notifies_or_refreshes`` for the
    undecorated case, where neither fires).
    """
    calls = []
    monkeypatch.setattr(update_check, "refresh_cache_async", lambda **kwargs: calls.append(1))
    monkeypatch.setattr(cli_main, "_stderr_is_tty", lambda: False)  # notice suppressed
    runner = CliRunner()

    with config_seam(_minimal_config()):
        result = runner.invoke(cli, ["config", "show"])

    assert result.exit_code == 0
    assert calls == [1]


# =============================================================================
# cli.main.main(): the frozen refresh child's entry point, before Click
# =============================================================================


def test_frozen_refresh_signal_runs_inline_and_skips_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-signalled refresh child must never touch Click at all.

    Regression test: before the fix, a frozen binary's detached refresh
    child was launched as ``[binary, "-m", "xnatctl.core.update_check"]`` --
    meaningless to a compiled executable, since ``sys.executable`` there IS
    the binary rather than an interpreter. Click parsed "-m" as an unknown
    command and exited 2, so the cache never refreshed on frozen installs.
    ``cli.main.main()`` now checks ``update_check.FROZEN_REFRESH_ENV_VAR``
    before calling ``cli()`` at all and runs the refresh directly instead.
    """
    monkeypatch.setenv(update_check.FROZEN_REFRESH_ENV_VAR, "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["xnatctl"])
    calls: list[str] = []
    monkeypatch.setattr(update_check, "main", lambda: calls.append("refreshed"))
    monkeypatch.setattr(cli_main, "cli", lambda *a, **k: calls.append("cli-invoked"))

    cli_main.main()

    assert calls == ["refreshed"]


@pytest.mark.parametrize(
    ("frozen", "argv"),
    [
        (False, ["xnatctl"]),
        (True, ["xnatctl", "project", "list"]),
    ],
    ids=["signal-leaked-into-a-normal-install", "signal-leaked-into-a-real-command"],
)
def test_frozen_signal_alone_does_not_swallow_a_real_command(
    monkeypatch: pytest.MonkeyPatch, frozen: bool, argv: list[str]
) -> None:
    """The private signal must not hijack an ordinary invocation.

    Nothing should ever set this variable, but environments leak -- and on
    the signal alone, `xnatctl project list` would silently perform an
    update fetch, ignore its arguments, and exit 0. The branch therefore
    also requires the frozen build the signal exists for and the bare argv
    its spawner actually passes.
    """
    monkeypatch.setenv(update_check.FROZEN_REFRESH_ENV_VAR, "1")
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", argv)
    calls: list[str] = []
    monkeypatch.setattr(update_check, "main", lambda: calls.append("refreshed"))
    monkeypatch.setattr(cli_main, "cli", lambda *a, **k: calls.append("cli-invoked"))

    cli_main.main()

    assert calls == ["cli-invoked"]


def test_unsignalled_invocation_runs_cli_as_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against over-correcting: a normal invocation (the signal unset)
    must still reach ``cli()`` exactly as before.
    """
    monkeypatch.delenv(update_check.FROZEN_REFRESH_ENV_VAR, raising=False)
    calls: list[str] = []
    monkeypatch.setattr(update_check, "main", lambda: calls.append("refreshed"))
    monkeypatch.setattr(cli_main, "cli", lambda *a, **k: calls.append("cli-invoked"))

    cli_main.main()

    assert calls == ["cli-invoked"]
