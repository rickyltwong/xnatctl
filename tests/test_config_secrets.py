"""Password-handling tests for Config/Profile.

`config.yaml` could hold a plaintext password with nothing but file permissions
protecting it, and `Config.save`'s docstring claimed the opposite ("excludes
secrets"). These tests pin the keychain path that replaced it.

The `keyring` module is always faked here -- never install a real backend into
the test environment and never touch a real keychain.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from xnatctl.core.config import (
    KEYRING_SERVICE,
    PASSWORD_SOURCE_KEYRING,
    Config,
    Profile,
    get_credentials,
    keyring_key,
)
from xnatctl.core.exceptions import ConfigurationError
from xnatctl.core.fsutil import POSIX_PERMISSIONS

URL = "https://xnat.example.org"


class FakeKeyring:
    """Minimal stand-in for the keyring module's two functions."""

    def __init__(self, entries: dict[tuple[str, str], str] | None = None):
        self.entries: dict[tuple[str, str], str] = entries or {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.entries.get((service, key))

    def set_password(self, service: str, key: str, password: str) -> None:
        self.entries[(service, key)] = password


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeKeyring]:
    """Inject a fake `keyring` module for the duration of the test."""
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    yield fake


def _profile(**kwargs: Any) -> Profile:
    return Profile(url=URL, name="prod", **kwargs)


# =============================================================================
# Serialization
# =============================================================================


def test_keyring_profile_never_serializes_a_password() -> None:
    """The whole point of moving the secret is that it stops being written."""
    profile = _profile(password="s3cret", password_source=PASSWORD_SOURCE_KEYRING)

    data = profile.to_dict()

    assert data["password_source"] == PASSWORD_SOURCE_KEYRING
    assert "password" not in data


def test_inline_password_still_serializes() -> None:
    """Existing configs keep working; keyring support adds an option, not a removal."""
    assert _profile(password="s3cret").to_dict()["password"] == "s3cret"


def test_password_source_round_trips_through_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = Config()
    config.add_profile("prod", url=URL)
    config.profiles["prod"].password_source = PASSWORD_SOURCE_KEYRING
    config.save(config_path=path)

    on_disk = yaml.safe_load(path.read_text())
    assert on_disk["profiles"]["prod"]["password_source"] == PASSWORD_SOURCE_KEYRING
    assert "password" not in on_disk["profiles"]["prod"]

    reloaded = Config.load(config_path=path)
    assert reloaded.profiles["prod"].password_source == PASSWORD_SOURCE_KEYRING


def test_loaded_profiles_know_their_own_name(tmp_path: Path) -> None:
    """The keyring key is built from the profile name, so the name has to
    survive deserialization.
    """
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"default_profile": "prod", "profiles": {"prod": {"url": URL}}}))

    assert Config.load(config_path=path).profiles["prod"].name == "prod"


def test_profile_without_password_source_omits_the_key() -> None:
    """An untouched profile must not gain a new key -- a future change will version the
    schema, and until then the file should stay as it was.
    """
    assert "password_source" not in _profile().to_dict()


# =============================================================================
# Resolution
# =============================================================================


def test_inline_password_resolves_without_touching_the_keychain() -> None:
    assert _profile(password="s3cret").resolve_password() == "s3cret"


def test_keyring_password_is_read_from_the_keychain(fake_keyring: FakeKeyring) -> None:
    fake_keyring.set_password(KEYRING_SERVICE, keyring_key("prod", URL), "from-keychain")
    profile = _profile(password_source=PASSWORD_SOURCE_KEYRING)

    assert profile.resolve_password() == "from-keychain"


def test_missing_keychain_entry_raises_with_the_fix_command(fake_keyring: FakeKeyring) -> None:
    profile = _profile(password_source=PASSWORD_SOURCE_KEYRING)

    with pytest.raises(ConfigurationError) as exc_info:
        profile.resolve_password()

    assert "xnatctl config set-password prod" in str(exc_info.value)


def test_unnamed_keyring_profile_raises_rather_than_guessing() -> None:
    profile = Profile(url=URL, password_source=PASSWORD_SOURCE_KEYRING)

    with pytest.raises(ConfigurationError) as exc_info:
        profile.resolve_password()

    assert "no name" in str(exc_info.value)


# =============================================================================
# get_credentials -- the chokepoint every client construction goes through
# =============================================================================


def test_get_credentials_reads_the_keychain(
    fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XNAT_PASS", raising=False)
    monkeypatch.delenv("XNAT_USER", raising=False)
    fake_keyring.set_password(KEYRING_SERVICE, keyring_key("prod", URL), "from-keychain")
    profile = _profile(username="admin", password_source=PASSWORD_SOURCE_KEYRING)

    assert get_credentials(profile) == ("admin", "from-keychain")


def test_env_password_wins_and_skips_the_keychain(
    fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env password must short-circuit before the keychain: with no entry
    stored, consulting it would raise.
    """
    monkeypatch.setenv("XNAT_PASS", "from-env")
    monkeypatch.delenv("XNAT_USER", raising=False)
    profile = _profile(username="admin", password_source=PASSWORD_SOURCE_KEYRING)

    assert get_credentials(profile) == ("admin", "from-env")


# =============================================================================
# Load-time exposure warning
# =============================================================================


@pytest.mark.skipif(
    not POSIX_PERMISSIONS, reason="POSIX permission bits are not meaningful on this platform"
)
def test_world_readable_config_with_inline_password_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump({"profiles": {"prod": {"url": URL, "password": "s3cret"}}}),
    )
    os.chmod(path, 0o644)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
        Config.load(config_path=path)

    assert any("readable by other users" in r.message for r in caplog.records)
    assert not any("s3cret" in r.getMessage() for r in caplog.records), "warning leaked the secret"


def test_private_config_with_inline_password_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"prod": {"url": URL, "password": "s3cret"}}}))
    os.chmod(path, 0o600)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
        Config.load(config_path=path)

    assert caplog.records == []


def test_world_readable_config_without_a_password_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"prod": {"url": URL}}}))
    os.chmod(path, 0o644)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
        Config.load(config_path=path)

    assert caplog.records == []


def test_no_permission_warning_where_modes_are_meaningless(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the mode check is skipped entirely.

    ``os.stat`` reports 0o666 for every writable file there, so the check would
    fire on every command and tell the user to run a ``chmod`` that neither
    exists on that platform nor would help. Exercised by patching the flag, so
    the behaviour stays covered on the POSIX machines that run this suite.
    """
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"prod": {"url": URL, "password": "s3cret"}}}))
    os.chmod(path, 0o644)
    monkeypatch.setattr("xnatctl.core.config.POSIX_PERMISSIONS", False)

    with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
        Config.load(config_path=path)

    assert caplog.records == []
