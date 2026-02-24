"""Tests for xnatctl CLI admin commands."""

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
                username="admin",
                password="adminpass",
                verify_ssl=False,
            )
        },
    )


def _mock_client() -> MagicMock:
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "admin"}
    return client


class TestAdminRefreshCatalogs:
    """Tests for admin refresh-catalogs command."""

    def test_refresh_catalogs_basic(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                    {"ID": "XNAT_E002", "subject_ID": "XNAT_S002", "label": "Sess2"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["admin", "refresh-catalogs", "TESTPROJ", "--no-parallel"],
                    )

        assert result.exit_code == 0
        assert "Refreshed" in result.output

    def test_refresh_catalogs_with_options(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--option",
                            "checksum",
                            "--option",
                            "delete",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0

    def test_refresh_catalogs_no_experiments(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "refresh-catalogs", "EMPTYPROJ"]
                    )

        assert result.exit_code == 0
        assert "No experiments" in result.output

    def test_refresh_catalogs_with_limit(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_E00{i}", "subject_ID": f"XNAT_S00{i}", "label": f"S{i}"}
                    for i in range(5)
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--limit",
                            "2",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0
        assert client.post.call_count == 2

    def test_refresh_catalogs_json_output(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--output",
                            "json",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0
        assert "refreshed" in result.output


class TestAdminUserAdd:
    """Tests for admin user add command."""

    def test_add_user_to_groups(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "PROJ1_member",
                            "PROJ2_owner",
                        ],
                    )

        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_user_to_groups_from_projects(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "EXTRA_group",
                            "--projects",
                            "PROJ1,PROJ2",
                            "--role",
                            "collaborator",
                        ],
                    )

        assert result.exit_code == 0
        call_args = client.put.call_args
        groups_sent = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "PROJ1_collaborator" in groups_sent
        assert "PROJ2_collaborator" in groups_sent
        assert "EXTRA_group" in groups_sent

    def test_add_user_to_groups_failure(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal server error"
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "PROJ1_member",
                        ],
                    )

        assert result.exit_code != 0


class TestAdminAudit:
    """Tests for admin audit command."""

    def test_audit_list(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [
            {
                "timestamp": "2024-01-15T10:00:00",
                "user": "admin",
                "action": "create",
                "resource": "/data/projects/PROJ1",
                "project": "PROJ1",
            },
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit", "--limit", "10"])

        assert result.exit_code == 0

    def test_audit_no_entries(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = []

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit"])

        assert result.exit_code == 0
        assert "No audit entries" in result.output

    def test_audit_api_unavailable(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = Exception("404 Not Found")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit"])

        assert result.exit_code != 0
