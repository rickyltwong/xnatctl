"""Permission and atomicity tests for the on-disk secrets.

The session cache holds a live JSESSIONID and config.yaml can hold a plaintext
profile password. Both used to be created with ``open(path, "w")``, which
applies the process umask -- 0664 on this host -- with a follow-up ``chmod``
that was wrapped in ``except OSError: pass``. These tests pin the three
properties that replaced it: the file is 0600 whatever the umask, a rewrite
tightens a file that is already too permissive, and a failing chmod is reported
rather than swallowed.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from xnatctl.core.auth import AuthManager
from xnatctl.core.config import Config, Profile
from xnatctl.core.fsutil import atomic_private_write


def mode_of(path: Path) -> int:
    """Permission bits of ``path``, e.g. 0o600."""
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_umask() -> Iterator[int]:
    """Run the test under a permissive umask, then restore the real one."""
    previous = os.umask(0o022)
    try:
        yield previous
    finally:
        os.umask(previous)


@pytest.fixture
def group_writable_umask() -> Iterator[int]:
    """The umask this host actually runs (yields 0664 files)."""
    previous = os.umask(0o002)
    try:
        yield previous
    finally:
        os.umask(previous)


# =============================================================================
# Session cache
# =============================================================================


@pytest.mark.usefixtures("loose_umask")
def test_session_cache_is_owner_only_under_loose_umask(tmp_path: Path) -> None:
    cache = tmp_path / "cfgdir" / ".session"
    AuthManager(cache_file=cache).save_session("TOK", "https://x.example.org", "u")

    assert mode_of(cache) == 0o600


@pytest.mark.usefixtures("group_writable_umask")
def test_session_cache_is_owner_only_under_group_writable_umask(tmp_path: Path) -> None:
    """The motivating regression: umask 0o002 yielded a 0664 token."""
    cache = tmp_path / "cfgdir" / ".session"
    AuthManager(cache_file=cache).save_session("TOK", "https://x.example.org", "u")

    assert mode_of(cache) == 0o600


@pytest.mark.usefixtures("loose_umask")
def test_created_parent_directory_is_owner_only(tmp_path: Path) -> None:
    parent = tmp_path / "cfgdir"
    AuthManager(cache_file=parent / ".session").save_session("TOK", "https://x", "u")

    assert mode_of(parent) == 0o700


def test_existing_parent_directory_permissions_are_left_alone(tmp_path: Path) -> None:
    """Only directories we create are tightened; an existing one may be the
    user's deliberate choice, and silently changing it would be a surprise."""
    parent = tmp_path / "cfgdir"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)

    AuthManager(cache_file=parent / ".session").save_session("TOK", "https://x", "u")

    assert mode_of(parent) == 0o755


def test_rewriting_a_world_readable_cache_tightens_it(tmp_path: Path) -> None:
    """``opener=`` only applies its mode to files it creates, so an in-place
    rewrite of a 0644 file would have stayed 0644. The atomic replace hands the
    destination the temp file's 0600 instead."""
    cache = tmp_path / ".session"
    cache.write_text("{}")
    os.chmod(cache, 0o644)

    AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert mode_of(cache) == 0o600


def test_saved_session_round_trips(tmp_path: Path) -> None:
    """Permissions work must not disturb the payload."""
    cache = tmp_path / ".session"
    manager = AuthManager(cache_file=cache)
    manager.save_session("TOK", "https://x.example.org", "alice")

    loaded = manager.load_session()
    assert loaded is not None
    assert loaded.token == "TOK"
    assert loaded.username == "alice"


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    cache = tmp_path / ".session"
    AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert sorted(p.name for p in tmp_path.iterdir()) == [".session"]


@pytest.mark.usefixtures("group_writable_umask")
def test_cache_mode_does_not_depend_on_chmod_succeeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core invariant.

    The old code created the file under the umask and relied on a follow-up
    chmod to fix it, so the token was briefly group-readable and permanently so
    if the chmod failed. With the file created 0600 up front, neutering chmod
    entirely must not change the result.
    """
    cache = tmp_path / ".session"
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)

    AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert mode_of(cache) == 0o600


def test_failing_chmod_warns_instead_of_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The old code was ``except OSError: pass`` -- a failed chmod left the
    token world-readable with no log line at all."""
    cache = tmp_path / ".session"
    real_chmod = os.chmod

    def failing_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if str(path) == str(cache):
            raise OSError(1, "Operation not permitted")
        real_chmod(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", failing_chmod)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.fsutil"):
        AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert any("Could not restrict permissions" in r.message for r in caplog.records)


# =============================================================================
# config.yaml
# =============================================================================


@pytest.mark.usefixtures("group_writable_umask")
def test_config_file_is_owner_only(tmp_path: Path) -> None:
    """config.yaml can carry a plaintext password today, so it gets the same
    treatment as the session cache."""
    path = tmp_path / "cfgdir" / "config.yaml"
    config = Config()
    config.profiles["default"] = Profile(url="https://x.example.org")

    config.save(config_path=path)

    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700


def test_config_round_trips_after_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = Config()
    config.profiles["prod"] = Profile(url="https://x.example.org")
    config.default_profile = "prod"
    config.save(config_path=path)

    loaded = Config.load(config_path=path)
    assert loaded.default_profile == "prod"
    assert loaded.profiles["prod"].url == "https://x.example.org"


# =============================================================================
# atomic_private_write
# =============================================================================


def test_atomic_write_leaves_original_intact_on_error(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("original")

    with pytest.raises(RuntimeError):
        with atomic_private_write(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")

    assert target.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secret.txt"]


@pytest.mark.usefixtures("group_writable_umask")
def test_atomic_write_creates_owner_only_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"

    with atomic_private_write(target) as handle:
        handle.write("s3cret")

    assert mode_of(target) == 0o600
    assert target.read_text() == "s3cret"
