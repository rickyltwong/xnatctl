"""Tests for core HTTP client authentication behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import PermissionDeniedError, SessionExpiredError


def _make_response(status_code: int) -> httpx.Response:
    req = httpx.Request("GET", "https://example.org/data/user")
    return httpx.Response(status_code, request=req, json={"ok": True})


def test_request_auto_reauth_retries_once_on_401(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        username="user",
        password="pass",
        session_token="old-token",
        auto_reauth=True,
        max_retries=0,
    )

    mock_httpx = MagicMock()
    mock_httpx.request = MagicMock(side_effect=[_make_response(401), _make_response(200)])
    monkeypatch.setattr(client, "_get_client", MagicMock(return_value=mock_httpx))

    def fake_authenticate() -> str:
        client.session_token = "new-token"
        return "new-token"

    monkeypatch.setattr(client, "authenticate", MagicMock(side_effect=fake_authenticate))

    resp = client.get("/data/user")

    assert resp.status_code == 200
    client.authenticate.assert_called_once()
    assert mock_httpx.request.call_count == 2

    first_call = mock_httpx.request.call_args_list[0].kwargs
    second_call = mock_httpx.request.call_args_list[1].kwargs
    assert first_call["cookies"] == {"JSESSIONID": "old-token"}
    assert second_call["cookies"] == {"JSESSIONID": "new-token"}


def test_request_raises_session_expired_when_auto_reauth_disabled(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        username="user",
        password="pass",
        session_token="old-token",
        auto_reauth=False,
        max_retries=0,
    )

    mock_httpx = MagicMock()
    mock_httpx.request = MagicMock(return_value=_make_response(401))
    monkeypatch.setattr(client, "_get_client", MagicMock(return_value=mock_httpx))

    with pytest.raises(SessionExpiredError) as excinfo:
        client.get("/data/user")

    err = excinfo.value
    assert err.details["status_code"] == 401
    assert err.details["method"] == "GET"
    assert err.details["path"] == "/data/user"


def test_request_raises_session_expired_when_no_creds(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        session_token="old-token",
        auto_reauth=True,
        max_retries=0,
    )

    mock_httpx = MagicMock()
    mock_httpx.request = MagicMock(return_value=_make_response(401))
    monkeypatch.setattr(client, "_get_client", MagicMock(return_value=mock_httpx))

    with pytest.raises(SessionExpiredError):
        client.get("/data/user")


def test_request_raises_permission_denied_on_403_without_reauth(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        username="user",
        password="pass",
        session_token="token",
        auto_reauth=True,
        max_retries=0,
    )

    mock_httpx = MagicMock()
    mock_httpx.request = MagicMock(return_value=_make_response(403))
    monkeypatch.setattr(client, "_get_client", MagicMock(return_value=mock_httpx))
    monkeypatch.setattr(client, "authenticate", MagicMock())

    with pytest.raises(PermissionDeniedError) as excinfo:
        client.get("/data/user")

    client.authenticate.assert_not_called()
    err = excinfo.value
    assert err.details["status_code"] == 403
    assert err.details["method"] == "GET"
    assert err.details["path"] == "/data/user"
