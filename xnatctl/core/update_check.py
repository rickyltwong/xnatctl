"""Update-availability check: a one-line stderr notice, like ``gh``/``npm``.

Design constraint: nothing here may ever add wait time to a normal command.
:func:`check_for_update` only reads a local cache file -- it never touches the
network. Keeping that cache warm is a separate concern, handled by
:func:`refresh_cache_async`, which launches a fully detached child process
(``python -m xnatctl.core.update_check``) to do the GET and rewrite the
cache.

That child is a *process*, not a thread. A background thread is killed the
instant the interpreter starts tearing down -- for a short-lived command
(``xnatctl whoami``, say) the process can exit before a DNS lookup or TLS
handshake on the refresh thread has even completed, so a missing cache could
never warm up no matter how many times the command ran. ``subprocess.Popen``
with ``start_new_session=True`` and closed stdio detaches the child from this
process's session entirely: it keeps running (and finishes the write) even
after the parent has already exited, and ``Popen()`` itself still returns
immediately -- it does not wait for the child, so this remains latency-free.

A config-file opt-out (``update_check: false`` in config.yaml, alongside the
env vars below) is available too -- see :func:`update_check_disabled`'s
``config_update_check`` parameter. The CLI wiring lives in ``cli/main.py``,
which is the one call site that already has a loaded ``Config`` to read --
and, deliberately, is the ONLY thing this whole feature (notice and
background refresh alike) is gated on being present at all: a command that
never loads a config (shell completion, ``local extract``, ``config init``,
every ``dicom`` subcommand, ...) neither notifies nor spawns the refresh
child, since it touches no server, reads no config, and therefore has no
way to have read an opt-out from in the first place -- a detached PyPI fetch
firing behind a plain `xnatctl completion bash` would be a surprising side
effect for a command that does nothing else network-shaped.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from packaging.version import InvalidVersion, Version

from xnatctl.core.config import CONFIG_DIR
from xnatctl.core.fsutil import atomic_private_write, ensure_private_dir

logger = logging.getLogger(__name__)

#: Cache file: ``{"last_check": <epoch seconds>, "latest": "<version>"}``.
#: Lives next to config.yaml/.session, written 0600 via core.fsutil like
#: those other secrets-adjacent files (this one isn't a secret, but reusing
#: the same helper keeps one code path for "files under ~/.config/xnatctl").
UPDATE_CACHE_FILE = CONFIG_DIR / ".update-check"

CACHE_TTL_SECONDS = 24 * 60 * 60

PYPI_PROJECT_URL = "https://pypi.org/pypi/xnatctl/json"
FETCH_TIMEOUT_SECONDS = 1.0

#: Any non-empty value on any of these disables both the notice and the
#: background cache refresh. Mirrors the convention of NO_UPDATE_NOTIFIER
#: (used by several JS CLIs) plus xnatctl's own env-var naming, and treats a
#: CI environment as opted out by default -- a CI runner has no one to show
#: the notice to and no reason to phone home on every job.
_OPT_OUT_ENV_VARS = ("NO_UPDATE_NOTIFIER", "XNAT_NO_UPDATE_CHECK", "CI")

#: Private signal set on the detached refresh child's environment when the
#: parent is a frozen (PyInstaller) binary. ``sys.executable`` in that case
#: IS the xnatctl binary itself, not a python interpreter, so re-invoking it
#: with ``-m xnatctl.core.update_check`` (the non-frozen form, below) is
#: meaningless -- Click parses "-m" as an unknown command and exits 2 into
#: the closed stdio, so the cache never refreshes and every command spawns
#: a doomed child. ``cli.main.main()`` checks this var before touching Click
#: at all and runs :func:`main` in-process instead -- not documented as
#: config, since a user is never meant to set it themselves.
FROZEN_REFRESH_ENV_VAR = "_XNATCTL_FROZEN_UPDATE_CHECK_REFRESH"


def _running_under_pytest() -> bool:
    """True when the current process is a pytest run.

    There is no autouse fixture isolating this cache file the way
    ``isolate_session_cache``/``isolate_audit_log`` isolate theirs (this
    module doesn't own conftest.py), so without this guard every CliRunner
    invocation across the whole test suite would read/write the real
    ``~/.config/xnatctl/.update-check`` and, once a cache is missing or
    stale, launch a real refresh subprocess making a real HTTP request to
    pypi.org. Tests that specifically want to exercise the notify path
    monkeypatch this to return False.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def update_check_disabled(
    env: Mapping[str, str] | None = None,
    *,
    config_update_check: bool | None = None,
) -> bool:
    """Return True when the update check (notice + background refresh) is off.

    Checked once, at the CLI wiring layer, before either the notice or the
    refresh is attempted -- so an opt-out silences both.

    Args:
        env: Environment mapping to check the opt-out vars against; defaults
            to the real ``os.environ``.
        config_update_check: The loaded config's ``update_check`` field (see
            ``core/config.py``), when one is available. ``None`` means "no
            config was consulted" (e.g. a command that never loads one) and
            never disables anything on its own; only an explicit ``False``
            does. There is no config-driven way to force the check back ON
            when an env var already opted out -- env vars are meant to be the
            override of last resort.
    """
    if _running_under_pytest():
        return True
    if config_update_check is False:
        return True
    active_env = env if env is not None else os.environ
    return any(active_env.get(name) for name in _OPT_OUT_ENV_VARS)


def _read_cache() -> dict[str, Any] | None:
    """Return the parsed cache, or None if missing/unreadable/malformed."""
    try:
        with UPDATE_CACHE_FILE.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_version(raw: str) -> str | None:
    """Parse and canonicalize a version string. None if it doesn't parse.

    A cached (or freshly fetched) version string is stored and re-emitted in
    its canonical ``str(Version(...))`` form rather than verbatim -- PyPI's
    JSON API, a hand-edited cache, or a stray trailing newline can all put
    non-canonical text in the value, and that text is what ends up in the
    printed notice if it isn't normalized first.
    """
    try:
        return str(Version(raw))
    except InvalidVersion:
        return None


def _write_cache(latest: str) -> None:
    """Persist the latest known version. Any failure is logged, never raised."""
    normalized = _normalize_version(latest)
    if normalized is None:
        logger.debug("Refusing to cache an unparsable version: %r", latest)
        return
    try:
        ensure_private_dir(UPDATE_CACHE_FILE.parent)
        with atomic_private_write(UPDATE_CACHE_FILE) as handle:
            json.dump({"last_check": time.time(), "latest": normalized}, handle)
    except OSError as e:
        logger.debug("Could not write update-check cache: %s", e)


def check_for_update(current: str) -> str | None:
    """Return the newer version available, per the cache, or None.

    Reads only the local cache file -- this function never makes a network
    call, so it is safe to call unconditionally at the end of every command.
    A missing, stale-but-present, corrupt, not-actually-newer, or
    pre-release/dev cached version all resolve to None; staleness only
    matters to :func:`refresh_cache_async`, which decides whether to go
    fetch a replacement. A staged pre-release on PyPI (``0.5.0rc1``) is
    deliberately never surfaced here even when it outranks the installed
    version -- ``pip install xnatctl`` wouldn't install it either, so
    nudging a stable user towards it would be misleading.
    """
    data = _read_cache()
    if data is None:
        return None

    latest_raw = data.get("latest")
    if not isinstance(latest_raw, str):
        return None

    normalized = _normalize_version(latest_raw)
    if normalized is None:
        return None
    latest_version = Version(normalized)
    if latest_version.is_prerelease or latest_version.is_devrelease:
        return None

    try:
        current_version = Version(current)
    except InvalidVersion:
        return None

    if latest_version > current_version:
        return normalized
    return None


def _fetch_latest_version(timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """Fetch the latest published version from PyPI. None on any failure.

    Deliberately catches every exception, not a curated list: this call
    crosses DNS, TLS, HTTP, and JSON parsing, each with its own exception
    hierarchy (``OSError``, ``urllib.error.URLError``,
    ``http.client.HTTPException``, ``ValueError``/``json.JSONDecodeError``,
    a malformed-payload ``KeyError``/``TypeError``, ...), and this function's
    entire contract is "return the version, or None -- never raise."
    """
    try:
        with urllib.request.urlopen(PYPI_PROJECT_URL, timeout=timeout) as response:  # noqa: S310
            payload = json.load(response)
        version = payload["info"]["version"]
    except Exception as e:  # noqa: BLE001 -- "return version or None, never raise"
        logger.debug("Update-check fetch failed: %s", e)
        return None
    return version if isinstance(version, str) else None


def fetch_latest_version(timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """Synchronously fetch the latest published version from PyPI. Never raises.

    Public wrapper around :func:`_fetch_latest_version`, for a caller that
    wants an on-demand fetch outside the detached background refresh --
    currently ``xnatctl upgrade --check`` (``cli/upgrade.py``), which passes
    a longer timeout than the passive per-command refresh would ever wait on.
    """
    return _fetch_latest_version(timeout=timeout)


def refresh_cache_now(timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """Synchronously fetch and, on success, persist the latest version. Never raises.

    The blocking counterpart to :func:`refresh_cache_async`: used for an
    explicit, user-requested check (``xnatctl upgrade --check``) rather than
    the passive per-command cache warm.
    """
    latest = fetch_latest_version(timeout=timeout)
    if latest is not None:
        _write_cache(latest)
    return latest


def _cleanup_stale_tmp_files() -> None:
    """Remove orphaned atomic-write temp files from a previously killed refresh.

    ``atomic_private_write`` names its temp file
    ``.{destination-name}.{pid}.tmp`` and unlinks it in a ``finally`` -- but a
    refresh process that dies mid-write (killed, crashed, host rebooted)
    leaves that temp file behind forever, since nothing else ever revisits
    it. Opportunistic hygiene: sweep for them before writing a fresh one.
    Never raises -- a permission error, or racing another refresh process
    that's mid-write right now, is not worth aborting this refresh over.
    """
    pattern = f".{UPDATE_CACHE_FILE.name}.*.tmp"
    try:
        for stale in UPDATE_CACHE_FILE.parent.glob(pattern):
            try:
                stale.unlink()
            except OSError as e:
                logger.debug("Could not remove stale temp file %s: %s", stale, e)
    except OSError as e:
        logger.debug("Could not scan for stale temp files: %s", e)


def _refresh(fetch: Callable[[], str | None]) -> None:
    """Fetch and, on success, overwrite the cache. Never raises.

    ``fetch`` is an injectable seam -- tests substitute their own, including
    ones that deliberately raise to probe failure handling -- so its
    contract of "return str | None, never raise" can't be enforced at the
    type level. The real ``_fetch_latest_version`` already catches its own
    failures, but this is the shared entry point for both the real refresh
    subprocess and any test-injected ``fetch``, so it catches broadly too:
    nothing reachable from here may ever surface as an exception.
    """
    try:
        latest = fetch()
        if latest is not None:
            _write_cache(latest)
    except Exception as e:  # noqa: BLE001 -- shared refresh entry point, must never raise
        logger.debug("Update-check refresh failed: %s", e)


def _spawn_refresh_subprocess() -> subprocess.Popen[bytes] | None:
    """Launch the cache-refreshing child process, detached from this one.

    Stdio is closed and the child gets its own session
    (``start_new_session=True``, POSIX-only -- silently ignored on Windows)
    so it is not tied to this process's controlling terminal or process
    group and keeps running after this process exits. ``Popen()`` itself
    still returns as soon as the child is forked/spawned; it never waits for
    the child, so this call adds no latency here.

    On a normal (non-frozen) install, the child is ``python -m
    xnatctl.core.update_check`` -- a real interpreter running this module's
    :func:`main`. On a frozen (PyInstaller) binary, ``sys.executable`` is
    the xnatctl binary itself, not an interpreter, so that ``-m`` form is
    meaningless to it; instead this re-invokes the binary bare and passes
    :data:`FROZEN_REFRESH_ENV_VAR` in its environment, which ``cli.main.
    main()`` checks before touching Click at all, running the same refresh
    in-process. Both forms are still a detached child process, never a
    thread -- see the module docstring for why a thread can't work here.
    """
    extra_kwargs: dict[str, Any] = {}
    if getattr(sys, "frozen", False):
        argv = [sys.executable]
        extra_kwargs["env"] = {
            **os.environ,
            FROZEN_REFRESH_ENV_VAR: "1",
            # This is a PyInstaller one-file build (see xnatctl.spec: the
            # binaries and datas go straight into EXE, with no COLLECT), so
            # the running binary unpacked itself into a temp directory that
            # it deletes on exit. A child re-invoking that same binary would
            # otherwise inherit the parent's unpack-directory variables and
            # reuse it -- and this parent exits almost immediately, taking
            # the directory out from under the detached child that is
            # supposed to outlive it. This tells the bootloader to unpack
            # its own copy instead.
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
        }
    else:
        argv = [sys.executable, "-m", "xnatctl.core.update_check"]
    try:
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            **extra_kwargs,
        )
    except OSError as e:
        logger.debug("Could not spawn update-check refresh subprocess: %s", e)
        return None


def refresh_cache_async(
    launch: Callable[[], subprocess.Popen[bytes] | None] = _spawn_refresh_subprocess,
) -> subprocess.Popen[bytes] | None:
    """Refresh the cache via a detached child process if it is stale or missing.

    Never blocks the caller: ``launch`` (by default, spawning the child) is
    the only thing this does, and ``Popen()`` doesn't wait for the child.
    Returns whatever ``launch`` returns (mainly so tests -- which replace
    ``launch`` with something that runs the refresh inline instead of
    spawning a process -- can inspect or wait on it deterministically); real
    callers ignore the return value. Returns None without calling ``launch``
    when the cache is still within its 24h TTL.

    A cache whose ``last_check`` is in the future (corrupt, or a clock that
    jumped) is treated as needing a refresh rather than as maximally fresh --
    only a non-negative age under the TTL counts as "still good."

    Multiple xnatctl invocations racing a stale cache each spawn their own
    refresh child; the atomic write means the last one to finish simply wins
    the cache file. That's accepted as harmless fan-out, not guarded against.
    """
    data = _read_cache()
    if data is not None:
        last_check = data.get("last_check")
        if isinstance(last_check, int | float):
            age = time.time() - last_check
            if 0 <= age < CACHE_TTL_SECONDS:
                return None

    return launch()


def main() -> None:
    """Entry point for ``python -m xnatctl.core.update_check``.

    Runs on the detached child process spawned by :func:`refresh_cache_async`.
    Wrapped in a blanket try/except: this process's stdio is already closed
    (so an unhandled traceback would just vanish rather than confuse a user),
    but the broad catch keeps that guarantee explicit rather than accidental,
    and keeps the process exit code clean either way.
    """
    try:
        _cleanup_stale_tmp_files()
        _refresh(fetch=_fetch_latest_version)
    except Exception as e:  # noqa: BLE001 -- detached subprocess, must never raise
        logger.debug("Update-check refresh subprocess failed: %s", e)


if __name__ == "__main__":
    main()
