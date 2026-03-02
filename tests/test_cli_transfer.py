"""Tests for project transfer CLI commands."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.project import project


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestTransferInit:
    def test_generates_config(self, runner: CliRunner, tmp_path) -> None:
        output_path = tmp_path / "transfer.yaml"
        with patch("xnatctl.models.transfer.TransferConfig.scaffold") as mock_scaffold:
            mock_scaffold.return_value = "source_project: SRC\n"
            result = runner.invoke(
                project,
                [
                    "transfer-init",
                    "-P",
                    "SRC",
                    "--dest-project",
                    "DST",
                    "--output-file",
                    str(output_path),
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0


class TestTransferCheck:
    def test_check_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-check", "--help"])
        assert result.exit_code == 0
        assert "transfer-check" in result.output.lower() or "pre-flight" in result.output.lower()


class TestTransferHistory:
    def test_history_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-history", "--help"])
        assert result.exit_code == 0


class TestTransferStatus:
    def test_status_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-status", "--help"])
        assert result.exit_code == 0


class TestTransfer:
    def test_transfer_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer", "--help"])
        assert result.exit_code == 0
        assert "--dest-profile" in result.output
        assert "--dry-run" in result.output
