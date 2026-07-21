"""Tests for xnatctl CLI api commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import click
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
                            "--params",
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

    def test_api_get_json_format_non_json_falls_back_to_passthrough(
        self, runner: CliRunner
    ) -> None:
        """Requesting -o json when response is not JSON warns and passes the body through.

        Issue #13: the previous hard-error branch was user-hostile (it
        discarded the body); the CLI now emits a one-line stderr warning
        and writes the raw body to stdout, exiting 0.
        """
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

        assert result.exit_code == 0
        assert "plain text" in result.output
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
                            "--params",
                            "xsiType=xnat:mrSessionData",
                            "--params",
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
                            "--params",
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
                            "--params",
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
                            "--params",
                            "xnat:mrSessionData/fields/field[name=session_type]/field=Research",
                        ],
                    )

        assert result.exit_code == 0
        assert "[200]" in result.output
        # XSI-typed key preserved
        url = client.put.call_args[0][0]
        assert "xnat:mrSessionData" in url
        assert "%3A" not in url.split("?")[1]

    def test_api_put_resource_file_auto_inbody(self, runner: CliRunner, tmp_path) -> None:
        """PUT of a file to a resource endpoint auto-adds inbody=true (issue #18)."""
        payload = tmp_path / "params.json"
        payload.write_text('{"k": "v"}')

        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/projects/PROJ/resources/BIDS/files/params.json",
                            "-f",
                            str(payload),
                        ],
                    )

        assert result.exit_code == 0
        url = client.put.call_args[0][0]
        assert "inbody=true" in url
        assert "note: added inbody=true" in result.output

    def test_api_put_resource_file_inbody_not_overridden(self, runner: CliRunner, tmp_path) -> None:
        """An explicit inbody param is preserved; nothing auto-injected."""
        payload = tmp_path / "params.json"
        payload.write_text('{"k": "v"}')

        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/projects/PROJ/resources/BIDS/files/params.json",
                            "--params",
                            "inbody=false",
                            "-f",
                            str(payload),
                        ],
                    )

        assert result.exit_code == 0
        url = client.put.call_args[0][0]
        assert "inbody=false" in url
        assert "inbody=true" not in url
        assert "note: added inbody=true" not in result.output

    def test_api_put_non_file_path_no_inbody(self, runner: CliRunner) -> None:
        """A non-resource-file PUT is left untouched (no over-eager injection)."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
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
        url = client.put.call_args[0][0]
        assert "inbody" not in url

    def test_api_put_inbody_without_body_errors(self, runner: CliRunner) -> None:
        """inbody=true with no body is an actionable error, not a silent empty PUT."""
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/projects/PROJ/resources/BIDS/files/params.json",
                            "--params",
                            "inbody=true",
                        ],
                    )

        assert result.exit_code != 0
        assert "no request body" in result.output
        client.put.assert_not_called()


class TestInbodyHelpers:
    """Unit tests for the resource-file inbody helpers."""

    def test_named_file_matches(self) -> None:
        from xnatctl.cli.api import _is_resource_file_path

        assert _is_resource_file_path("/data/projects/P/resources/BIDS/files/params.json")
        assert _is_resource_file_path("/data/experiments/E/scans/1/resources/DICOM/files/a.dcm")

    def test_collection_endpoint_not_matched(self) -> None:
        from xnatctl.cli.api import _is_resource_file_path

        # Bare .../files collection (zip/bulk upload) must not match.
        assert not _is_resource_file_path("/data/projects/P/resources/BIDS/files")

    def test_non_resource_path_not_matched(self) -> None:
        from xnatctl.cli.api import _is_resource_file_path

        assert not _is_resource_file_path("/data/projects/P")

    def test_inject_when_body_present(self) -> None:
        from xnatctl.cli.api import _maybe_add_inbody

        out = _maybe_add_inbody("/data/projects/P/resources/R/files/f.json", (), has_body=True)
        assert out == ("inbody=true",)

    def test_existing_inbody_respected(self) -> None:
        from xnatctl.cli.api import _maybe_add_inbody

        out = _maybe_add_inbody(
            "/data/projects/P/resources/R/files/f.json",
            ("inbody=false",),
            has_body=True,
        )
        assert out == ("inbody=false",)

    def test_no_inject_without_body(self) -> None:
        from xnatctl.cli.api import _maybe_add_inbody

        out = _maybe_add_inbody("/data/projects/P/resources/R/files/f.json", (), has_body=False)
        assert out == ()

    def test_empty_body_with_inbody_raises(self) -> None:
        from xnatctl.cli.api import _maybe_add_inbody

        with pytest.raises(click.UsageError):
            _maybe_add_inbody(
                "/data/projects/P/resources/R/files/f.json",
                ("inbody=true",),
                has_body=False,
            )


class TestApiBinaryFileBody:
    """Tests for binary-safe ``--file/-f`` bodies in api put/post."""

    def test_api_put_binary_file(self, runner: CliRunner, tmp_path) -> None:
        """A non-UTF-8 file (DICOM-like) is sent as raw bytes via ``data=``."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        # 0xBE is the byte that triggered UnicodeDecodeError in the wild.
        binary_path = tmp_path / "blob.dcm"
        binary_path.write_bytes(b"\xbe\x00\x01\x02fake-dicom\xff")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/foo/files/blob.dcm",
                            "--file",
                            str(binary_path),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == b"\xbe\x00\x01\x02fake-dicom\xff"
        assert isinstance(call_kwargs["data"], bytes)

    def test_api_post_binary_file(self, runner: CliRunner, tmp_path) -> None:
        """A non-UTF-8 file is sent via POST as raw bytes via ``data=``."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.post.return_value = mock_resp

        binary_path = tmp_path / "blob.bin"
        binary_path.write_bytes(b"\xbe\xbf\xc0\xc1")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/services/import",
                            "--file",
                            str(binary_path),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == b"\xbe\xbf\xc0\xc1"
        assert isinstance(call_kwargs["data"], bytes)

    def test_api_put_json_file_still_uses_json_kwarg(self, runner: CliRunner, tmp_path) -> None:
        """JSON-decodable UTF-8 files preserve the existing ``json=`` ergonomics."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        json_path = tmp_path / "payload.json"
        json_path.write_text(json.dumps({"description": "Updated"}))

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/projects/MYPROJ",
                            "--file",
                            str(json_path),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] == {"description": "Updated"}
        assert call_kwargs["data"] is None

    def test_api_put_text_non_json_file_uses_data_kwarg(self, runner: CliRunner, tmp_path) -> None:
        """Plain UTF-8 text that is not JSON is sent as a string via ``data=``."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        text_path = tmp_path / "note.txt"
        text_path.write_text("plain non-json text body")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/foo",
                            "--file",
                            str(text_path),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == "plain non-json text body"
        assert isinstance(call_kwargs["data"], str)


class TestApiContentType:
    """Tests for the ``--content-type/-t`` flag on api post and api put."""

    def test_explicit_content_type_with_inline_data(self, runner: CliRunner) -> None:
        """``-d 'user:pass' -t text/plain`` -> data=user:pass, header set."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/xapi/xsync/credentials/check/projects/PROJ",
                            "-d",
                            "user:pass",
                            "-t",
                            "text/plain",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["data"] == "user:pass"
        assert call_kwargs["json"] is None
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}

    def test_explicit_content_type_overrides_extension(self, runner: CliRunner, tmp_path) -> None:
        """``-f payload.json -t text/plain`` -> explicit flag wins, body verbatim."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        # Write a JSON file with no whitespace inside the object; the
        # explicit-content-type path must send these bytes verbatim, not
        # re-serialize through json.dumps (which would inject a space).
        payload = tmp_path / "payload.json"
        payload.write_text('{"k":"v"}')

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/xapi/some/endpoint",
                            "-f",
                            str(payload),
                            "-t",
                            "text/plain",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}
        # Body sent byte-for-byte verbatim, not normalized JSON.
        assert call_kwargs["data"] == '{"k":"v"}'

    def test_explicit_text_plain_preserves_json_file_bytes_verbatim(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """``-f creds.json -t text/plain`` with whitespace-sensitive contents."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        # Compact, ordering-sensitive JSON literal. If the body is round-
        # tripped through json.loads / json.dumps the wire bytes will differ.
        original = '{"k":"v","n":1}'
        payload = tmp_path / "creds.json"
        payload.write_text(original)

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/xapi/xsync/credentials/check/projects/PROJ",
                            "-f",
                            str(payload),
                            "-t",
                            "text/plain",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}
        assert call_kwargs["data"] == original

    def test_put_explicit_content_type_preserves_file_bytes_verbatim(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """``api put -f creds.json -t text/plain`` parity with POST verbatim."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.put.return_value = mock_resp

        original = '{"k":"v","n":1}'
        payload = tmp_path / "creds.json"
        payload.write_text(original)

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/xapi/xsync/credentials/save/projects/PROJ",
                            "-f",
                            str(payload),
                            "-t",
                            "text/plain",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}
        assert call_kwargs["data"] == original

    def test_auto_detect_json_extension(self, runner: CliRunner, tmp_path) -> None:
        """``-f payload.json`` without -t keeps the json= path."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"k": "v"}))

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "post",
                            "/data/services/import",
                            "-f",
                            str(payload),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"k": "v"}
        assert call_kwargs["data"] is None
        # httpx sets application/json automatically, so we leave headers unset.
        assert call_kwargs.get("headers") is None

    def test_auto_detect_txt_extension(self, runner: CliRunner, tmp_path) -> None:
        """``-f notes.txt`` -> data=<text>, Content-Type: text/plain."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        notes = tmp_path / "notes.txt"
        notes.write_text("hello world")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/foo", "-f", str(notes)],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == "hello world"
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}

    def test_auto_detect_xml_extension(self, runner: CliRunner, tmp_path) -> None:
        """``-f project.xml`` -> data=<text>, Content-Type: application/xml."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.put.return_value = mock_resp

        proj = tmp_path / "project.xml"
        proj.write_text("<project><ID>PROJ</ID></project>")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "put", "/data/projects/PROJ", "-f", str(proj)],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == "<project><ID>PROJ</ID></project>"
        assert call_kwargs["headers"] == {"Content-Type": "application/xml"}

    def test_unknown_extension_falls_back_to_octet_stream(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """``-f payload.bin`` (valid UTF-8 inside) -> octet-stream."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        blob = tmp_path / "payload.bin"
        blob.write_text("printable bytes")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/foo", "-f", str(blob)],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == "printable bytes"
        assert call_kwargs["headers"] == {"Content-Type": "application/octet-stream"}

    def test_binary_file_forces_octet_stream(self, runner: CliRunner, tmp_path) -> None:
        """A non-UTF-8 .dcm file -> octet-stream, body sent as raw bytes."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.put.return_value = mock_resp

        blob = tmp_path / "foo.dcm"
        blob.write_bytes(b"\xbe\x00\x01\x02DICM\xff")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/data/foo/files/foo.dcm",
                            "-f",
                            str(blob),
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == b"\xbe\x00\x01\x02DICM\xff"
        assert call_kwargs["headers"] == {"Content-Type": "application/octet-stream"}

    def test_non_utf8_json_extension_still_octet_stream(self, runner: CliRunner, tmp_path) -> None:
        """Decode failure wins over .json extension."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        # Misleading extension but invalid UTF-8 content.
        bad = tmp_path / "weird.json"
        bad.write_bytes(b"\xbe\xbf\xc0")

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/foo", "-f", str(bad)],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] is None
        assert call_kwargs["data"] == b"\xbe\xbf\xc0"
        assert call_kwargs["headers"] == {"Content-Type": "application/octet-stream"}

    def test_default_inline_data_still_json(self, runner: CliRunner) -> None:
        """Regression: ``-d '{"k":"v"}'`` without -t still uses json=."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/projects", "-d", '{"k":"v"}'],
                    )

        assert result.exit_code == 0
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"k": "v"}
        assert call_kwargs["data"] is None
        # No explicit Content-Type header; httpx sets application/json itself.
        assert call_kwargs.get("headers") is None

    def test_put_explicit_content_type(self, runner: CliRunner) -> None:
        """``api put -d 'user:pass' -t text/plain`` -> parity with POST."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "api",
                            "put",
                            "/xapi/xsync/credentials/save/projects/PROJ",
                            "-d",
                            "user:pass",
                            "-t",
                            "text/plain",
                        ],
                    )

        assert result.exit_code == 0
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["data"] == "user:pass"
        assert call_kwargs["json"] is None
        assert call_kwargs["headers"] == {"Content-Type": "text/plain"}

    def test_help_lists_content_type_flag(self, runner: CliRunner) -> None:
        """Help text for both verbs exposes -t/--content-type."""
        result_post = runner.invoke(cli, ["api", "post", "--help"])
        assert result_post.exit_code == 0
        assert "--content-type" in result_post.output
        assert "-t" in result_post.output

        result_put = runner.invoke(cli, ["api", "put", "--help"])
        assert result_put.exit_code == 0
        assert "--content-type" in result_put.output
        assert "-t" in result_put.output


class TestDetectContentType:
    """Direct unit tests for the ``_detect_content_type`` helper."""

    def test_none_file_path_returns_none(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert _detect_content_type(None, raw_bytes=None, decoded_text=None) is None

    def test_decode_failure_always_octet_stream(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        # decoded_text=None signals UTF-8 decode failure; overrides .json ext.
        assert (
            _detect_content_type("foo.json", raw_bytes=b"\xbe\xbf", decoded_text=None)
            == "application/octet-stream"
        )

    def test_json_extension(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type("payload.json", raw_bytes=b'{"k":"v"}', decoded_text='{"k":"v"}')
            == "application/json"
        )

    def test_txt_extension(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert _detect_content_type("notes.txt", raw_bytes=b"hi", decoded_text="hi") == "text/plain"

    def test_xml_extension(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type("project.xml", raw_bytes=b"<x/>", decoded_text="<x/>")
            == "application/xml"
        )

    def test_unknown_extension(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type("foo.bin", raw_bytes=b"hi", decoded_text="hi")
            == "application/octet-stream"
        )

    def test_no_extension(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type("foo", raw_bytes=b"hi", decoded_text="hi")
            == "application/octet-stream"
        )

    def test_extension_case_insensitive(self) -> None:
        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type("payload.JSON", raw_bytes=b'{"k":"v"}', decoded_text='{"k":"v"}')
            == "application/json"
        )

    def test_pathlib_path_accepted(self) -> None:
        """The helper accepts ``pathlib.Path`` as well as ``str``."""
        from pathlib import Path

        from xnatctl.cli.api import _detect_content_type

        assert (
            _detect_content_type(Path("notes.txt"), raw_bytes=b"hi", decoded_text="hi")
            == "text/plain"
        )


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
                            "--params",
                            "removeFiles=true",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        call_args = client.delete.call_args
        url = call_args[0][0]
        assert "removeFiles=true" in url


# =============================================================================
# B01 — secret-leak fixes (D1, D2, D3, D4)
# =============================================================================


# Click wraps help text; assert on tokens that survive line-wrapping.
_PARAMS_WARNING_TOKENS = ("Do not use for", "JSON body")


class TestApiParamsHelpWarning:
    """D1 — ``--params`` help text warns about secret leakage on all 4 cmds."""

    @pytest.mark.parametrize("verb", ["get", "post", "put", "delete"])
    def test_api_params_help_warns_on_secrets(self, runner: CliRunner, verb: str) -> None:
        """Every ``api {verb} --help`` documents the secret-leak warning."""
        result = runner.invoke(cli, ["api", verb, "--help"])
        assert result.exit_code == 0
        for token in _PARAMS_WARNING_TOKENS:
            assert token in result.output, f"missing warning token {token!r} in --help for {verb}"
        # Points users at the safer body-passing options.
        assert "-d" in result.output
        assert "-f" in result.output


class TestApiStdinBody:
    """D2 — ``-d -`` and ``-f -`` read the body once from stdin."""

    def test_api_post_reads_body_from_stdin(self, runner: CliRunner) -> None:
        """``api post -d -`` reads JSON body from stdin and POSTs it."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/endpoint", "-d", "-"],
                        input='{"k":"v"}',
                    )

        assert result.exit_code == 0, result.output
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"k": "v"}
        assert call_kwargs["data"] is None

    def test_api_put_reads_body_from_stdin(self, runner: CliRunner) -> None:
        """``api put -d -`` reads JSON body from stdin and PUTs it."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.put.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "put", "/data/projects/MYPROJ", "-d", "-"],
                        input='{"description":"x"}',
                    )

        assert result.exit_code == 0, result.output
        call_kwargs = client.put.call_args[1]
        assert call_kwargs["json"] == {"description": "x"}
        assert call_kwargs["data"] is None

    def test_api_post_stdin_sentinel_for_file_flag(self, runner: CliRunner) -> None:
        """``api post -f -`` is symmetric with ``-d -`` and also reads stdin."""
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.post.return_value = mock_resp

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/endpoint", "-f", "-"],
                        input='{"from":"stdin"}',
                    )

        assert result.exit_code == 0, result.output
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"from": "stdin"}
        assert call_kwargs["data"] is None

    def test_api_post_rejects_double_stdin(self, runner: CliRunner) -> None:
        """Passing both ``-d -`` and ``-f -`` is a usage error."""
        client = _mock_client()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["api", "post", "/data/endpoint", "-d", "-", "-f", "-"],
                        input='{"k":"v"}',
                    )

        assert result.exit_code != 0
        assert "cannot combine -d - and -f -" in result.output
        client.post.assert_not_called()


class TestApiEnvVarExpansion:
    """D3 — ``${VAR}`` / ``$VAR`` expansion inside the ``-d`` value."""

    def test_api_post_expands_env_vars_in_data(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Set env vars are substituted before JSON parse."""
        monkeypatch.setenv("CNMDP_PASS", "hunter2")
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
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
                            "-d",
                            '{"p":"$CNMDP_PASS"}',
                        ],
                    )

        assert result.exit_code == 0, result.output
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"p": "hunter2"}

    def test_api_post_warns_on_unset_env_var(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset ``${VAR}`` references produce a warning listing the names."""
        monkeypatch.delenv("XNATCTL_NOPE", raising=False)
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
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
                            "-d",
                            '{"p":"${XNATCTL_NOPE}"}',
                        ],
                    )

        # Warning + non-zero JSON parse failure are both acceptable; the
        # warning is the load-bearing assertion here.
        assert "XNATCTL_NOPE" in result.output
        assert "not set" in result.output

    def test_api_post_envvar_inside_json_string_value(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``${VAR}`` inside a JSON string value is substituted in place."""
        monkeypatch.setenv("XNATCTL_TOKEN", "abc123")
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
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
                            "-d",
                            '{"auth":"Bearer ${XNATCTL_TOKEN}"}',
                        ],
                    )

        assert result.exit_code == 0, result.output
        call_kwargs = client.post.call_args[1]
        assert call_kwargs["json"] == {"auth": "Bearer abc123"}


class TestHandleErrorsRedaction:
    """D4 — ``handle_errors`` strips secrets from URLs before printing."""

    def test_handle_errors_redacts_url_password(self, runner: CliRunner) -> None:
        """An exception whose str includes ``?password=hunter2`` is scrubbed."""
        import re as _re

        from xnatctl.cli.common import handle_errors
        from xnatctl.core.exceptions import XNATCtlError

        @click.command()
        @handle_errors
        def boom() -> None:
            """Raise an XNATCtlError that mentions a secret-bearing URL."""
            raise XNATCtlError(
                "Request failed: https://xnat.example.org/xapi/x?username=admin&password=hunter2"
            )

        result = runner.invoke(boom, [])
        assert result.exit_code == 1
        # Strip ANSI escapes and Rich's soft line wrapping before asserting,
        # since the err_console formatter colors and wraps URLs.
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        plain = plain.replace("\n", "")
        assert "hunter2" not in plain
        assert "password=***" in plain
        # Non-secret keys are kept for debuggability.
        assert "username=admin" in plain
