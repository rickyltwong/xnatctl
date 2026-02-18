"""Tests for xnatctl CLI api commands."""

from __future__ import annotations

import json
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


class TestApiGet:
    """Tests for api get command."""

    def test_api_get_json_response(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "PROJ1", "name": "Project One"},
                    {"ID": "PROJ2", "name": "Project Two"},
                ]
            }
        }
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["api", "get", "/data/projects"]
                    )

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_api_get_with_params(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ResultSet": {"Result": []}}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "get",
                            "/data/projects",
                            "-P",
                            "columns=ID,name",
                        ],
                    )

        assert result.exit_code == 0
        call_args = client.get.call_args
        assert call_args[1]["params"]["columns"] == "ID,name"

    def test_api_get_json_output_format(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "value"}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["api", "get", "/some/path", "-o", "json"]
                    )

        assert result.exit_code == 0
        assert "key" in result.output

    def test_api_get_non_json_response(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = "plain text response"
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["api", "get", "/some/text/endpoint"]
                    )

        assert result.exit_code == 0
        assert "plain text response" in result.output


class TestApiPost:
    """Tests for api post command."""

    def test_api_post_with_data(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "created"}
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/projects",
                            "--data",
                            '{"ID": "NEWPROJ"}',
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"ID": "NEWPROJ"}

    def test_api_post_with_file(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        client.post.return_value = mock_resp

        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"key": "value"}))

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/endpoint",
                            "--file",
                            str(payload),
                        ],
                    )

        assert result.exit_code == 0

    def test_api_post_non_json_data(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/endpoint",
                            "--data",
                            "plain text body",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["data"] == "plain text body"
        assert call_kwargs["json"] is None


class TestApiPut:
    """Tests for api put command."""

    def test_api_put_with_data(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "updated"}
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/projects/PROJ1",
                            "--data",
                            '{"description": "Updated"}',
                        ],
                    )

        assert result.exit_code == 0


class TestApiDelete:
    """Tests for api delete command."""

    def test_api_delete_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.delete.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "delete",
                            "/data/projects/PROJ1/subjects/SUB001",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_api_delete_aborted(self, runner: CliRunner) -> None:
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "delete", "/data/projects/PROJ1"],
                        input="n\n",
                    )

        assert result.exit_code != 0
        client.delete.assert_not_called()

    def test_api_delete_with_params(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        client.delete.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "delete",
                            "/data/resource",
                            "-P",
                            "removeFiles=true",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        call_args = client.delete.call_args
        assert call_args[1]["params"]["removeFiles"] == "true"
