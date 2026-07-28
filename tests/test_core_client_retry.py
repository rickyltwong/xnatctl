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

from xnatctl.core.client import (
    RETRY_BACKOFF_BASE,
    XNATClient,
)
from xnatctl.core.client import (
    RETRYABLE_STATUS_CODES as CLIENT_RETRYABLE_STATUS_CODES,
)
from xnatctl.core.exceptions import (
    ClientRequestError,
    NetworkError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    XNATCtlError,
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


def test_502_exhaustion_raises_retry_exhausted(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502), max_retries=1)

    with pytest.raises(RetryExhaustedError) as exc:
        # ROB-03: status exhaustion now raises a typed RetryExhaustedError
        # wrapping a ServerError, never a raw httpx.HTTPStatusError.
        client._request("GET", "/data/projects")

    assert isinstance(exc.value.last_error, ServerError)
    assert exc.value.last_error.status_code == 502
    assert len(calls) == 2  # initial + 1 retry
    assert sleeps == [RETRY_BACKOFF_BASE**1]  # [2]


def test_409_is_not_retried_and_raises_client_request_error(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(409), max_retries=3)

    with pytest.raises(ClientRequestError) as exc:
        # ROB-03: a non-retryable 4xx is a typed ClientRequestError, not raw httpx.
        client._request("POST", "/data/services/import")

    assert exc.value.status_code == 409
    assert exc.value.method == "POST"
    assert exc.value.path == "/data/services/import"
    assert len(calls) == 1  # no retry
    assert sleeps == []


def test_500_is_retried_then_typed_on_exhaustion(sleeps: list[float]) -> None:
    """ROB-03 adds 500 to the retryable set (was non-retryable)."""
    client, calls = _client(_status_sequence(500, 500, 500, 500), max_retries=3)

    with pytest.raises(RetryExhaustedError) as exc:
        client._request("GET", "/data/projects")

    assert isinstance(exc.value.last_error, ServerError)
    assert exc.value.last_error.status_code == 500
    assert len(calls) == 4  # attempts 0..3
    assert sleeps == [RETRY_BACKOFF_BASE**1, RETRY_BACKOFF_BASE**2, RETRY_BACKOFF_BASE**3]


def test_500_then_200_recovers(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(500, 200), max_retries=3)

    resp = client._request("GET", "/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 2


def test_429_honors_retry_after_header(sleeps: list[float]) -> None:
    """ROB-03: 429 is retryable and a bounded integer Retry-After overrides backoff."""

    def handler(request: httpx.Request) -> httpx.Response:
        if len(seen) == 0:
            seen.append(1)
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"ok": True})

    seen: list[int] = []
    client, calls = _client(handler, max_retries=3)

    resp = client._request("GET", "/data/projects")

    assert resp.status_code == 200
    assert sleeps == [7.0]  # Retry-After, not the exponential step (which would be 2)


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


# =============================================================================
# Transport-exception contract (ROB-03)
#
# httpx's transport errors are NOT all ConnectError/TimeoutException subclasses;
# ReadError, WriteError, RemoteProtocolError and ProxyError sit beside them and
# used to escape XNATClient raw, surfacing as "Unexpected error:" with no retry.
# =============================================================================


def _raising(exc_factory: Callable[[httpx.Request], Exception]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_factory(request)

    return handler


@pytest.mark.parametrize(
    "exc_name",
    ["ReadError", "WriteError", "RemoteProtocolError", "ProxyError"],
)
def test_transient_transport_errors_are_retried_and_typed(
    exc_name: str, sleeps: list[float]
) -> None:
    """A socket dying mid-exchange retries like a read timeout, never leaks raw."""
    exc_cls = getattr(httpx, exc_name)
    client, calls = _client(_raising(lambda r: exc_cls("boom", request=r)), max_retries=2)

    with pytest.raises(RetryExhaustedError):
        client._request("GET", "/data/projects")

    assert len(calls) == 3, "initial attempt plus two retries"
    assert sleeps == [RETRY_BACKOFF_BASE**1, RETRY_BACKOFF_BASE**2]


@pytest.mark.parametrize("exc_name", ["UnsupportedProtocol", "DecodingError"])
def test_permanent_transport_errors_fail_fast(exc_name: str, sleeps: list[float]) -> None:
    """A bad scheme or an undecodable body will not fix itself; do not retry."""
    exc_cls = getattr(httpx, exc_name)
    client, calls = _client(_raising(lambda r: exc_cls("boom", request=r)), max_retries=3)

    with pytest.raises(NetworkError):
        client._request("GET", "/data/projects")

    assert len(calls) == 1, "permanent transport failures must not burn retries"
    assert sleeps == []


def test_transient_transport_error_recovers_when_it_stops(sleeps: list[float]) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadError("dropped", request=request)
        return httpx.Response(200, json={"ok": True})

    client, calls = _client(handler, max_retries=3)

    assert client._request("GET", "/data/projects").json() == {"ok": True}
    assert len(calls) == 3


@pytest.mark.parametrize(
    "exc_name",
    [
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
        "DecodingError",
    ],
)
def test_no_httpx_exception_escapes_the_client(exc_name: str, sleeps: list[float]) -> None:
    """The ROB-03 contract, asserted as a guard over the whole httpx error family.

    Add any newly-handled httpx error class here; a raw leak fails this test
    rather than silently reaching the CLI as "Unexpected error:".
    """
    exc_cls = getattr(httpx, exc_name)
    client, _ = _client(_raising(lambda r: exc_cls("boom", request=r)), max_retries=1)

    with pytest.raises(XNATCtlError):
        client._request("GET", "/data/projects")


@pytest.mark.parametrize("exc_name", ["ReadError", "RemoteProtocolError", "UnsupportedProtocol"])
def test_authenticate_maps_transport_errors_too(exc_name: str) -> None:
    """authenticate() bypasses the retry loop and had the identical gap."""
    exc_cls = getattr(httpx, exc_name)
    client, _ = _client(_raising(lambda r: exc_cls("boom", request=r)))
    client.username, client.password = "user", "pass"

    with pytest.raises(NetworkError):
        client.authenticate()


def test_retryable_status_policy_has_a_single_source() -> None:
    """uploads extends the client's set rather than redefining it (ROB-03 step 3)."""
    from xnatctl.services.uploads import (
        RETRYABLE_STATUS_CODES as UPLOAD_CODES,
    )
    from xnatctl.services.uploads import (
        UPLOAD_ONLY_RETRYABLE_STATUS_CODES,
    )

    assert CLIENT_RETRYABLE_STATUS_CODES <= UPLOAD_CODES
    assert UPLOAD_CODES - CLIENT_RETRYABLE_STATUS_CODES == UPLOAD_ONLY_RETRYABLE_STATUS_CODES
    assert 400 in UPLOAD_CODES, "the XNAT import race stays upload-only"
    assert 400 not in CLIENT_RETRYABLE_STATUS_CODES, "a 400 is a real client error on the core path"
