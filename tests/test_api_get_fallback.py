"""Tests for `api get -o json` non-JSON warn-and-passthrough (issue #13)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner that splits stdout and stderr."""
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


def _patched_invoke(runner: CliRunner, client: MagicMock, args: list[str]):
    """Invoke the CLI with the mock client + config patched in."""
    with core_config_seam(_mock_config()):
        with config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                return runner.invoke(cli, args)


class TestApiGetJsonFallback:
    """`-o json` against a non-JSON response warns and passes the body through."""

    def test_non_json_text_response_warns_and_passes_through(self, runner: CliRunner) -> None:
        """Plain text body under -o json -> stderr warning, stdout body, exit 0."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError("Not JSON", "plain text response", 0)
        mock_resp.text = "raw plain-text body"
        mock_resp.headers = {"content-type": "text/plain"}
        client.get.return_value = mock_resp

        result = _patched_invoke(
            runner, client, ["api", "get", "/xapi/xsync/progress/PROJ", "-o", "json"]
        )

        assert result.exit_code == 0
        assert "raw plain-text body" in result.stdout
        # One-line stderr warning identifying the path and the policy.
        assert "Warning" in result.stderr
        assert "not JSON" in result.stderr

    def test_non_json_binary_response_passes_raw_bytes(self, runner: CliRunner) -> None:
        """Binary body under -o json -> raw bytes on stdout, no decode corruption."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError("Not JSON", "plain text response", 0)
        # 0x80-0xFF are invalid UTF-8 and would be lost in a text round-trip.
        binary_data = bytes(range(256))
        mock_resp.content = binary_data
        mock_resp.headers = {"content-type": "application/octet-stream"}
        client.get.return_value = mock_resp

        result = _patched_invoke(runner, client, ["api", "get", "/some/file.bin", "-o", "json"])

        assert result.exit_code == 0
        assert binary_data in result.stdout_bytes
        assert "Warning" in result.stderr

    def test_json_response_under_o_json_unchanged(self, runner: CliRunner) -> None:
        """True JSON endpoint must keep the existing JSON behavior."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"alpha": 1, "beta": [2, 3]}
        client.get.return_value = mock_resp

        result = _patched_invoke(runner, client, ["api", "get", "/data/some/json", "-o", "json"])

        assert result.exit_code == 0
        # No stderr warning when the response actually was JSON.
        assert "Warning" not in result.stderr
        assert '"alpha"' in result.stdout
        assert '"beta"' in result.stdout

    def test_non_json_under_table_format_still_works(self, runner: CliRunner) -> None:
        """The fallback policy only fires under -o json; table format keeps prior behavior."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError("Not JSON", "plain text response", 0)
        mock_resp.text = "text body for table mode"
        mock_resp.headers = {"content-type": "text/plain"}
        client.get.return_value = mock_resp

        result = _patched_invoke(runner, client, ["api", "get", "/some/text/endpoint"])

        assert result.exit_code == 0
        assert "text body for table mode" in result.stdout
        # No warning needed when the user didn't ask for JSON.
        assert "Warning" not in result.stderr
