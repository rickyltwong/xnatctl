"""Transport-level characterization tests for XNATClient._request (ROB-14).

These lock the CURRENT retry/backoff contract via ``httpx.MockTransport`` before
ROB-03 (typed status-exhaustion errors) and ROB-09 (idempotency-aware retries)
change it. Assertions marked ``# current behavior`` deliberately capture today's
warts (raw ``httpx.HTTPStatusError`` on status exhaustion, POSTs retried after a
read timeout); flip them in the same PR as the behavior change.

All sleeps are monkeypatched away, so the suite stays wall-clock cheap.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from xnatctl.core.client import RETRY_BACKOFF_BASE, XNATClient
from xnatctl.core.exceptions import (
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerUnreachableError,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps and return immediately."""
    recorded: list[float] = []
    monkeypatch.setattr("xnatctl.core.client.time.sleep", lambda s: recorded.append(s))
    return recorded


def _client(handler: Handler, *, max_retries: int = 3) -> tuple[XNATClient, list[httpx.Request]]:
    """Build an XNATClient whose transport is a MockTransport wrapping ``handler``.

    Returns the client plus a list that records every request the transport saw.
    """
    calls: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = XNATClient(base_url="https://xnat.example.org", max_retries=max_retries)
    # Inject the mock transport without touching the public constructor: _request
    # calls _get_client(), which returns this pre-seeded client as-is.
    client._client = httpx.Client(
        base_url=client.base_url, transport=httpx.MockTransport(_recording)
    )
    return client, calls


def _status_sequence(*codes: int) -> Handler:
    """Return a handler that yields the given status codes in order."""
    seq = iter(codes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(seq), json={"ok": True})

    return handler


def test_502_502_then_200_succeeds_after_two_retries(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502, 200), max_retries=3)

    resp = client._request("GET", "/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 3  # two retries, then success
    assert sleeps == [RETRY_BACKOFF_BASE**1, RETRY_BACKOFF_BASE**2]  # [2, 4]


def test_502_exhaustion_raises_raw_httpstatuserror(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502), max_retries=1)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        # current behavior: status exhaustion falls through to raise_for_status()
        # and leaks a raw httpx error. ROB-03 will make this RetryExhaustedError.
        client._request("GET", "/data/projects")

    assert exc.value.response.status_code == 502
    assert len(calls) == 2  # initial + 1 retry
    assert sleeps == [RETRY_BACKOFF_BASE**1]  # [2]


def test_409_is_not_retried_and_leaks_raw_httpstatuserror(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(409), max_retries=3)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        # current behavior: non-retryable 4xx reaches raise_for_status() raw.
        # ROB-03 will map it to a typed ClientRequestError.
        client._request("POST", "/data/services/import")

    assert exc.value.response.status_code == 409
    assert len(calls) == 1  # no retry
    assert sleeps == []


def test_connect_error_exhausts_to_retry_exhausted(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, calls = _client(handler, max_retries=3)

    with pytest.raises(RetryExhaustedError) as exc:
        client._request("GET", "/data/projects")

    assert exc.value.attempts == client.max_retries + 1  # 4
    assert isinstance(exc.value.last_error, ServerUnreachableError)
    assert len(calls) == 4  # attempts 0..3
    assert sleeps == [RETRY_BACKOFF_BASE**1, RETRY_BACKOFF_BASE**2, RETRY_BACKOFF_BASE**3]


def test_post_read_timeout_is_retried_today(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client, calls = _client(handler, max_retries=2)

    with pytest.raises(RetryExhaustedError):
        # current behavior: a read-phase timeout on a non-idempotent POST is
        # retried, risking duplicate side effects. ROB-09 will stop retrying
        # POSTs after send and raise a typed timeout immediately.
        client._request("POST", "/data/services/import")

    assert len(calls) == client.max_retries + 1  # 3 -- POST retried
    assert sleeps == [RETRY_BACKOFF_BASE**1, RETRY_BACKOFF_BASE**2]


def test_404_raises_resource_not_found_immediately(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(404), max_retries=3)

    with pytest.raises(ResourceNotFoundError):
        client._request("GET", "/data/projects/NOPE")

    assert len(calls) == 1  # typed immediately, no retry
    assert sleeps == []


def test_403_raises_permission_denied_immediately(sleeps: list[float]) -> None:
    from xnatctl.core.exceptions import PermissionDeniedError

    client, calls = _client(_status_sequence(403), max_retries=3)

    with pytest.raises(PermissionDeniedError):
        client._request("DELETE", "/data/projects/LOCKED")

    assert len(calls) == 1
    assert sleeps == []
