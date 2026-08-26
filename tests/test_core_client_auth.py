"""Tests for core HTTP client authentication behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    NetworkError,
    PermissionDeniedError,
    ResourceNotFoundError,
    SessionExpiredError,
)


def _make_response(status_code: int) -> httpx.Response:
    req = httpx.Request("GET", "https://example.org/data/user")
    return httpx.Response(status_code, request=req, json={"ok": True})


def _make_text_response(path: str, text: str, status_code: int = 200) -> httpx.Response:
    req = httpx.Request("GET", f"https://example.org{path}")
    return httpx.Response(status_code, request=req, text=text)


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

    # The session cookie is now set on the client instance each iteration
    # (httpx 0.28 deprecates per-request cookies=), so the reauth token is
    # picked up on the retry. Assert the cookie was refreshed old -> new.
    cookie_sets = [
        c.args for c in mock_httpx.cookies.set.call_args_list if c.args[:1] == ("JSESSIONID",)
    ]
    assert cookie_sets[0] == ("JSESSIONID", "old-token")
    assert cookie_sets[1] == ("JSESSIONID", "new-token")


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


def test_request_raises_resource_not_found_on_404_with_status_details(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        username="user",
        password="pass",
        session_token="token",
        max_retries=0,
    )

    mock_httpx = MagicMock()
    mock_httpx.request = MagicMock(return_value=_make_response(404))
    monkeypatch.setattr(client, "_get_client", MagicMock(return_value=mock_httpx))

    with pytest.raises(ResourceNotFoundError) as excinfo:
        client.get("/data/user")

    err = excinfo.value
    assert err.details["status_code"] == 404
    assert err.details["method"] == "GET"
    assert err.details["path"] == "/data/user"


def test_whoami_uses_current_username_endpoint_and_preserves_username_hint(monkeypatch):
    client = XNATClient(
        base_url="https://example.org",
        username="Ricky_Wong",
        session_token="token",
    )

    def fake_get(path: str, **kwargs):
        if path == "/xapi/users/username":
            return _make_text_response(path, "ricky_wong")
        raise AssertionError(f"unexpected path: {path}")

    def fake_get_json(path: str, **kwargs):
        if path == "/xapi/users/ricky_wong":
            return {
                "username": "ricky_wong",
                "firstName": "Ricky",
                "lastName": "Wong",
                "email": "ricky@example.org",
                "enabled": True,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "get_json", fake_get_json)

    result = client.whoami()

    # model_dump() is the shape the whoami dict always had -- pinned so the
    # typed return keeps rendering identically downstream.
    assert result.model_dump() == {
        "username": "Ricky_Wong",
        "firstname": "Ricky",
        "lastname": "Wong",
        "email": "ricky@example.org",
        "enabled": True,
    }


def test_whoami_normalizes_explicit_null_name_and_email_fields(monkeypatch):
    """XNAT sends explicit null firstName/lastName/email for service/API
    accounts that never had a name set -- normal output, not malformed. The
    real `enabled` value must survive, not get discarded/reinvented.
    """
    client = XNATClient(
        base_url="https://example.org",
        username="svc_account",
        session_token="token",
    )

    def fake_get(path: str, **kwargs):
        if path == "/xapi/users/username":
            return _make_text_response(path, "svc_account")
        raise AssertionError(f"unexpected path: {path}")

    def fake_get_json(path: str, **kwargs):
        if path == "/xapi/users/svc_account":
            return {
                "username": "svc_account",
                "firstName": None,
                "lastName": None,
                "email": None,
                "enabled": False,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "get_json", fake_get_json)

    result = client.whoami()

    assert result.model_dump() == {
        "username": "svc_account",
        "firstname": "",
        "lastname": "",
        "email": "",
        "enabled": False,
    }


def test_whoami_missing_payload_username_falls_back_without_losing_other_fields(monkeypatch):
    """A payload with a missing/null `username` must not be discarded
    wholesale: the already-resolved username fills in, and the payload's
    real name fields still come through.
    """
    client = XNATClient(
        base_url="https://example.org",
        username="jdoe",
        session_token="token",
    )

    def fake_get(path: str, **kwargs):
        if path == "/xapi/users/username":
            return _make_text_response(path, "jdoe")
        raise AssertionError(f"unexpected path: {path}")

    def fake_get_json(path: str, **kwargs):
        if path == "/xapi/users/jdoe":
            return {
                "username": None,
                "firstName": "Jane",
                "lastName": "Doe",
                "enabled": True,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "get_json", fake_get_json)

    result = client.whoami()

    assert result.username == "jdoe"
    assert result.firstname == "Jane"
    assert result.enabled is True


def test_whoami_defaults_enabled_to_false_for_a_genuinely_unusable_payload(monkeypatch):
    """When `enabled` itself is unparseable (the one case the field
    validators can't absorb), the fallback must pick a defensible default
    rather than inventing `enabled=True` for an account nothing confirms is
    active.
    """
    client = XNATClient(
        base_url="https://example.org",
        username="jdoe",
        session_token="token",
    )

    def fake_get(path: str, **kwargs):
        if path == "/xapi/users/username":
            return _make_text_response(path, "jdoe")
        raise AssertionError(f"unexpected path: {path}")

    def fake_get_json(path: str, **kwargs):
        if path == "/xapi/users/jdoe":
            return {
                "username": "jdoe",
                "firstName": "Jane",
                # Not bool-coercible -- forces ValidationError even after
                # the name/email field validators absorb everything else.
                "enabled": {"unexpected": "shape"},
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "get_json", fake_get_json)

    result = client.whoami()

    assert result.username == "jdoe"
    assert result.enabled is False


def test_whoami_falls_back_to_username_hint_when_current_user_endpoints_unavailable(
    monkeypatch,
):
    client = XNATClient(
        base_url="https://example.org",
        username="Ricky_Wong",
        session_token="token",
    )

    def fake_get(path: str, **kwargs):
        # get()'s documented contract is that only XNATCtlError subtypes ever
        # escape it -- a real "endpoints unavailable" failure looks like this,
        # not a raw RuntimeError.
        raise NetworkError("https://example.org", "connection refused")

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "get_json", MagicMock(side_effect=AssertionError("unused")))

    result = client.whoami()

    assert result.model_dump() == {
        "username": "Ricky_Wong",
        "firstname": "",
        "lastname": "",
        "email": "",
        "enabled": True,
    }
