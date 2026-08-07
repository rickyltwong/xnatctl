"""Tests for xnatctl CLI config commands."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Drop Rich's colour codes so assertions can see the literal message."""
    return _ANSI_RE.sub("", text)


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _mock_config() -> Config:
    """Build a mock Config with profiles."""
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                verify_ssl=True,
                default_project="PROJ1",
            ),
            "dev": Profile(
                url="https://xnat-dev.example.org",
                verify_ssl=False,
            ),
        },
    )


class TestConfigInit:
    """Tests for config init command."""

    def test_config_init_new(self, runner: CliRunner, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"

        with patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file):
            with patch("xnatctl.cli.config_cmd.Config") as mock_cls:
                mock_cfg = MagicMock()
                mock_cfg.profiles = {}
                mock_cls.return_value = mock_cfg
                mock_cls.load.return_value = mock_cfg

                result = runner.invoke(
                    cli,
                    ["config", "init", "--url", "https://xnat.example.org"],
                )

        assert result.exit_code == 0

    def test_config_init_existing_profile_no_force(self, runner: CliRunner, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("dummy")

        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file):
            with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
                result = runner.invoke(
                    cli,
                    ["config", "init", "--url", "https://xnat.example.org"],
                )

        assert result.exit_code != 0

    def test_config_init_with_force(self, runner: CliRunner, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("dummy")

        with patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file):
            with patch("xnatctl.cli.config_cmd.Config") as mock_cls:
                mock_cfg = MagicMock()
                mock_cfg.profiles = {"default": MagicMock()}
                mock_cls.return_value = mock_cfg

                result = runner.invoke(
                    cli,
                    [
                        "config",
                        "init",
                        "--url",
                        "https://xnat.example.org",
                        "--force",
                    ],
                )

        assert result.exit_code == 0


class TestConfigShow:
    """Tests for config show command."""

    def test_config_show_table(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code == 0
        assert "default" in result.output

    def test_config_show_json(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "show", "-o", "json"])

        assert result.exit_code == 0
        assert "xnat.example.org" in result.output

    def test_config_show_no_profiles(self, runner: CliRunner) -> None:
        cfg = Config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with config_seam(cfg):
                result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code != 0

    def test_config_show_filter_by_profile(self, runner: CliRunner) -> None:
        """``-p NAME`` restricts profile-detail sections to that single profile."""
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with config_seam(cfg):
                result = runner.invoke(cli, ["config", "show", "-p", "dev"])

        assert result.exit_code == 0
        # The selected profile name appears in the per-profile section.
        assert "dev" in result.output
        # The non-selected profile's URL does NOT appear in the output.
        assert "xnat.example.org" not in result.output
        assert "xnat-dev.example.org" in result.output

    def test_config_show_filter_by_profile_json(self, runner: CliRunner) -> None:
        """``-p NAME -o json`` narrows ``profiles`` and ``profile_details``."""
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with config_seam(cfg):
                result = runner.invoke(cli, ["config", "show", "-p", "dev", "-o", "json"])

        assert result.exit_code == 0
        import json as _json

        payload = _json.loads(result.output)
        assert payload["profiles"] == ["dev"]
        assert list(payload["profile_details"].keys()) == ["dev"]

    def test_config_show_unknown_profile_errors(self, runner: CliRunner) -> None:
        """Unknown ``-p NAME`` exits non-zero and lists available profiles."""
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with config_seam(cfg):
                result = runner.invoke(cli, ["config", "show", "-p", "nonexist"])

        assert result.exit_code != 0
        assert "nonexist" in result.output
        # Available profiles are listed.
        assert "default" in result.output
        assert "dev" in result.output


class TestConfigUseContext:
    """Tests for config use-context command."""

    def test_use_context_success(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with patch.object(cfg, "save") as mock_save:
                result = runner.invoke(cli, ["config", "use-context", "dev"])

        assert result.exit_code == 0
        assert "dev" in result.output
        mock_save.assert_called_once()

    def test_use_context_not_found(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "use-context", "nonexist"])

        assert result.exit_code != 0


class TestConfigCurrentContext:
    """Tests for config current-context command."""

    def test_current_context(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "current-context"])

        assert result.exit_code == 0
        assert "default" in result.output


class TestConfigAddProfile:
    """Tests for config add-profile command."""

    def test_add_profile_success(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with patch.object(cfg, "save"):
                result = runner.invoke(
                    cli,
                    [
                        "config",
                        "add-profile",
                        "staging",
                        "--url",
                        "https://xnat-staging.example.org",
                    ],
                )

        assert result.exit_code == 0
        assert "staging" in result.output

    def test_add_profile_duplicate(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(
                cli,
                [
                    "config",
                    "add-profile",
                    "default",
                    "--url",
                    "https://xnat.example.org",
                ],
            )

        assert result.exit_code != 0

    def test_add_profile_with_options(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with patch.object(cfg, "save"):
                result = runner.invoke(
                    cli,
                    [
                        "config",
                        "add-profile",
                        "staging",
                        "--url",
                        "https://xnat-staging.example.org",
                        "--project",
                        "MYPROJ",
                        "--no-verify-ssl",
                    ],
                )

        assert result.exit_code == 0


class TestConfigRemoveProfile:
    """Tests for config remove-profile command."""

    def test_remove_profile_success(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            with patch.object(cfg, "save"):
                result = runner.invoke(cli, ["config", "remove-profile", "dev", "--yes"])

        assert result.exit_code == 0
        assert "removed" in result.output

    def test_remove_profile_not_found(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "remove-profile", "nonexist", "--yes"])

        assert result.exit_code != 0

    def test_remove_default_profile(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "remove-profile", "default", "--yes"])

        assert result.exit_code != 0

    def test_remove_profile_abort(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(cli, ["config", "remove-profile", "dev"], input="n\n")

        assert result.exit_code != 0


class TestConfigSetPassword:
    """Tests for `config set-password`.

    The command exists because nothing else could put a password into a
    profile: `add-profile` has no password option and `auth login` only caches
    a session token, so before this the only route was hand-editing the YAML.
    """

    def _config(self) -> Config:
        return Config(
            default_profile="prod",
            profiles={"prod": Profile(url="https://xnat.example.org", name="prod")},
        )

    def test_password_is_stored_in_the_keychain_not_the_file(self, runner: CliRunner) -> None:
        cfg = self._config()
        fake_keyring = MagicMock()

        with (
            core_config_seam(cfg),
            patch.dict("sys.modules", {"keyring": fake_keyring}),
            patch.object(Config, "save") as save,
        ):
            result = runner.invoke(
                cli, ["config", "set-password", "prod"], input="s3cret\ns3cret\n"
            )

        assert result.exit_code == 0
        fake_keyring.set_password.assert_called_once_with(
            "xnatctl", "prod@https://xnat.example.org", "s3cret"
        )
        save.assert_called_once()
        assert cfg.profiles["prod"].password_source == "keyring"
        assert cfg.profiles["prod"].password is None

    def test_inline_password_is_dropped_on_migration(self, runner: CliRunner) -> None:
        """The migration path for a profile that already holds plaintext."""
        cfg = self._config()
        cfg.profiles["prod"].password = "old-plaintext"

        with (
            core_config_seam(cfg),
            patch.dict("sys.modules", {"keyring": MagicMock()}),
            patch.object(Config, "save"),
        ):
            result = runner.invoke(
                cli, ["config", "set-password", "prod"], input="s3cret\ns3cret\n"
            )

        assert result.exit_code == 0
        assert cfg.profiles["prod"].password is None
        assert "password" not in cfg.profiles["prod"].to_dict()

    def test_password_is_never_echoed(self, runner: CliRunner) -> None:
        with (
            core_config_seam(self._config()),
            patch.dict("sys.modules", {"keyring": MagicMock()}),
            patch.object(Config, "save"),
        ):
            result = runner.invoke(
                cli, ["config", "set-password", "prod"], input="s3cret\ns3cret\n"
            )

        assert "s3cret" not in result.output

    def test_unknown_profile_fails(self, runner: CliRunner) -> None:
        with core_config_seam(self._config()):
            result = runner.invoke(cli, ["config", "set-password", "nope"])

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_missing_keyring_package_reports_the_install_hint(self, runner: CliRunner) -> None:
        with core_config_seam(self._config()), patch.dict("sys.modules", {"keyring": None}):
            result = runner.invoke(cli, ["config", "set-password", "prod"])

        assert result.exit_code == 1
        # Rich colourises inside the message, so compare on the plain text.
        assert "xnatctl[keyring]" in _strip_ansi(result.output)

    def test_keychain_write_failure_does_not_rewrite_the_config(self, runner: CliRunner) -> None:
        """A failed keychain write must not leave the profile claiming its
        password lives somewhere it does not.
        """
        cfg = self._config()
        fake_keyring = MagicMock()
        fake_keyring.set_password.side_effect = RuntimeError("no backend available")

        with (
            core_config_seam(cfg),
            patch.dict("sys.modules", {"keyring": fake_keyring}),
            patch.object(Config, "save") as save,
        ):
            result = runner.invoke(
                cli, ["config", "set-password", "prod"], input="s3cret\ns3cret\n"
            )

        assert result.exit_code == 1
        save.assert_not_called()
        assert cfg.profiles["prod"].password_source is None

    def test_password_is_never_accepted_as_an_argument(self, runner: CliRunner) -> None:
        """Prompt-only by design, so it cannot reach shell history or the
        process table (the remaining argv password flags are likewise refused).
        """
        with core_config_seam(self._config()):
            result = runner.invoke(cli, ["config", "set-password", "prod", "--password", "s3cret"])

        assert result.exit_code != 0
        assert "no such option" in result.output.lower()


class TestConfigInitGuidedLogin:
    """`config init` continues into a login.

    Onboarding used to be two commands with a cliff between them: init wrote a
    profile and stopped, and the natural next command failed with a
    profile-not-found error that pointed nowhere.
    """

    def test_login_is_invoked_with_the_new_profile(self, runner: CliRunner, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        login = MagicMock(return_value={"status": "authenticated", "username": "admin"})

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
            patch("xnatctl.cli.auth.do_login", login),
            patch("xnatctl.cli.auth.report_login"),
        ):
            result = runner.invoke(
                cli,
                ["config", "init", "--url", "https://xnat.example.org", "--login"],
            )

        assert result.exit_code == 0
        login.assert_called_once()
        assert login.call_args.kwargs["profile_name"] == "default"
        assert login.call_args.args[1].url == "https://xnat.example.org"

    def test_no_login_skips_and_says_what_to_run(self, runner: CliRunner, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        login = MagicMock()

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
            patch("xnatctl.cli.auth.do_login", login),
        ):
            result = runner.invoke(
                cli,
                ["config", "init", "--url", "https://xnat.example.org", "--no-login"],
            )

        assert result.exit_code == 0
        login.assert_not_called()
        assert "auth login" in result.output

    def test_non_tty_does_not_prompt(self, runner: CliRunner, tmp_path: Path) -> None:
        """An unanswered prompt in a pipeline is a hang, so the prompt only
        appears on a real terminal.
        """
        config_file = tmp_path / "config.yaml"
        login = MagicMock()

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
            patch("xnatctl.cli.config_cmd.sys.stdin.isatty", return_value=False),
            patch("xnatctl.cli.auth.do_login", login),
        ):
            result = runner.invoke(cli, ["config", "init", "--url", "https://xnat.example.org"])

        assert result.exit_code == 0
        login.assert_not_called()

    def test_failed_login_keeps_the_profile_and_exits_zero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The profile was written and is valid; only the login failed. Saying
        `config init` failed would be wrong, and would invite a rerun.
        """
        from xnatctl.core.exceptions import AuthenticationError

        config_file = tmp_path / "config.yaml"

        with (
            patch("xnatctl.cli.config_cmd.CONFIG_FILE", config_file),
            patch("xnatctl.core.config.CONFIG_FILE", config_file),
            patch(
                "xnatctl.cli.auth.do_login",
                side_effect=AuthenticationError("https://xnat.example.org", "bad password"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["config", "init", "--url", "https://xnat.example.org", "--login"],
            )

        assert result.exit_code == 0
        assert config_file.exists(), "the profile must survive a failed login"
        assert "Profile saved" in result.output
        assert "auth login" in result.output
