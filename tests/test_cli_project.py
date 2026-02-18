"""Tests for xnatctl CLI project commands."""

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
    cfg = Config(
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
    return cfg



class TestProjectList:
    """Tests for project list command."""

    def test_project_list_table(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "Smith",
                        "description": "Test project",
                    },
                    {
                        "ID": "PROJ2",
                        "name": "Project Two",
                        "pi_lastname": "Jones",
                        "description": "",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_json(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "Smith",
                        "description": "Test project",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(
                        cli, ["project", "list", "--output", "json"]
                    )

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_quiet(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "",
                        "description": "",
                    },
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(cli, ["project", "list", "--quiet"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_empty(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0


class TestProjectShow:
    """Tests for project show command."""

    def test_project_show(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "PROJ1",
                            "name": "Project One",
                            "secondary_ID": "",
                            "pi_lastname": "Smith",
                            "description": "A test project",
                            "accessibility": "private",
                        }
                    ]
                }
            },
            {"ResultSet": {"Result": [{"ID": "SUB1"}, {"ID": "SUB2"}]}},
            {"ResultSet": {"Result": [{"ID": "EXP1"}]}},
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(cli, ["project", "show", "PROJ1"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_show_not_found(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(cli, ["project", "show", "NONEXIST"])

        assert result.exit_code != 0


class TestProjectCreate:
    """Tests for project create command."""

    def test_project_create_success(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "create",
                            "NEWPROJ",
                            "--name",
                            "New Project",
                            "--pi",
                            "Smith",
                        ],
                    )

        assert result.exit_code == 0
        assert "NEWPROJ" in result.output

    def test_project_create_failure(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Project already exists"
        mock_client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(
                        cli, ["project", "create", "EXISTING"]
                    )

        assert result.exit_code != 0

    def test_project_create_with_description(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=mock_client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "create",
                            "NEWPROJ",
                            "--description",
                            "A new project",
                            "--accessibility",
                            "protected",
                        ],
                    )

        assert result.exit_code == 0
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "accessibility" in str(call_kwargs)
