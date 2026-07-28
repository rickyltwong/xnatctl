"""Behavior tests for `whoami` and `health ping` (CLI-02 / MAINT-09).

These pin the two root-level commands onto the standard decorator stack: they
must honor the global ``--profile``/``-o``/``-q`` flags (wrong-server identity is
dangerous for a multi-instance admin tool) and route errors through
``@handle_errors`` instead of hand-rolled ``except Exception`` blocks.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def two_profile_config(monkeypatch) -> Config:
    """A config with a default and a prod profile, each with distinct URLs."""
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


def _mock_client():
    """A patch context for XNATClient plus a configured instance."""
    ctx = patch("xnatctl.cli.common.XNATClient")
    mock_cls = ctx.__enter__()
    inst = mock_cls.return_value
    inst.is_authenticated = True
    inst.session_token = "tok"
    inst.whoami.return_value = {"username": "serveruser"}
    inst.ping.return_value = {
        "url": inst.base_url,
        "status": "OK",
        "version": "1.8.0",
        "latency_ms": 5,
    }
    return ctx, mock_cls, inst


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


def test_whoami_honors_profile(runner, two_profile_config) -> None:
    """`-p prod whoami` must target prod's server, not the default profile."""
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        try:
            result = runner.invoke(cli, ["-p", "prod", "whoami"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    # The client was constructed for prod's URL, proving the profile was honored.
    assert mock_cls.call_args.kwargs["base_url"] == "https://prod.example.org"


def test_whoami_honors_output_json(runner, two_profile_config) -> None:
    """`-o json whoami` must emit parseable JSON (not hardcoded table)."""
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
    """No credentials at all -> AUTH_ERROR (3), a clean line, no traceback."""
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


# ---------------------------------------------------------------------------
# health ping
# ---------------------------------------------------------------------------


def test_health_ping_honors_profile(runner, two_profile_config) -> None:
    """`-p prod health ping` pings prod's server."""
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
        inst.ping.return_value = {
            "url": "https://prod.example.org",
            "status": "OK",
            "version": "1.8.0",
            "latency_ms": 5,
        }
        try:
            result = runner.invoke(cli, ["-p", "prod", "-o", "json", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["url"] == "https://prod.example.org"
    assert payload["authenticated"] is True


def test_health_ping_quiet_prints_only_url(runner, two_profile_config) -> None:
    """`-q health ping` suppresses the banner/table, prints just the URL."""
    with config_seam(two_profile_config):
        cm, mock_cls, inst = _mock_client()
        inst.ping.return_value = {
            "url": "https://prod.example.org",
            "status": "OK",
            "version": "1.8.0",
            "latency_ms": 5,
        }
        try:
            result = runner.invoke(cli, ["-p", "prod", "-q", "health", "ping"])
        finally:
            cm.__exit__(None, None, None)

    assert result.exit_code == 0
    assert result.output.strip() == "https://prod.example.org"
    assert "Server reachable" not in result.output
