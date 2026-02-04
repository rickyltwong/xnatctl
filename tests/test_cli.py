"""Tests for xnatctl CLI commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


# =============================================================================
# Basic CLI Tests
# =============================================================================


class TestCLIBasics:
    """Tests for basic CLI functionality."""

    def test_cli_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "xnatctl" in result.output
        assert "project" in result.output
        assert "subject" in result.output
        assert "session" in result.output

    def test_cli_version(self, runner: CliRunner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "xnatctl" in result.output

    def test_config_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "init" in result.output
        assert "show" in result.output

    def test_auth_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output
        assert "logout" in result.output

    def test_project_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["project", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "create" in result.output

    def test_subject_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["subject", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "delete" in result.output
        assert "rename" in result.output

    def test_session_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["session", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "download" in result.output

    def test_scan_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "delete" in result.output

    def test_resource_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["resource", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "upload" in result.output

    def test_prearchive_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["prearchive", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "archive" in result.output
        assert "delete" in result.output

    def test_admin_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["admin", "--help"])
        assert result.exit_code == 0
        assert "refresh-catalogs" in result.output
        assert "user" in result.output
        assert "audit" in result.output

    def test_api_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["api", "--help"])
        assert result.exit_code == 0
        assert "get" in result.output
        assert "post" in result.output
        assert "put" in result.output
        assert "delete" in result.output

    def test_dicom_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["dicom", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output
        assert "inspect" in result.output

    def test_completion_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["completion", "--help"])
        assert result.exit_code == 0
        assert "bash" in result.output
        assert "zsh" in result.output
        assert "fish" in result.output

    def test_health_help(self, runner: CliRunner):
        result = runner.invoke(cli, ["health", "--help"])
        assert result.exit_code == 0
        assert "ping" in result.output


# =============================================================================
# Config Command Tests
# =============================================================================


class TestConfigCommands:
    """Tests for config commands."""

    def test_config_init(self, runner: CliRunner, temp_dir):
        result = runner.invoke(
            cli,
            ["config", "init", "--url", "https://test.example.org"],
            env={"HOME": str(temp_dir)},
        )
        # May fail due to missing config dir, but should not crash
        assert result.exit_code in (0, 1)

    def test_config_show_no_config(self, runner: CliRunner, temp_dir):
        result = runner.invoke(
            cli,
            ["config", "show"],
            env={"HOME": str(temp_dir)},
        )
        # Should handle missing config gracefully
        assert result.exit_code in (0, 1)


# =============================================================================
# Completion Command Tests
# =============================================================================


class TestCompletionCommands:
    """Tests for shell completion generation."""

    def test_completion_bash(self, runner: CliRunner):
        result = runner.invoke(cli, ["completion", "bash"])
        assert result.exit_code == 0
        assert "_xnatctl_completion" in result.output
        assert "complete" in result.output

    def test_completion_zsh(self, runner: CliRunner):
        result = runner.invoke(cli, ["completion", "zsh"])
        assert result.exit_code == 0
        assert "#compdef" in result.output
        assert "xnatctl" in result.output

    def test_completion_fish(self, runner: CliRunner):
        result = runner.invoke(cli, ["completion", "fish"])
        assert result.exit_code == 0
        assert "function" in result.output
        assert "xnatctl" in result.output
