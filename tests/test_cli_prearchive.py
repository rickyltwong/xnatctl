"""Tests for xnatctl CLI prearchive commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import AuthenticatedCLI, config_seam, core_config_seam, make_response

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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["prearchive", "list", "--project", "PROJ1"])

        assert result.exit_code == 0
        mock_service.list.assert_called_once_with(project="PROJ1")

    def test_prearchive_list_filter_and_sort(self, runner: CliRunner) -> None:
        """--filter and --sort-by narrow AND order the prearchive listing,
        client-side.

        Rows are deliberately NOT in scan_date order: if --sort-by were
        ignored, the surviving (filtered) rows would print in their
        original relative order (SessionB, SessionA, SessionC) instead of
        date order (SessionA, SessionC, SessionB), so a bypassed --sort-by
        would be caught, not just a bypassed --filter.
        """
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {
                "project": "PROJ1",
                "timestamp": "20240120_120000",
                "name": "SessionB",
                "status": "Ready",
                "scan_date": "2024-01-20",
                "subject": "SUB002",
            },
            {
                "project": "PROJ1",
                "timestamp": "20240116_120000",
                "name": "SessionErr",
                "status": "Error",
                "scan_date": "2024-01-16",
                "subject": "SUB003",
            },
            {
                "project": "PROJ1",
                "timestamp": "20240110_120000",
                "name": "SessionA",
                "status": "Ready",
                "scan_date": "2024-01-10",
                "subject": "SUB001",
            },
            {
                "project": "PROJ1",
                "timestamp": "20240115_120000",
                "name": "SessionC",
                "status": "Ready",
                "scan_date": "2024-01-15",
                "subject": "SUB004",
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "prearchive",
                                "list",
                                "--filter",
                                "status:Ready",
                                "--sort-by",
                                "scan_date",
                                "--quiet",
                            ],
                        )

        assert result.exit_code == 0
        # A disabled-TLS warning (verify_ssl=False in _mock_config) shares
        # stdout with quiet output in CliRunner's merged output; only the
        # "PROJ1/<timestamp>/<name>" lines are this command's actual output.
        names_in_order = [
            line.split("/")[-1] for line in result.output.splitlines() if line.startswith("PROJ1/")
        ]
        assert names_in_order == ["SessionA", "SessionC", "SessionB"]
        assert "SessionErr" not in result.output

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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.prearchive.PrearchiveService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["prearchive", "list", "--quiet"])

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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
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


class TestPrearchiveSettings:
    """Tests for `prearchive settings` -- get/set, dry-run, declined confirmation, 403."""

    def test_get_shows_recognized_mode(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text="4", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["prearchive", "settings", "-P", "PROJ1"])

        assert result.exit_code == 0
        authenticated_cli.client.get.assert_called_once_with("/data/projects/PROJ1/prearchive_code")
        assert "auto-archive" in result.output

    def test_get_falls_back_to_profile_default_project(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text="0", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["prearchive", "settings"])

        assert result.exit_code == 0
        # authenticated_cli's default profile carries default_project=TESTPROJ.
        authenticated_cli.client.get.assert_called_once_with(
            "/data/projects/TESTPROJ/prearchive_code"
        )

    def test_get_unrecognized_code_reported_not_crashed(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get.return_value = make_response(
            text="9", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["prearchive", "settings", "-P", "PROJ1"])

        assert result.exit_code == 0
        assert "not a recognized mode" in result.output
        assert "9" in result.output

    def test_set_typo_rejected_client_side(self, authenticated_cli: AuthenticatedCLI) -> None:
        """The server accepts (and stores) any integer silently (verified live)
        -- a typo'd --set value must never reach the network.
        """
        result = authenticated_cli.invoke(
            ["prearchive", "settings", "-P", "PROJ1", "--set", "bogus-mode", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_sends_mapped_code(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.put.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(
            ["prearchive", "settings", "-P", "PROJ1", "--set", "auto-archive-overwrite", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/projects/PROJ1/prearchive_code/5"
        )

    def test_set_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["prearchive", "settings", "-P", "PROJ1", "--set", "auto-archive", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.get.assert_not_called()
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_set_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["prearchive", "settings", "-P", "PROJ1", "--set", "auto-archive"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_403_reports_site_policy_not_permission_denied(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        from xnatctl.core.exceptions import PermissionDeniedError

        authenticated_cli.client.put.side_effect = PermissionDeniedError("prearchive_code", "set")

        result = authenticated_cli.invoke(
            ["prearchive", "settings", "-P", "PROJ1", "--set", "auto-archive", "--yes"]
        )

        assert result.exit_code != 0
        assert "site policy" in result.output
