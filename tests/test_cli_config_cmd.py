"""Tests for xnatctl CLI config commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


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

    def test_config_init_existing_profile_no_force(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
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
            result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code != 0


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
                result = runner.invoke(
                    cli, ["config", "remove-profile", "dev", "--yes"]
                )

        assert result.exit_code == 0
        assert "removed" in result.output

    def test_remove_profile_not_found(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(
                cli, ["config", "remove-profile", "nonexist", "--yes"]
            )

        assert result.exit_code != 0

    def test_remove_default_profile(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(
                cli, ["config", "remove-profile", "default", "--yes"]
            )

        assert result.exit_code != 0

    def test_remove_profile_abort(self, runner: CliRunner) -> None:
        cfg = _mock_config()

        with patch("xnatctl.cli.config_cmd.Config.load", return_value=cfg):
            result = runner.invoke(
                cli, ["config", "remove-profile", "dev"], input="n\n"
            )

        assert result.exit_code != 0
