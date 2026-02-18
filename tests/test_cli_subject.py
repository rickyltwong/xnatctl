"""Tests for xnatctl CLI subject commands."""

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
    """Build a mock Config with a default profile including default_project."""
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="testuser",
                password="testpass",
                verify_ssl=False,
                default_project="TESTPROJ",
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



class TestSubjectList:
    """Tests for subject list command."""

    def test_subject_list_with_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                        {"ID": "XNAT_S002", "label": "SUB002", "src": ""},
                    ]
                }
            },
            {"ResultSet": {"Result": [{"ID": "EXP1"}]}},
            {"ResultSet": {"Result": [{"ID": "EXP2"}, {"ID": "EXP3"}]}},
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli, ["subject", "list", "--project", "TESTPROJ"]
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_list_uses_default_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [{"ID": "XNAT_S001", "label": "SUB001", "src": ""}]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(cli, ["subject", "list"])

        assert result.exit_code == 0

    def test_subject_list_no_project_no_default(self, runner: CliRunner) -> None:
        client = _mock_client()
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

        with patch("xnatctl.core.config.Config.load", return_value=cfg):
            with patch("xnatctl.cli.common.Config.load", return_value=cfg):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(cli, ["subject", "list"])

        assert result.exit_code != 0
        assert "Project required" in result.output or "Project required" in result.stderr

    def test_subject_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                    {"ID": "XNAT_S002", "label": "SUB002", "src": ""},
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli, ["subject", "list", "-P", "TESTPROJ", "--quiet"]
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_list_with_filter(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                    {"ID": "XNAT_S002", "label": "CTL001", "src": ""},
                ]
            }
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "list",
                            "-P",
                            "TESTPROJ",
                            "--filter",
                            "label:SUB*",
                            "--quiet",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output
        assert "CTL001" not in result.output


class TestSubjectShow:
    """Tests for subject show command."""

    def test_subject_show(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "XNAT_S001", "label": "SUB001"}
                    ]
                }
            },
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "EXP1", "label": "SESSION1"},
                    ]
                }
            },
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli, ["subject", "show", "SUB001", "-P", "TESTPROJ"]
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_show_not_found(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli, ["subject", "show", "NOSUB", "-P", "TESTPROJ"]
                    )

        assert result.exit_code != 0


class TestSubjectDelete:
    """Tests for subject delete command."""

    def test_subject_delete_dry_run(self, runner: CliRunner) -> None:
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "Would delete" in result.output
        client.delete.assert_not_called()

    def test_subject_delete_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.delete.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        assert "Deleted" in result.output
        client.delete.assert_called_once()

    def test_subject_delete_failure(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server error"
        client.delete.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch(
                    "xnatctl.cli.common.XNATClient", return_value=client
                ):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--yes",
                        ],
                    )

        assert result.exit_code != 0
