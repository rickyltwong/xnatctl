from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from xnatctl.core.auth import AuthManager
from xnatctl.core.config import Config, Profile
from xnatctl.core.fsutil import POSIX_PERMISSIONS, atomic_private_write


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_umask() -> Iterator[int]:
    previous = os.umask(0o022)
    try:
        yield previous
    finally:
        os.umask(previous)


@pytest.fixture
def group_writable_umask() -> Iterator[int]:
    previous = os.umask(0o002)
    try:
        yield previous
    finally:
        os.umask(previous)


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("loose_umask")
def test_session_cache_is_owner_only_under_loose_umask(tmp_path: Path) -> None:
    cache = tmp_path / "cfgdir" / ".session"
    AuthManager(cache_file=cache).save_session("TOK", "https://x.example.org", "u")

    assert mode_of(cache) == 0o600


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("group_writable_umask")
def test_session_cache_is_owner_only_under_group_writable_umask(tmp_path: Path) -> None:
    cache = tmp_path / "cfgdir" / ".session"
    AuthManager(cache_file=cache).save_session("TOK", "https://x.example.org", "u")

    assert mode_of(cache) == 0o600


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("loose_umask")
def test_created_parent_directory_is_owner_only(tmp_path: Path) -> None:
    parent = tmp_path / "cfgdir"
    AuthManager(cache_file=parent / ".session").save_session("TOK", "https://x", "u")

    assert mode_of(parent) == 0o700


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
def test_existing_parent_directory_permissions_are_left_alone(tmp_path: Path) -> None:
    parent = tmp_path / "cfgdir"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)

    AuthManager(cache_file=parent / ".session").save_session("TOK", "https://x", "u")

    assert mode_of(parent) == 0o755


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
def test_rewriting_a_world_readable_cache_tightens_it(tmp_path: Path) -> None:
    cache = tmp_path / ".session"
    cache.write_text("{}")
    os.chmod(cache, 0o644)

    AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert mode_of(cache) == 0o600


def test_saved_session_round_trips(tmp_path: Path) -> None:
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


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("group_writable_umask")
def test_cache_mode_does_not_depend_on_chmod_succeeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / ".session"
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)

    AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert mode_of(cache) == 0o600


def test_failing_chmod_warns_instead_of_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = tmp_path / ".session"
    real_chmod = os.chmod

    def failing_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if str(path) == str(cache):
            raise OSError(1, "Operation not permitted")
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", failing_chmod)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.fsutil"):
        AuthManager(cache_file=cache).save_session("TOK", "https://x", "u")

    assert any("Could not restrict permissions" in r.message for r in caplog.records)


def test_failing_unlink_warns_instead_of_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = tmp_path / ".session"
    manager = AuthManager(cache_file=cache)
    manager.save_session("TOK", "https://x", "u")

    def failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.auth"):
        result = manager.clear_session()

    assert result is False
    assert any("Could not clear cached session" in r.message for r in caplog.records)


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("group_writable_umask")
def test_config_file_is_owner_only(tmp_path: Path) -> None:
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


def test_atomic_write_leaves_original_intact_on_error(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("original")

    with pytest.raises(RuntimeError):
        with atomic_private_write(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")

    assert target.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secret.txt"]


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
@pytest.mark.usefixtures("group_writable_umask")
def test_atomic_write_creates_owner_only_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"

    with atomic_private_write(target) as handle:
        handle.write("s3cret")

    assert mode_of(target) == 0o600
    assert target.read_text() == "s3cret"
