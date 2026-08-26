"""Tests for project transfer CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import AuthenticatedCLI

from xnatctl.cli.project import project
from xnatctl.models.info import ServerInfo, UserInfo
from xnatctl.services.transfer.orchestrator import TransferResult


def _server_info() -> ServerInfo:
    """A healthy ping result for transfer-check mocks."""
    return ServerInfo(url="https://xnat.example.org", status="ok", version="1.8.5", latency_ms=5)


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
        assert "Config written to" in result.output
        assert output_path.read_text() == "source_project: SRC\n"

    def test_prints_to_stdout_without_output_file(self, runner: CliRunner) -> None:
        with patch("xnatctl.models.transfer.TransferConfig.scaffold") as mock_scaffold:
            mock_scaffold.return_value = "source_project: SRC\ndest_project: DST\n"
            result = runner.invoke(
                project,
                ["transfer-init", "-P", "SRC", "--dest-project", "DST"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "source_project: SRC" in result.output


class TestTransferCheck:
    def test_check_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-check", "--help"])
        assert result.exit_code == 0
        assert "transfer-check" in result.output.lower() or "pre-flight" in result.output.lower()

    def test_check_all_ok(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.ping.return_value = _server_info()
        authenticated_cli.client.whoami.return_value = UserInfo(username="srcuser", enabled=True)

        dest_client = MagicMock()
        dest_client.ping.return_value = _server_info()
        dest_client.whoami.return_value = UserInfo(username="destuser", enabled=True)

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            result = authenticated_cli.invoke(
                [
                    "project",
                    "transfer-check",
                    "-P",
                    "SRC",
                    "--dest-project",
                    "DST",
                    "--dest-profile",
                    "staging",
                ]
            )

        assert result.exit_code == 0
        assert "OK" in result.output
        dest_client.authenticate.assert_called_once()
        dest_client.close.assert_called_once()

    def test_check_reports_failures_and_exits_nonzero(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.ping.side_effect = RuntimeError("unreachable")
        authenticated_cli.client.whoami.return_value = UserInfo(username="srcuser", enabled=True)

        dest_client = MagicMock()
        dest_client.authenticate.side_effect = RuntimeError("bad creds")

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            result = authenticated_cli.invoke(
                [
                    "project",
                    "transfer-check",
                    "-P",
                    "SRC",
                    "--dest-project",
                    "DST",
                    "--dest-profile",
                    "staging",
                ]
            )

        assert result.exit_code != 0
        assert "FAIL" in result.output


class TestTransferStatus:
    def test_status_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-status", "--help"])
        assert result.exit_code == 0

    def test_status_no_history_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        with patch("xnatctl.core.config.CONFIG_DIR", tmp_path / "nonexistent"):
            result = authenticated_cli.invoke(["project", "transfer-status", "-P", "SRC"])

        assert result.exit_code != 0
        assert "No transfer history found" in result.output

    def test_status_no_transfers_for_project(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        (tmp_path / "transfer.db").touch()
        mock_store = MagicMock()
        mock_store.get_sync_history.return_value = []

        with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
            with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                result = authenticated_cli.invoke(["project", "transfer-status", "-P", "SRC"])

        assert result.exit_code != 0
        assert "No transfers found" in result.output
        mock_store.close.assert_called_once()

    def test_status_shows_last_sync(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        (tmp_path / "transfer.db").touch()
        mock_store = MagicMock()
        mock_store.get_sync_history.return_value = [
            {
                "id": 7,
                "status": "completed",
                "sync_start": "2026-08-01T00:00:00",
                "sync_end": "2026-08-01T00:05:00",
                "subjects_synced": 3,
                "subjects_failed": 0,
                "subjects_skipped": 1,
                "dest_url": "https://dest.example.org",
                "dest_project": "DST",
            }
        ]

        with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
            with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                result = authenticated_cli.invoke(["project", "transfer-status", "-P", "SRC"])

        assert result.exit_code == 0
        assert "completed" in result.output
        assert "DST" in result.output
        mock_store.close.assert_called_once()


class TestTransferHistory:
    def test_history_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer-history", "--help"])
        assert result.exit_code == 0

    def test_history_no_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        with patch("xnatctl.core.config.CONFIG_DIR", tmp_path / "nonexistent"):
            result = authenticated_cli.invoke(["project", "transfer-history", "-P", "SRC"])

        assert result.exit_code != 0
        assert "No transfer history found" in result.output

    def test_history_lists_runs(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        (tmp_path / "transfer.db").touch()
        mock_store = MagicMock()
        mock_store.get_sync_history.return_value = [
            {
                "id": 2,
                "status": "completed",
                "sync_start": "2026-08-02T00:00:00Z",
                "dest_url": "https://dest.example.org",
                "subjects_synced": 5,
                "subjects_failed": 0,
            },
            {
                "id": 1,
                "status": "failed",
                "sync_start": "2026-08-01T00:00:00Z",
                "dest_url": "https://dest.example.org",
                "subjects_synced": 2,
                "subjects_failed": 1,
            },
        ]

        with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
            with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                result = authenticated_cli.invoke(
                    ["project", "transfer-history", "-P", "SRC", "-o", "json"]
                )

        assert result.exit_code == 0
        assert '"id": 2' in result.output
        assert '"id": 1' in result.output
        mock_store.close.assert_called_once()


class TestTransfer:
    def test_transfer_command_exists(self, runner: CliRunner) -> None:
        result = runner.invoke(project, ["transfer", "--help"])
        assert result.exit_code == 0
        assert "--dest-profile" in result.output
        assert "--dry-run" in result.output

    def test_transfer_success(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        dest_client = MagicMock()
        dest_client.base_url = "https://dest.example.org"
        mock_store = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = TransferResult(
            subjects_synced=4,
            subjects_failed=0,
            subjects_skipped=1,
            experiments_synced=6,
            success=True,
        )

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
                with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                    with patch(
                        "xnatctl.services.transfer.orchestrator.TransferOrchestrator",
                        return_value=mock_orchestrator,
                    ) as mock_orch_cls:
                        result = authenticated_cli.invoke(
                            [
                                "project",
                                "transfer",
                                "-P",
                                "SRC",
                                "--dest-project",
                                "DST",
                                "--dest-profile",
                                "staging",
                                "--yes",
                            ]
                        )

        assert result.exit_code == 0
        assert "Subjects Synced" in result.output and "4" in result.output
        mock_orch_cls.assert_called_once()
        dest_client.authenticate.assert_called_once()
        mock_orchestrator.run.assert_called_once()
        assert mock_orchestrator.run.call_args.kwargs["dry_run"] is False
        mock_store.close.assert_called_once()
        dest_client.close.assert_called_once()

    def test_transfer_dry_run_no_confirmation_needed(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        dest_client = MagicMock()
        dest_client.base_url = "https://dest.example.org"
        mock_store = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = TransferResult(success=True, dry_run=True)

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
                with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                    with patch(
                        "xnatctl.services.transfer.orchestrator.TransferOrchestrator",
                        return_value=mock_orchestrator,
                    ):
                        result = authenticated_cli.invoke(
                            [
                                "project",
                                "transfer",
                                "-P",
                                "SRC",
                                "--dest-project",
                                "DST",
                                "--dest-profile",
                                "staging",
                                "--dry-run",
                            ]
                        )

        assert result.exit_code == 0
        assert mock_orchestrator.run.call_args.kwargs["dry_run"] is True

    def test_transfer_failure_exits_nonzero(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        dest_client = MagicMock()
        dest_client.base_url = "https://dest.example.org"
        mock_store = MagicMock()
        mock_orchestrator = MagicMock()
        mock_orchestrator.run.return_value = TransferResult(
            success=False, errors=["subject SUB001: network error"]
        )

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
                with patch("xnatctl.core.state.TransferStateStore", return_value=mock_store):
                    with patch(
                        "xnatctl.services.transfer.orchestrator.TransferOrchestrator",
                        return_value=mock_orchestrator,
                    ):
                        result = authenticated_cli.invoke(
                            [
                                "project",
                                "transfer",
                                "-P",
                                "SRC",
                                "--dest-project",
                                "DST",
                                "--dest-profile",
                                "staging",
                                "--yes",
                            ]
                        )

        assert result.exit_code != 0
        assert "network error" in result.output
        mock_store.close.assert_called_once()
        dest_client.close.assert_called_once()

    def test_transfer_prompt_abort_no_orchestrator_run(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        dest_client = MagicMock()

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client):
            with patch("xnatctl.core.config.CONFIG_DIR", tmp_path):
                with patch(
                    "xnatctl.services.transfer.orchestrator.TransferOrchestrator"
                ) as mock_orch_cls:
                    result = authenticated_cli.invoke(
                        [
                            "project",
                            "transfer",
                            "-P",
                            "SRC",
                            "--dest-project",
                            "DST",
                            "--dest-profile",
                            "staging",
                        ],
                        input="n\n",
                    )

        assert result.exit_code != 0
        mock_orch_cls.assert_not_called()
