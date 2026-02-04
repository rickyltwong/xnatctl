"""Tests for CLI common helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from xnatctl.cli.common import Context, require_auth
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import AuthenticationError


def _protected_command(ctx: Context) -> str:
    return "ok"


def test_require_auth_reauthenticates_on_stale_session():
    ctx = Context()
    ctx.config = Config(profiles={"default": Profile(url="https://example.org")})
    ctx.config.profiles["default"].username = "user"
    ctx.config.profiles["default"].password = "pass"

    mock_client = MagicMock()
    mock_client.is_authenticated = True
    mock_client.whoami.side_effect = AuthenticationError("https://example.org", "expired")
    mock_client.authenticate.return_value = "new-token"
    mock_client.base_url = "https://example.org"
    mock_client.session_token = "old-token"

    ctx.client = mock_client
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)
    result = decorated(ctx)

    assert result == "ok"
    ctx.auth_manager.clear_session.assert_called_once_with()
    mock_client.authenticate.assert_called_once_with()
    ctx.auth_manager.save_session.assert_called_once_with(
        token="new-token",
        url="https://example.org",
        username="user",
    )
    assert mock_client.session_token is None


def test_require_auth_raises_when_session_expired_and_no_creds(monkeypatch):
    monkeypatch.delenv("XNAT_USER", raising=False)
    monkeypatch.delenv("XNAT_PASS", raising=False)

    ctx = Context()
    ctx.config = Config(profiles={"default": Profile(url="https://example.org")})

    mock_client = MagicMock()
    mock_client.is_authenticated = True
    mock_client.whoami.side_effect = AuthenticationError("https://example.org", "expired")
    mock_client.base_url = "https://example.org"
    mock_client.session_token = "old-token"

    ctx.client = mock_client
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)

    with pytest.raises(click.ClickException) as excinfo:
        decorated(ctx)

    message = str(excinfo.value)
    assert "Session expired" in message
    assert "xnatctl auth login" in message
