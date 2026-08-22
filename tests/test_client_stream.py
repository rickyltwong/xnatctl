"""Transport-level tests for XNATClient.stream.

The public streaming entry point must give streamed reads the same contract as
``_request``: basic-auth fallback, the session cookie sent per call (never on
the shared jar), typed-error mapping, and the retry ladder -- all before the
body is yielded. These drive a real ``XNATClient`` wired to
``httpx.MockTransport`` at the ``_client`` seam, the same pattern as
``tests/test_core_client_retry.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    PermissionDeniedError,
    ResourceNotFoundError,
    RetryExhaustedError,
    SessionExpiredError,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps and return immediately."""
    recorded: list[float] = []
    monkeypatch.setattr("xnatctl.core.client.time.sleep", lambda s: recorded.append(s))
    return recorded


def _client(
    handler: Handler,
    *,
    max_retries: int = 3,
    username: str | None = None,
    password: str | None = None,
    session_token: str | None = None,
) -> tuple[XNATClient, list[httpx.Request]]:
    """Build an XNATClient whose transport records every request it sees."""
    calls: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = XNATClient(
        base_url="https://xnat.example.org",
        max_retries=max_retries,
        username=username,
        password=password,
        session_token=session_token,
    )
    client._client = httpx.Client(
        base_url=client.base_url, transport=httpx.MockTransport(_recording)
    )
    return client, calls


def _status_sequence(*codes: int) -> Handler:
    """Return a handler yielding the given status codes in order."""
    seq = iter(codes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(seq), content=b"body")

    return handler


def test_basic_auth_used_when_unauthenticated() -> None:
    """A username/password client with no session token streams via basic auth."""
    client, calls = _client(
        lambda r: httpx.Response(200, content=b"ok"),
        username="user",
        password="pass",
    )

    with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert len(calls) == 1
    assert "authorization" in calls[0].headers
    assert calls[0].headers["authorization"].startswith("Basic ")
    assert "cookie" not in calls[0].headers


def test_session_token_sent_as_cookie() -> None:
    """A session-token client sends JSESSIONID as a per-call Cookie header."""
    client, calls = _client(
        lambda r: httpx.Response(200, content=b"ok"),
        session_token="TOKEN123",
    )

    with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert calls[0].headers.get("cookie") == "JSESSIONID=TOKEN123"
    # Cookie is sent explicitly, never left mutated on the shared jar.
    assert "JSESSIONID" not in dict(client._client.cookies)  # type: ignore[union-attr]


def test_401_raises_session_expired() -> None:
    client, _ = _client(_status_sequence(401))

    with pytest.raises(SessionExpiredError):  # noqa: SIM117
        with client.stream("GET", "/data/projects"):
            pass


def test_403_raises_permission_denied() -> None:
    client, _ = _client(_status_sequence(403))

    with pytest.raises(PermissionDeniedError):  # noqa: SIM117
        with client.stream("GET", "/data/projects"):
            pass


def test_404_raises_resource_not_found() -> None:
    client, _ = _client(_status_sequence(404))

    with pytest.raises(ResourceNotFoundError):  # noqa: SIM117
        with client.stream("GET", "/data/projects"):
            pass


def test_503_then_200_succeeds_after_retry(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(503, 200), max_retries=3)

    with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert len(calls) == 2  # one retry, then success
    assert len(sleeps) == 1


def test_persistent_503_raises_retry_exhausted(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(503, 503), max_retries=1)

    with pytest.raises(RetryExhaustedError):  # noqa: SIM117
        with client.stream("GET", "/data/projects"):
            pass

    assert len(calls) == 2  # initial + 1 retry
    assert len(sleeps) == 1


def test_body_arrives_incrementally() -> None:
    """The yielded response streams its body through iter_bytes."""
    payload = b"".join(bytes([i % 256]) for i in range(4096))
    client, _ = _client(lambda r: httpx.Response(200, content=payload))

    chunks: list[bytes] = []
    with client.stream("GET", "/data/files") as resp:
        for chunk in resp.iter_bytes(chunk_size=256):
            chunks.append(chunk)

    assert len(chunks) > 1
    assert b"".join(chunks) == payload


def test_parallel_401s_trigger_exactly_one_reauth() -> None:
    """Two streams hitting one session expiry re-authenticate once, not twice.

    Each extra login opens another server-side session; under a shared
    service account, a per-worker reauth stampede exhausts the concurrent
    session limit. The reauth lock must make the second thread reuse the
    token the first one fetched.
    """
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    auth_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_posts
        if request.url.path == "/data/JSESSION":
            with lock:
                auth_posts += 1
            return httpx.Response(200, text="FRESH")
        if request.headers.get("cookie") == "JSESSIONID=FRESH":
            return httpx.Response(200, content=b"ok")
        # Stale token: hold the 401 until both workers are in flight, so both
        # observe the expiry before either can refresh.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - only on failure
            pass
        return httpx.Response(401)

    client, _ = _client(
        handler,
        username="user",
        password="pass",
        session_token="STALE",
    )
    client.auto_reauth = True

    errors: list[Exception] = []

    def worker() -> None:
        try:
            with client.stream("GET", "/data/projects") as resp:
                assert resp.status_code == 200
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert auth_posts == 1


def test_concurrent_streams_keep_their_own_auth() -> None:
    """Two threads streaming over one client each carry the session cookie.

    The per-call Cookie header (rather than a mutated shared jar) is what makes
    concurrent streaming safe; here it must not drop or corrupt auth under
    contention.
    """
    seen_cookies: list[str | None] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen_cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, content=b"ok")

    client, _ = _client(handler, session_token="SHARED")

    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(10):
                with client.stream("GET", "/data/projects") as resp:
                    assert resp.status_code == 200
        except Exception as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert seen_cookies  # every request carried the cookie
    assert all(c == "JSESSIONID=SHARED" for c in seen_cookies)
