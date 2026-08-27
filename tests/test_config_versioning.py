"""Tests for config.yaml / session cache schema versioning.

Covers three things: a versionless file behaves exactly like a `version: 1`
file, unknown keys warn instead of erroring, and the migration hook applies
registered migrations in order without ever rewriting the file on a bare read.
The session cache side has no migration table -- any version mismatch just
discards the cache, so it is covered separately at the bottom.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import core_config_seam

import xnatctl.core.config as config_module
from xnatctl.cli.main import cli
from xnatctl.core.auth import AuthManager, CachedSession
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ConfigurationError

# =============================================================================
# config.yaml versioning
# =============================================================================


class TestConfigVersioning:
    def test_versionless_file_loads_as_v1(self, tmp_path: Path) -> None:
        """A config.yaml with no `version` key is version 1, unchanged."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "default_profile: test\n"
            "output_format: table\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
            "    verify_ssl: false\n"
            "    timeout: 30\n"
        )

        cfg = Config.load(config_path)

        assert cfg.default_profile == "test"
        assert cfg.output_format == "table"
        assert cfg.version == 1
        assert cfg.profiles["test"].url == "https://xnat-test.example.org"
        assert cfg.profiles["test"].verify_ssl is False
        assert cfg.profiles["test"].timeout == 30

    def test_unknown_keys_warn_but_load(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown top-level and profile keys warn once and are ignored."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\n"
            "default_profile: test\n"
            "output_format: table\n"
            "future_top_level_thing: yes\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
            "    future_profile_thing: 42\n"
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        # Loads fine despite the unrecognized keys.
        assert cfg.profiles["test"].url == "https://xnat-test.example.org"

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "future_top_level_thing" in warnings[0]
        assert "future_profile_thing" in warnings[0]

    def test_known_keys_never_warn(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Every field the code actually reads must be silent, not just tolerated.

        Exercises all 13 known ``ProfileModel`` fields (the `sample_config_yaml`
        fixture only covers 4), plus every top-level field.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\n"
            "default_profile: full\n"
            "output_format: json\n"
            "profiles:\n"
            "  full:\n"
            "    url: https://xnat.example.org\n"
            "    verify_ssl: false\n"
            "    ca_bundle: /etc/ssl/certs/custom.pem\n"
            "    timeout: 120\n"
            "    default_project: PROJ1\n"
            "    username: alice\n"
            "    password: hunter2\n"
            "    password_source: keyring\n"
            "    workers: 8\n"
            "    overwrite: append\n"
            "    direct_archive: true\n"
            "    archive_mode: zip\n"
            "    extract: true\n"
        )
        # 0600 so the plaintext `password:` field above doesn't also trip the
        # unrelated exposed-password warning and pollute this assertion.
        config_path.chmod(0o600)

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert caplog.records == []
        profile = cfg.profiles["full"]
        assert profile.ca_bundle == "/etc/ssl/certs/custom.pem"
        assert profile.workers == 8
        assert profile.archive_mode == "zip"

    def test_migration_applied_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chained migrations run from the file's version up to current, in order."""

        def mig_1_to_2(data: dict[str, Any]) -> dict[str, Any]:
            data = dict(data)
            data["output_format"] = data.get("output_format", "") + "-v2"
            return data

        def mig_2_to_3(data: dict[str, Any]) -> dict[str, Any]:
            data = dict(data)
            data["output_format"] = data.get("output_format", "") + "-v3"
            return data

        monkeypatch.setattr(config_module, "CURRENT_CONFIG_VERSION", 3)
        monkeypatch.setattr(config_module, "MIGRATIONS", {1: mig_1_to_2, 2: mig_2_to_3})

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\ndefault_profile: default\noutput_format: json\nprofiles: {}\n"
        )

        cfg = Config.load(config_path)

        # Migrations ran in order (v1->v2 before v2->v3), and only in memory.
        assert cfg.output_format == "json-v2-v3"
        assert "version: 1" in config_path.read_text()

    def test_newer_version_warns_and_loads(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file from a newer xnatctl loads best-effort with a warning."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 999\n"
            "default_profile: test\n"
            "output_format: table\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert cfg.profiles["test"].url == "https://xnat-test.example.org"

        version_warnings = [
            r.getMessage() for r in caplog.records if "newer than this xnatctl" in r.getMessage()
        ]
        assert len(version_warnings) == 1
        assert "999" in version_warnings[0]
        assert str(config_module.CURRENT_CONFIG_VERSION) in version_warnings[0]

    def test_config_init_writes_version(self, tmp_path: Path) -> None:
        """A freshly written config.yaml declares its schema version."""
        config_file = tmp_path / "config.yaml"

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
        ):
            result = CliRunner().invoke(
                cli,
                ["config", "init", "--url", "https://xnat.example.org", "--no-login"],
            )

        assert result.exit_code == 0
        contents = config_file.read_text()
        assert "version: 1" in contents
        # It is the first line, not merely present somewhere in the file.
        assert contents.strip().splitlines()[0] == "version: 1"

    def test_non_numeric_version_defaults_to_1_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`version: banana` must not brick the load -- a sanity check that
        resets only a local variable leaves the bad value in the dict that
        gets validated, raising straight out of Pydantic's `version: int`
        field.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: banana\n"
            "default_profile: test\n"
            "output_format: table\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert cfg.profiles["test"].url == "https://xnat-test.example.org"
        assert cfg.version == 1
        bad_version_warnings = [
            r.getMessage() for r in caplog.records if "invalid config version" in r.getMessage()
        ]
        assert len(bad_version_warnings) == 1
        assert "banana" in bad_version_warnings[0]

    def test_float_version_defaults_to_1_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`version: 1.5` is not an int Pydantic will accept either -- same
        brick, same fix.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1.5\ndefault_profile: default\noutput_format: table\nprofiles: {}\n"
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert cfg.default_profile == "default"
        assert cfg.version == 1
        assert any("invalid config version" in r.getMessage() for r in caplog.records)

    def test_quoted_integer_version_string_is_honored(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A quoted `version: "999"` must mean the same as an unquoted
        `version: 999` -- YAML's own type inference should not silently
        change which version this file declares.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'version: "999"\ndefault_profile: default\noutput_format: table\nprofiles: {}\n'
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert cfg.version == 999
        version_warnings = [
            r.getMessage() for r in caplog.records if "newer than this xnatctl" in r.getMessage()
        ]
        assert len(version_warnings) == 1
        assert "999" in version_warnings[0]

    def test_quoted_version_string_feeds_migration_dispatch_correctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quoted `version: "2"` must dispatch as version 2, not fall back
        to 1 and rerun the 1->2 migration on an already-migrated document.
        """

        def mig_1_to_2(data: dict[str, Any]) -> dict[str, Any]:
            data = dict(data)
            data["output_format"] = data.get("output_format", "") + "-mig1"
            return data

        def mig_2_to_3(data: dict[str, Any]) -> dict[str, Any]:
            data = dict(data)
            data["output_format"] = data.get("output_format", "") + "-mig2"
            return data

        monkeypatch.setattr(config_module, "CURRENT_CONFIG_VERSION", 3)
        monkeypatch.setattr(config_module, "MIGRATIONS", {1: mig_1_to_2, 2: mig_2_to_3})

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'version: "2"\ndefault_profile: default\noutput_format: base\nprofiles: {}\n'
        )

        cfg = Config.load(config_path)

        # Only mig_2_to_3 should have run. If the quoted "2" were misread as
        # 1, mig_1_to_2 would have run too and this would read "base-mig1-mig2".
        assert cfg.output_format == "base-mig2"

    def test_unquoted_yaml_boolean_key_does_not_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """PyYAML parses a bare `yes:`/`no:` key as a Python bool under YAML
        1.1. Pydantic's `extra="allow"` requires string keys and raises on
        anything else, so a config carrying one of these -- which the old
        hand-rolled loader tolerated silently -- must keep loading.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\n"
            "default_profile: test\n"
            "output_format: table\n"
            "yes: this key parses as the bool True\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
            "    no: this one too, inside a profile\n"
        )

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.config"):
            cfg = Config.load(config_path)

        assert cfg.profiles["test"].url == "https://xnat-test.example.org"

        # One aggregated warning naming both sanitized keys: PyYAML's `yes:`
        # becomes the top-level key `True`, `no:` (inside the profile)
        # becomes `False` -- str()'d by `_sanitize_keys` before Pydantic
        # ever sees them.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "True" in warnings[0]
        assert "False" in warnings[0]

    def test_missing_intermediate_migration_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A v1 file with current=3 and only a 1->2 migration registered has
        a gap at 2->3. Silently stopping partway would validate a
        half-migrated document and then stamp it v3 on the next save --
        this must fail loudly instead.
        """

        def mig_1_to_2(data: dict[str, Any]) -> dict[str, Any]:
            return dict(data)

        monkeypatch.setattr(config_module, "CURRENT_CONFIG_VERSION", 3)
        monkeypatch.setattr(config_module, "MIGRATIONS", {1: mig_1_to_2})

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\ndefault_profile: default\noutput_format: table\nprofiles: {}\n"
        )

        with pytest.raises(ConfigurationError):
            Config.load(config_path)

    def test_newer_version_save_is_refused_not_downgraded(self, tmp_path: Path) -> None:
        """Loading a config newer than this build understands, then saving
        it (e.g. via `config use-context`), must refuse rather than silently
        drop the unrecognized field and relabel the file as the older,
        current version.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 2\n"
            "default_profile: test\n"
            "output_format: table\n"
            "future_auth: oidc\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
        )
        original_bytes = config_path.read_bytes()

        cfg = Config.load(config_path)
        assert cfg.version == 2

        with pytest.raises(ConfigurationError):
            cfg.save(config_path)

        # No downgrade, no data loss: the file on disk is untouched.
        assert config_path.read_bytes() == original_bytes

    def test_fresh_config_still_saves_normally(self, tmp_path: Path) -> None:
        """The fail-closed save check must not affect an in-memory config
        that was never loaded from a newer-than-current file.
        """
        cfg = Config(default_profile="default")
        cfg.add_profile(name="default", url="https://xnat.example.org")

        config_path = tmp_path / "config.yaml"
        cfg.save(config_path)

        assert config_path.exists()


# =============================================================================
# Session cache versioning
# =============================================================================


class TestSessionCacheVersioning:
    def test_versionless_cache_loads(self, tmp_path: Path) -> None:
        """A cache written before the version field existed is version 1."""
        cache_file = tmp_path / ".session"
        session = CachedSession(
            token="tok",
            url="https://xnat.example.org",
            username="alice",
            created_at=datetime.now(),
        )
        payload = session.to_dict()
        del payload["version"]
        cache_file.write_text(json.dumps(payload))

        manager = AuthManager(cache_file=cache_file)
        loaded = manager.load_session(url="https://xnat.example.org")

        assert loaded is not None
        assert loaded.token == "tok"

    def test_unknown_version_discards_cache(self, tmp_path: Path) -> None:
        """A cache with a version this build does not recognize is discarded,
        not crashed on -- the caller falls through to a normal re-auth.
        """
        cache_file = tmp_path / ".session"
        session = CachedSession(
            token="tok",
            url="https://xnat.example.org",
            username="alice",
            created_at=datetime.now(),
        )
        payload = session.to_dict()
        payload["version"] = 999
        cache_file.write_text(json.dumps(payload))

        manager = AuthManager(cache_file=cache_file)
        loaded = manager.load_session(url="https://xnat.example.org")

        assert loaded is None
        assert not cache_file.exists()

    def test_boolean_version_discards_cache(self, tmp_path: Path) -> None:
        """JSON `true` must not be accepted as a stand-in for version 1 just
        because `true == 1` -- the version check has to be type-strict.
        """
        cache_file = tmp_path / ".session"
        session = CachedSession(
            token="tok",
            url="https://xnat.example.org",
            username="alice",
            created_at=datetime.now(),
        )
        payload = session.to_dict()
        payload["version"] = True
        cache_file.write_text(json.dumps(payload))

        manager = AuthManager(cache_file=cache_file)
        loaded = manager.load_session(url="https://xnat.example.org")

        assert loaded is None
        assert not cache_file.exists()

    def test_float_version_discards_cache(self, tmp_path: Path) -> None:
        """JSON `1.0` must not be accepted as version 1 either, for the same
        `1.0 == 1` reason.
        """
        cache_file = tmp_path / ".session"
        session = CachedSession(
            token="tok",
            url="https://xnat.example.org",
            username="alice",
            created_at=datetime.now(),
        )
        payload = session.to_dict()
        payload["version"] = 1.0
        cache_file.write_text(json.dumps(payload))

        manager = AuthManager(cache_file=cache_file)
        loaded = manager.load_session(url="https://xnat.example.org")

        assert loaded is None
        assert not cache_file.exists()


# =============================================================================
# Single warning per command, through the real CLI
# =============================================================================


class TestConfigShowWarnsOnce:
    def test_config_show_warns_once_not_twice(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`config show` must not load the config twice -- once via
        @global_options into ctx.config, once more in its own body -- or an
        unknown-key warning prints twice for one command invocation.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 1\n"
            "default_profile: test\n"
            "output_format: table\n"
            "future_top_level_thing: yes\n"
            "profiles:\n"
            "  test:\n"
            "    url: https://xnat-test.example.org\n"
        )

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
            caplog.at_level(logging.WARNING, logger="xnatctl.core.config"),
        ):
            result = CliRunner().invoke(cli, ["config", "show"])

        assert result.exit_code == 0
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


# =============================================================================
# Newer-version refusal, through the real CLI, for every mutating command
# =============================================================================


class TestNewerVersionMutatingCommandsViaCLI:
    """Every mutating `config` command must refuse against a config loaded
    from a version newer than this build understands -- except
    `config init --force`, the documented, deliberate exception.
    """

    def test_use_context_refuses(self) -> None:
        cfg = Config(
            default_profile="test",
            profiles={
                "test": Profile(url="https://xnat-test.example.org", name="test"),
                "other": Profile(url="https://xnat-other.example.org", name="other"),
            },
            version=2,
        )

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = CliRunner().invoke(cli, ["config", "use-context", "other"])

        # The in-memory switch may have happened before the refused save
        # (Config.set_default_profile has no way to know save() will fail),
        # but the command as a whole must not report success.
        assert result.exit_code != 0

    def test_add_profile_refuses(self) -> None:
        cfg = Config(
            default_profile="test",
            profiles={"test": Profile(url="https://xnat-test.example.org", name="test")},
            version=2,
        )

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = CliRunner().invoke(
                cli,
                ["config", "add-profile", "newprof", "--url", "https://new.example.org"],
            )

        assert result.exit_code != 0

    def test_remove_profile_refuses(self) -> None:
        cfg = Config(
            default_profile="test",
            profiles={
                "test": Profile(url="https://xnat-test.example.org", name="test"),
                "extra": Profile(url="https://xnat-extra.example.org", name="extra"),
            },
            version=2,
        )

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = CliRunner().invoke(cli, ["config", "remove-profile", "extra", "--yes"])

        assert result.exit_code != 0

    def test_set_password_refuses_before_any_keyring_write(self) -> None:
        """The one command with an external side effect: the version check
        must fire before the keychain is ever touched, not after.
        """
        cfg = Config(
            default_profile="prod",
            profiles={"prod": Profile(url="https://xnat.example.org", name="prod")},
            version=2,
        )
        fake_keyring = MagicMock()

        with (
            core_config_seam(cfg),
            patch.dict("sys.modules", {"keyring": fake_keyring}),
        ):
            result = CliRunner().invoke(
                cli, ["config", "set-password", "prod"], input="s3cret\ns3cret\n"
            )

        assert result.exit_code != 0
        fake_keyring.set_password.assert_not_called()

    def test_init_force_warns_but_overwrites_newer_version(self, tmp_path: Path) -> None:
        """`config init --force` is the documented exception: it still
        succeeds against a newer-version file (that is the whole point of
        --force), but warns first rather than overwriting silently.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 2\n"
            "default_profile: default\n"
            "output_format: table\n"
            "future_auth: oidc\n"
            "profiles:\n"
            "  default:\n"
            "    url: https://old.example.org\n"
        )

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "config",
                    "init",
                    "--url",
                    "https://xnat.example.org",
                    "--no-login",
                    "--force",
                ],
            )

        assert result.exit_code == 0
        assert "declares config version 2" in result.output
        # --force did overwrite it, back down to the current version.
        assert "version: 1" in config_file.read_text()
