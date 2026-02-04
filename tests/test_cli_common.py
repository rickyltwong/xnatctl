"""Tests for CLI common helpers."""

from __future__ import annotations

from typing import Any, cast
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

    class FakeClient:
        base_url = "https://example.org"

        def __init__(self) -> None:
            self.session_token = "old-token"
            self.authenticate_calls = 0

        @property
        def is_authenticated(self) -> bool:
            return self.session_token is not None

        def whoami(self) -> dict[str, str]:
            raise AuthenticationError("https://example.org", "expired")

        def authenticate(self) -> str:
            self.authenticate_calls += 1
            self.session_token = "new-token"
            return "new-token"

    mock_client = FakeClient()

    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)
    result = decorated(ctx)

    assert result == "ok"
    ctx.auth_manager.clear_session.assert_called_once_with()
    assert mock_client.authenticate_calls == 1
    ctx.auth_manager.save_session.assert_called_once_with(
        token="new-token",
        url="https://example.org",
        username="user",
    )
    assert mock_client.session_token == "new-token"


def test_require_auth_raises_when_session_expired_and_no_creds(monkeypatch):
    monkeypatch.delenv("XNAT_USER", raising=False)
    monkeypatch.delenv("XNAT_PASS", raising=False)

    ctx = Context()
    ctx.config = Config(profiles={"default": Profile(url="https://example.org")})

    class FakeClient:
        base_url = "https://example.org"

        def __init__(self) -> None:
            self.session_token = "old-token"

        @property
        def is_authenticated(self) -> bool:
            return self.session_token is not None

        def whoami(self) -> dict[str, str]:
            raise AuthenticationError("https://example.org", "expired")

    mock_client = FakeClient()

    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()

    decorated = require_auth(_protected_command)

    with pytest.raises(click.ClickException) as excinfo:
        decorated(ctx)

    message = str(excinfo.value)
    assert "Session expired" in message
    assert "xnatctl auth login" in message
