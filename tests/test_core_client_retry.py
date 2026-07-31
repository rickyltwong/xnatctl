"""Transport-level tests for XNATClient._request retry semantics.

Started as characterization tests locking the then-current contract; the
warts they pinned have since been fixed and the assertions flipped:

* status exhaustion raises RetryExhaustedError, and no httpx error of
  any kind escapes the client.
* read-phase failures are only retried for idempotent methods, and
  backoff is full-jitter rather than a fixed 2**n progression.
* Retry-After is honoured on 429/503 in both the delta-seconds and
  HTTP-date forms, bounded by a cap.

Pagination, convenience methods and the public transport seam live in
``tests/test_core_client_http.py``.

All sleeps are monkeypatched away, so the suite stays wall-clock cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from xnatctl.core.client import (
    _MAX_RETRY_AFTER_SECONDS,
    IDEMPOTENT_METHODS,
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
from xnatctl.core.exceptions import (
    TimeoutError as XNATTimeoutError,
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


def assert_jittered_backoff(sleeps: list[float], expected_attempts: int) -> None:
    """Assert full-jitter backoff: one sleep per retry, each within its window.

    Jitter replaced the fixed ``2**(attempt+1)`` progression with
    ``uniform(0, 2**(attempt+1))`` so parallel workers stop retrying in lockstep.
    Exact values are therefore no longer assertable -- the ceiling is.
    """
    assert len(sleeps) == expected_attempts, f"expected {expected_attempts} sleeps, got {sleeps}"
    for i, slept in enumerate(sleeps):
        ceiling = float(RETRY_BACKOFF_BASE ** (i + 1))
        assert 0.0 <= slept <= ceiling, f"sleep {i} = {slept} outside [0, {ceiling}]"


def test_502_502_then_200_succeeds_after_two_retries(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502, 200), max_retries=3)

    resp = client._request("GET", "/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 3  # two retries, then success
    assert_jittered_backoff(sleeps, 2)


def test_502_exhaustion_raises_retry_exhausted(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502), max_retries=1)

    with pytest.raises(RetryExhaustedError) as exc:
        # Status exhaustion raises a typed RetryExhaustedError
        # wrapping a ServerError, never a raw httpx.HTTPStatusError.
        client._request("GET", "/data/projects")

    assert isinstance(exc.value.last_error, ServerError)
    assert exc.value.last_error.status_code == 502
    assert len(calls) == 2  # initial + 1 retry
    assert_jittered_backoff(sleeps, 1)


def test_409_is_not_retried_and_raises_client_request_error(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(409), max_retries=3)

    with pytest.raises(ClientRequestError) as exc:
        # A non-retryable 4xx is a typed ClientRequestError, not raw httpx.
        client._request("POST", "/data/services/import")

    assert exc.value.status_code == 409
    assert exc.value.method == "POST"
    assert exc.value.path == "/data/services/import"
    assert len(calls) == 1  # no retry
    assert sleeps == []


def test_500_is_retried_then_typed_on_exhaustion(sleeps: list[float]) -> None:
    """500 is in the retryable set (it used to be non-retryable)."""
    client, calls = _client(_status_sequence(500, 500, 500, 500), max_retries=3)

    with pytest.raises(RetryExhaustedError) as exc:
        client._request("GET", "/data/projects")

    assert isinstance(exc.value.last_error, ServerError)
    assert exc.value.last_error.status_code == 500
    assert len(calls) == 4  # attempts 0..3
    assert_jittered_backoff(sleeps, 3)


def test_500_then_200_recovers(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(500, 200), max_retries=3)

    resp = client._request("GET", "/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 2


def test_429_honors_retry_after_header(sleeps: list[float]) -> None:
    """429 is retryable and a bounded integer Retry-After overrides backoff."""

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
    assert_jittered_backoff(sleeps, 3)


def test_post_read_timeout_is_not_retried(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client, calls = _client(handler, max_retries=2)

    # Deliberately flipped from the old behaviour: a read-phase timeout on a
    # non-idempotent POST is no
    # longer retried, because the server has already seen the request and a
    # retry could archive twice or launch a pipeline twice.
    with pytest.raises(XNATTimeoutError) as exc:
        client._request("POST", "/data/services/import")

    assert len(calls) == 1, "POST must not be retried after send"
    assert sleeps == []
    assert "may have partially executed" in str(exc.value)


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
# Transport-exception contract
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
    assert_jittered_backoff(sleeps, 2)


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
    """The no-escape contract, asserted as a guard over the whole httpx error family.

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
    """uploads extends the client's set rather than redefining it."""
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


# =============================================================================
# Idempotency-aware retries and jitter
# =============================================================================


@pytest.mark.parametrize("method", sorted(IDEMPOTENT_METHODS))
def test_idempotent_methods_retry_read_timeouts(method: str, sleeps: list[float]) -> None:
    """The request may have run, but repeating it is harmless for these verbs."""
    client, calls = _client(_raising(lambda r: httpx.ReadTimeout("t", request=r)), max_retries=2)

    with pytest.raises(RetryExhaustedError):
        client._request(method, "/data/projects")

    assert len(calls) == 3
    assert_jittered_backoff(sleeps, 2)


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_non_idempotent_methods_fail_fast_on_read_timeout(method: str, sleeps: list[float]) -> None:
    """A double archive or a double pipeline launch is worse than a failed call."""
    client, calls = _client(_raising(lambda r: httpx.ReadTimeout("t", request=r)), max_retries=3)

    with pytest.raises(XNATTimeoutError) as exc:
        client._request(method, "/data/services/archive")

    assert len(calls) == 1
    assert sleeps == []
    assert "may have partially executed" in str(exc.value)


def test_method_matching_is_case_insensitive(sleeps: list[float]) -> None:
    client, calls = _client(_raising(lambda r: httpx.ReadTimeout("t", request=r)), max_retries=2)

    with pytest.raises(RetryExhaustedError):
        client._request("get", "/data/projects")

    assert len(calls) == 3, "lowercase 'get' is still idempotent"


def test_retry_non_idempotent_opt_in_restores_retries(sleeps: list[float]) -> None:
    """The escape hatch exists for call sites that prove a POST is safe to repeat."""
    client, calls = _client(_raising(lambda r: httpx.ReadTimeout("t", request=r)), max_retries=2)

    with pytest.raises(RetryExhaustedError):
        client._request("POST", "/data/services/refresh", retry_non_idempotent=True)

    assert len(calls) == 3


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_connect_phase_failures_still_retry_for_any_method(
    method: str, sleeps: list[float]
) -> None:
    """The request never reached the server, so a retry cannot duplicate anything."""
    client, calls = _client(_raising(lambda r: httpx.ConnectError("refused", request=r)))

    with pytest.raises(RetryExhaustedError):
        client._request(method, "/data/services/archive")

    assert len(calls) == 4, "connect-phase retries are method-agnostic"


def test_non_idempotent_transport_error_after_send_fails_fast(sleeps: list[float]) -> None:
    """A dropped socket mid-POST has the same ambiguity as a read timeout."""
    client, calls = _client(_raising(lambda r: httpx.ReadError("dropped", request=r)))

    with pytest.raises(NetworkError) as exc:
        client._request("POST", "/data/services/archive")

    assert len(calls) == 1
    assert "may have partially executed" in str(exc.value)


def test_backoff_is_jittered_not_a_fixed_progression(sleeps: list[float]) -> None:
    """Two runs must not produce identical sleeps, or workers retry in lockstep."""
    runs = []
    for _ in range(6):
        sleeps.clear()
        client, _calls = _client(_status_sequence(*([503] * 4)), max_retries=3)
        with pytest.raises(RetryExhaustedError):
            client._request("GET", "/data/projects")
        runs.append(tuple(sleeps))

    assert len({r for r in runs}) > 1, "jitter should vary across runs"
    for run in runs:
        assert_jittered_backoff(list(run), 3)


# =============================================================================
# Retry-After parsing
# =============================================================================


def _retry_after_then_ok(value: str, status: int = 429) -> Handler:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not seen:
            seen.append(1)
            return httpx.Response(status, headers={"Retry-After": value}, json={})
        return httpx.Response(200, json={"ok": True})

    return handler


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_delta_seconds_is_used_verbatim(status: int, sleeps: list[float]) -> None:
    client, _ = _client(_retry_after_then_ok("7", status), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert sleeps == [7.0], "an explicit instruction is not jittered"


def test_retry_after_http_date_form_is_parsed(sleeps: list[float]) -> None:
    """RFC 9110 allows an HTTP-date; real proxies emit it."""
    when = datetime.now(UTC) + timedelta(seconds=45)
    client, _ = _client(_retry_after_then_ok(format_datetime(when, usegmt=True)), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert len(sleeps) == 1
    assert 30 <= sleeps[0] <= 50, f"expected ~45s from the HTTP-date, got {sleeps[0]}"


def test_retry_after_http_date_in_the_past_means_retry_now(sleeps: list[float]) -> None:
    when = datetime.now(UTC) - timedelta(seconds=60)
    client, _ = _client(_retry_after_then_ok(format_datetime(when, usegmt=True)), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert sleeps == [0.0], "a past date must not become a negative sleep"


@pytest.mark.parametrize("value", ["not-a-number", "", "12.5", "-5"])
def test_malformed_retry_after_falls_back_to_jittered_backoff(
    value: str, sleeps: list[float]
) -> None:
    client, _ = _client(_retry_after_then_ok(value), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert_jittered_backoff(sleeps, 1)


def test_retry_after_beyond_the_cap_falls_back_to_backoff(sleeps: list[float]) -> None:
    """A server asking for an hour is telling us to give up, not to sleep an hour."""
    client, _ = _client(_retry_after_then_ok(str(_MAX_RETRY_AFTER_SECONDS + 1)), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert_jittered_backoff(sleeps, 1)


def test_retry_after_exactly_at_the_cap_is_honoured(sleeps: list[float]) -> None:
    client, _ = _client(_retry_after_then_ok(str(_MAX_RETRY_AFTER_SECONDS)), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert sleeps == [float(_MAX_RETRY_AFTER_SECONDS)]


def test_retry_after_ignored_on_statuses_that_do_not_define_it(sleeps: list[float]) -> None:
    """Only 429/503 carry Retry-After semantics; a 502 uses normal backoff."""
    client, _ = _client(_retry_after_then_ok("7", status=502), max_retries=3)

    assert client._request("GET", "/data/projects").status_code == 200
    assert_jittered_backoff(sleeps, 1)
