"""Split the scalar timeout so connect fails in seconds, not hours."""

from __future__ import annotations

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import RequestTimeoutError as XNATTimeoutError
from xnatctl.core.timeouts import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    build_httpx_timeout,
)


def test_build_httpx_timeout_short_connect_long_read() -> None:
    t = build_httpx_timeout(DEFAULT_HTTP_TIMEOUT_SECONDS)
    assert t.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert t.read == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert t.write == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert t.connect != t.read  # the whole point: they are no longer identical


def test_build_httpx_timeout_none_falls_back_to_default_read() -> None:
    t = build_httpx_timeout(None)
    assert t.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert t.read == DEFAULT_HTTP_TIMEOUT_SECONDS


def test_client_default_timeout_has_short_connect() -> None:
    client = XNATClient(base_url="https://example.org")
    httpx_client = client._get_client()
    assert httpx_client.timeout.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert httpx_client.timeout.read == DEFAULT_HTTP_TIMEOUT_SECONDS


def test_client_custom_scalar_timeout_still_short_connect() -> None:
    """A profile that sets a small timeout still gets the fast connect ceiling."""
    client = XNATClient(base_url="https://example.org", timeout=60)
    httpx_client = client._get_client()
    assert httpx_client.timeout.connect == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert httpx_client.timeout.read == 60


def test_per_request_timeout_override_keeps_short_connect() -> None:
    """A per-request read override must NOT re-flatten connect to the scalar."""

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx exposes the effective per-request timeout on the outgoing request.
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == DEFAULT_CONNECT_TIMEOUT_SECONDS
        assert timeout["read"] == 120
        return httpx.Response(200, json={"ok": True})

    client = XNATClient(base_url="https://example.org")
    client._client = httpx.Client(
        base_url="https://example.org", transport=httpx.MockTransport(handler)
    )

    resp = client.get("/data/projects", timeout=120)
    assert resp.status_code == 200


def test_connect_timeout_raises_typed_error_and_fails_fast() -> None:
    """A connect-phase timeout raises the typed TimeoutError and does NOT retry."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client = XNATClient(base_url="https://example.org", max_retries=3)
    client._client = httpx.Client(
        base_url="https://example.org", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(XNATTimeoutError) as exc:
        client.get("/data/projects")

    # Fail fast: one attempt, no retry storm against a blackholed host.
    assert len(calls) == 1
    assert exc.value.timeout == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert "example.org" in str(exc.value)


def test_authenticate_connect_timeout_raises_typed_error() -> None:
    """authenticate() honors the same typed, fail-fast connect-timeout contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client = XNATClient(base_url="https://example.org", username="u", password="p")
    client._client = httpx.Client(
        base_url="https://example.org", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(XNATTimeoutError):
        client.authenticate()


def test_read_timeout_still_generic_network_error_bucket(monkeypatch) -> None:
    """Read-phase timeouts stay in the retried NetworkError bucket (not TimeoutError)."""
    from xnatctl.core.exceptions import RetryExhaustedError

    monkeypatch.setattr("xnatctl.core.client.time.sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = XNATClient(base_url="https://example.org", max_retries=1)
    client._client = httpx.Client(
        base_url="https://example.org", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(RetryExhaustedError) as exc:
        client.get("/data/projects")
    # Distinct from the connect-timeout path: not a bare TimeoutError.
    assert not isinstance(exc.value, XNATTimeoutError)
