"""Tests for xnatctl CLI prearchive commands."""

from __future__ import annotations

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
    """Build a mock Config with a default profile."""
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="testuser",
                password="testpass",
                verify_ssl=False,
            )
        },
    )


def _mock_client() -> MagicMock:
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}
    return client


class TestPrearchiveList:
    """Tests for prearchive list command."""

    def test_prearchive_list(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {
                "project": "PROJ1",
                "timestamp": "20240115_120000",
                "name": "Session1",
                "status": "Ready",
                "scan_date": "2024-01-15",
                "subject": "SUB001",
            },
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["prearchive", "list"])

        assert result.exit_code == 0

    def test_prearchive_list_with_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = []

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["prearchive", "list", "--project", "PROJ1"]
                        )

        assert result.exit_code == 0
        mock_service.list.assert_called_once_with(project="PROJ1")

    def test_prearchive_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {
                "project": "PROJ1",
                "timestamp": "20240115_120000",
                "name": "Session1",
                "status": "Ready",
                "scan_date": "2024-01-15",
                "subject": "SUB001",
            },
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["prearchive", "list", "--quiet"]
                        )

        assert result.exit_code == 0
        assert "PROJ1/20240115_120000/Session1" in result.output


class TestPrearchiveArchive:
    """Tests for prearchive archive command."""

    def test_archive_success(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.archive.return_value = {
            "success": True,
            "project": "PROJ1",
            "session": "Session1",
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "archive",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                            ],
                        )

        assert result.exit_code == 0
        assert "Archived" in result.output

    def test_archive_with_options(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.archive.return_value = {"success": True}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "archive",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                                "--subject",
                                "SUB001",
                                "--label",
                                "NewLabel",
                                "--overwrite",
                            ],
                        )

        assert result.exit_code == 0
        mock_service.archive.assert_called_once_with(
            project="PROJ1",
            timestamp="20240115_120000",
            session_name="Session1",
            subject="SUB001",
            experiment_label="NewLabel",
            overwrite=True,
        )

    def test_archive_json_output(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.archive.return_value = {
            "success": True,
            "project": "PROJ1",
            "session": "Session1",
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "archive",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                                "--output",
                                "json",
                            ],
                        )

        assert result.exit_code == 0
        assert "success" in result.output


class TestPrearchiveDelete:
    """Tests for prearchive delete command."""

    def test_delete_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "delete",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                                "--yes",
                            ],
                        )

        assert result.exit_code == 0
        assert "Deleted" in result.output
        mock_service.delete.assert_called_once()

    def test_delete_aborted(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "delete",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                            ],
                            input="n\n",
                        )

        assert result.exit_code != 0
        mock_service.delete.assert_not_called()


class TestPrearchiveRebuild:
    """Tests for prearchive rebuild command."""

    def test_rebuild_success(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "rebuild",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                            ],
                        )

        assert result.exit_code == 0
        assert "Rebuilt" in result.output


class TestPrearchiveMove:
    """Tests for prearchive move command."""

    def test_move_success(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "move",
                                "PROJ1",
                                "20240115_120000",
                                "Session1",
                                "PROJ2",
                            ],
                        )

        assert result.exit_code == 0
        assert "Moved" in result.output
        assert "PROJ2" in result.output
