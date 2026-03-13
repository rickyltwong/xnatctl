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


class TestBuildQueryString:
    """Tests for _build_query_string helper."""

    def test_empty_params(self) -> None:
        from xnatctl.cli.api import _build_query_string

        assert _build_query_string(()) == ""

    def test_simple_params(self) -> None:
        from xnatctl.cli.api import _build_query_string

        result = _build_query_string(("columns=ID,label", "format=json"))
        assert "columns=" in result
        assert "format=json" in result

    def test_xsi_colon_preserved(self) -> None:
        from xnatctl.cli.api import _build_query_string

        result = _build_query_string(("xsiType=xnat:mrSessionData",))
        assert "xsiType=xnat:mrSessionData" in result
        assert "%3A" not in result

    def test_xsi_slash_preserved(self) -> None:
        from xnatctl.cli.api import _build_query_string

        result = _build_query_string(("xnat:experimentData/subject_ID=XNAT_S00001",))
        assert "xnat:experimentData/subject_ID=XNAT_S00001" in result

    def test_brackets_preserved(self) -> None:
        from xnatctl.cli.api import _build_query_string

        result = _build_query_string(("xnat:mrSessionData/fields/field[name=type]/field=Research",))
        assert "[name=type]" in result
        # Value should be "Research", not "type]/field=Research"
        assert result.endswith("=Research")
        assert "field[name=type]/field" in result

    def test_split_param_bracket_edge_case(self) -> None:
        """Split on first = outside brackets, not inside."""
        from xnatctl.cli.api import _split_param

        result = _split_param("xnat:mrSessionData/fields/field[name=session_type]/field=Research")
        assert result is not None
        key, value = result
        assert key == "xnat:mrSessionData/fields/field[name=session_type]/field"
        assert value == "Research"

    def test_split_param_no_equals(self) -> None:
        from xnatctl.cli.api import _split_param

        assert _split_param("noequalssign") is None

    def test_no_equals_skipped(self) -> None:
        from xnatctl.cli.api import _build_query_string

        result = _build_query_string(("noequalssign",))
        assert result == ""


class TestIsTextContentType:
    """Tests for _is_text_content_type helper."""

    def test_text_plain(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("text/plain") is True

    def test_text_html_with_charset(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("text/html; charset=utf-8") is True

    def test_application_json(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("application/json") is True

    def test_application_xml(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("application/xml") is True

    def test_octet_stream_is_binary(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("application/octet-stream") is False

    def test_matlab_is_binary(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("application/x-matlab-data") is False

    def test_empty_string(self) -> None:
        from xnatctl.cli.api import _is_text_content_type

        assert _is_text_content_type("") is False


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
                    result = runner.invoke(cli, ["api", "get", "/data/projects"])

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
        url = call_args[0][0]
        assert "columns=ID%2Cname" in url or "columns=ID,name" in url

    def test_api_get_json_output_format(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "value"}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["api", "get", "/some/path", "-o", "json"])

        assert result.exit_code == 0
        assert "key" in result.output

    def test_api_get_non_json_response(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = "plain text response"
        mock_resp.headers = {"content-type": "text/plain"}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["api", "get", "/some/text/endpoint"])

        assert result.exit_code == 0
        assert "plain text response" in result.output

    def test_api_get_binary_response_preserved(self, runner: CliRunner) -> None:
        """Binary responses are written as raw bytes without text decoding."""
        client = _mock_client()
        mock_resp = MagicMock()
        # Bytes 0x80-0xFF are invalid in UTF-8 and would be corrupted by text decoding
        binary_data = bytes(range(256))
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.content = binary_data
        mock_resp.headers = {"content-type": "application/octet-stream"}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["api", "get", "/some/file.mat"])

        assert result.exit_code == 0
        assert binary_data in result.output_bytes

    def test_api_get_json_format_non_json_errors(self, runner: CliRunner) -> None:
        """Requesting -o json when response is not JSON produces an error."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = "plain text"
        mock_resp.headers = {"content-type": "text/plain"}
        client.get.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["api", "get", "/some/endpoint", "-o", "json"])

        assert result.exit_code != 0
        assert "not JSON" in result.output

    def test_api_get_xsi_typed_params_not_encoded(self, runner: CliRunner) -> None:
        """XSI-typed param keys like xnat:mrSessionData preserve colons."""
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
                            "/data/experiments",
                            "-P",
                            "xsiType=xnat:mrSessionData",
                            "-P",
                            "columns=ID,label",
                        ],
                    )

        assert result.exit_code == 0
        url = client.get.call_args[0][0]
        # Colons must NOT be percent-encoded
        assert "xsiType=xnat:mrSessionData" in url
        assert "%3A" not in url.split("?")[1]  # no encoded colons


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
        call_args = client.post.call_args
        assert call_args[1]["json"] == {"ID": "NEWPROJ"}
        # URL should be the path directly (no query string)
        assert call_args[0][0] == "/data/projects"

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

    def test_api_post_shows_status_code(self, runner: CliRunner) -> None:
        """POST response shows HTTP status code on stderr."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "XNAT_E00001"
        mock_resp.text = "XNAT_E00001"
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/experiments",
                            "-P",
                            "xnat:mrSessionData/subject_ID=XNAT_S00001",
                        ],
                    )

        assert result.exit_code == 0
        # Status line goes to stderr (captured in output by CliRunner)
        assert "[200]" in result.output

    def test_api_post_xsi_params_preserved(self, runner: CliRunner) -> None:
        """POST with XSI-typed params preserves colons and slashes."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "XNAT_E00001"
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/experiments",
                            "-P",
                            "xnat:experimentData/subject_ID=XNAT_S00001",
                        ],
                    )

        assert result.exit_code == 0
        url = client.post.call_args[0][0]
        assert "xnat:experimentData/subject_ID=XNAT_S00001" in url


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

    def test_api_put_shows_status_code(self, runner: CliRunner) -> None:
        """PUT response shows HTTP status code on stderr."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "XNAT_E00001"
        mock_resp.text = "XNAT_E00001"
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/experiments/XNAT_E00001",
                            "-P",
                            "xnat:mrSessionData/fields/field[name=session_type]/field=Research",
                        ],
                    )

        assert result.exit_code == 0
        assert "[200]" in result.output
        # XSI-typed key preserved
        url = client.put.call_args[0][0]
        assert "xnat:mrSessionData" in url
        assert "%3A" not in url.split("?")[1]


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
        url = call_args[0][0]
        assert "removeFiles=true" in url
