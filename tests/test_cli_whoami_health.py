from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.models.info import ServerInfo, UserInfo


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def two_profile_config(monkeypatch) -> Config:
    monkeypatch.delenv("XNAT_TOKEN", raising=False)
    monkeypatch.delenv("XNAT_USER", raising=False)
    monkeypatch.delenv("XNAT_PASS", raising=False)
    monkeypatch.delenv("XNAT_PROFILE", raising=False)
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://default.example.org",
                username="duser",
                password="dpass",
                default_project="DEFPROJ",
            ),
            "prod": Profile(
                url="https://prod.example.org",
                username="puser",
                password="ppass",
                default_project="PRODPROJ",
            ),
        },
    )


def _whoami_result() -> UserInfo:
    return UserInfo(username="serveruser", enabled=True)


def _ping_result(url: str) -> ServerInfo:
    return ServerInfo(url=url, status="ok", version="1.8.0", latency_ms=5)


def _mock_client():
    ctx = patch("xnatctl.cli.common.XNATClient")
    mock_cls = ctx.__enter__()
    inst = mock_cls.return_value
    inst.is_authenticated = True
    inst.session_token = "tok"
    inst.whoami.return_value = _whoami_result()
    inst.ping.return_value = _ping_result("https://default.example.org")
    return ctx, mock_cls, inst


def test_whoami_honors_profile(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        try:
            result = runner.invoke(cli, ["-p", "prod", "whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert mock_cls.call_args.kwargs["base_url"] == "https://prod.example.org"


def test_whoami_honors_output_json(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.base_url = "https://prod.example.org"
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "json", "whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["username"] == "serveruser"
    assert payload["server"] == "https://prod.example.org"
    assert payload["profile"] == "prod"


def test_whoami_quiet_prints_only_username(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        try:
            result = runner.invoke(cli, ["-q", "whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert result.output.strip() == "serveruser"


def test_whoami_not_authenticated_exits_auth_error(runner, monkeypatch) -> None:
    monkeypatch.delenv("XNAT_TOKEN", raising=False)
    monkeypatch.delenv("XNAT_USER", raising=False)
    monkeypatch.delenv("XNAT_PASS", raising=False)
    cfg = Config(
        default_profile="default",
        profiles={"default": Profile(url="https://default.example.org")},
    )
    with config_seam(cfg):
        cm, mock_cls, inst = _mock_client()
        inst.is_authenticated = False
        inst.username = None
        inst.password = None
        inst.session_token = None
        try:
            result = runner.invoke(cli, ["whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 3
    assert "Traceback" not in result.output
    assert "Not authenticated" in result.output


def test_health_ping_honors_profile(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        try:
            result = runner.invoke(cli, ["-p", "prod", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert mock_cls.call_args.kwargs["base_url"] == "https://prod.example.org"


def test_health_ping_honors_output_json(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.base_url = "https://prod.example.org"
        inst.ping.return_value = _ping_result("https://prod.example.org")
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "json", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["url"] == "https://prod.example.org"
    assert payload["authenticated"] is True


def test_health_ping_honors_output_tsv(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.base_url = "https://prod.example.org"
        inst.ping.return_value = _ping_result("https://prod.example.org")
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "tsv", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert "\x1b" not in result.output
    assert "┃" not in result.output and "│" not in result.output
    lines = [line for line in result.output.splitlines() if "\t" in line]
    assert lines[0] == "status\tversion\tlatency\tauthenticated"
    assert lines[1].split("\t") == ["ok", "1.8.0", "5ms", "true"]


def test_health_ping_quiet_prints_only_url(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.ping.return_value = _ping_result("https://prod.example.org")
        try:
            result = runner.invoke(cli, ["-p", "prod", "-q", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert result.output.strip() == "https://prod.example.org"
    assert "Server reachable" not in result.output


def test_whoami_json_shape_is_pinned(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.base_url = "https://prod.example.org"
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "json", "whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    expected = {
        "username": "serveruser",
        "server": "https://prod.example.org",
        "profile": "prod",
        "default_project": "PRODPROJ",
        "auth_mode": "session",
    }
    assert result.output == json.dumps(expected, indent=2) + "\n"


def test_health_ping_json_shape_is_pinned(runner, two_profile_config) -> None:
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.ping.return_value = _ping_result("https://prod.example.org")
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "json", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    expected = {
        "url": "https://prod.example.org",
        "status": "ok",
        "version": "1.8.0",
        "latency_ms": 5,
        "authenticated": True,
    }
    assert result.output == json.dumps(expected, indent=2) + "\n"
