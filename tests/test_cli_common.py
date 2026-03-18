"""Tests for CLI common helpers."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

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


def test_get_client_uses_cached_session_username_as_hint():
    ctx = Context()
    ctx.config = Config(
        default_profile="default",
        profiles={"default": Profile(url="https://example.org", verify_ssl=False)},
    )

    mock_session = MagicMock()
    mock_session.token = "cached-token"
    mock_session.username = "Ricky_Wong"

    ctx.auth_manager = MagicMock()
    ctx.auth_manager.load_session.return_value = mock_session
    ctx.auth_manager.get_token_from_env.return_value = None

    with patch("xnatctl.cli.common.XNATClient") as mock_client_cls:
        ctx.get_client()

    assert mock_client_cls.call_args.kwargs["username"] == "Ricky_Wong"
    assert mock_client_cls.call_args.kwargs["session_token"] == "cached-token"
