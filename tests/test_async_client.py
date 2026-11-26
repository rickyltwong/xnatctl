from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from xnatctl.core.async_client import AsyncXNATClient
from xnatctl.core.async_transport import request as async_request
from xnatctl.core.exceptions import (
    AuthenticationError,
    ClientRequestError,
    NetworkError,
    PermissionDeniedError,
    ResourceNotFoundError,
    RetryExhaustedError,
    ServerError,
    ServerUnreachableError,
    SessionExpiredError,
)
from xnatctl.core.exceptions import (
    RequestTimeoutError as XNATTimeoutError,
)
from xnatctl.core.timeouts import DEFAULT_CONNECT_TIMEOUT_SECONDS

pytestmark = pytest.mark.asyncio

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("xnatctl.core.async_transport.asyncio.sleep", fake_sleep)
    return recorded


def _client(
    handler: Handler,
    *,
    max_retries: int = 3,
    username: str | None = None,
    password: str | None = None,
    session_token: str | None = None,
    auto_reauth: bool = False,
) -> tuple[AsyncXNATClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    async def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = AsyncXNATClient(
        base_url="https://xnat.example.org",
        max_retries=max_retries,
        username=username,
        password=password,
        session_token=session_token,
        auto_reauth=auto_reauth,
        transport=httpx.MockTransport(recording),
    )
    return client, calls


def _status_sequence(*codes: int) -> Handler:
    seq = iter(codes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(seq), json={"ok": True})

    return handler


async def test_transport_kwarg_is_used_for_requests() -> None:
    client, calls = _client(lambda r: httpx.Response(200, json={"ok": True}))

    resp = await client.get("/data/projects")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(calls) == 1
    assert calls[0].url.path == "/data/projects"


async def test_get_json_injects_format_json() -> None:
    client, calls = _client(lambda r: httpx.Response(200, json={"a": 1}))

    data = await client.get_json("/data/projects")

    assert data == {"a": 1}
    assert calls[0].url.params["format"] == "json"


async def test_get_json_preserves_caller_params() -> None:
    client, calls = _client(lambda r: httpx.Response(200, json={}))

    await client.get_json("/data/projects", params={"columns": "ID"})

    assert calls[0].url.params["columns"] == "ID"
    assert calls[0].url.params["format"] == "json"


async def test_basic_auth_used_when_unauthenticated() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, json={}),
        username="user",
        password="pass",
    )

    await client.get("/data/projects")

    assert calls[0].headers.get("authorization", "").startswith("Basic ")
    assert "cookie" not in calls[0].headers


async def test_session_token_sent_as_cookie() -> None:
    client, calls = _client(lambda r: httpx.Response(200, json={}), session_token="TOKEN123")

    await client.get("/data/projects")

    assert "JSESSIONID=TOKEN123" in calls[0].headers.get("cookie", "")


async def test_session_token_takes_precedence_over_basic_auth() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, json={}),
        username="user",
        password="pass",
        session_token="TOK",
    )

    await client.get("/data/projects")

    assert "authorization" not in calls[0].headers


async def test_stream_basic_auth_used_when_unauthenticated() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, content=b"ok"),
        username="user",
        password="pass",
    )

    async with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert calls[0].headers.get("authorization", "").startswith("Basic ")


async def test_stream_session_token_sent_as_cookie() -> None:
    client, calls = _client(lambda r: httpx.Response(200, content=b"ok"), session_token="TOKEN123")

    async with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert calls[0].headers.get("cookie") == "JSESSIONID=TOKEN123"


async def test_get_401_raises_session_expired_without_auto_reauth() -> None:
    client, _ = _client(_status_sequence(401), session_token="stale")

    with pytest.raises(SessionExpiredError):
        await client.get("/data/projects")


async def test_get_auto_reauth_retries_once_on_401() -> None:
    responses = iter([httpx.Response(401), httpx.Response(200, json={"ok": True})])
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/data/JSESSION":
            posts += 1
            return httpx.Response(200, text="FRESH")
        return next(responses)

    client = AsyncXNATClient(
        base_url="https://xnat.example.org",
        username="user",
        password="pass",
        session_token="stale",
        auto_reauth=True,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    resp = await client.get("/data/projects")

    assert resp.status_code == 200
    assert posts == 1
    assert client.session_token == "FRESH"


async def test_stream_401_raises_session_expired() -> None:
    client, _ = _client(_status_sequence(401))

    with pytest.raises(SessionExpiredError):
        async with client.stream("GET", "/data/projects"):
            pass


async def test_stream_403_raises_permission_denied() -> None:
    client, _ = _client(_status_sequence(403))

    with pytest.raises(PermissionDeniedError):
        async with client.stream("GET", "/data/projects"):
            pass


async def test_stream_404_raises_resource_not_found() -> None:
    client, _ = _client(_status_sequence(404))

    with pytest.raises(ResourceNotFoundError):
        async with client.stream("GET", "/data/projects"):
            pass


async def test_parallel_401s_trigger_exactly_one_reauth() -> None:
    barrier = asyncio.Barrier(2)
    auth_posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_posts
        if request.url.path == "/data/JSESSION":
            auth_posts += 1
            return httpx.Response(200, text="FRESH")
        if request.headers.get("cookie") == "JSESSIONID=FRESH":
            return httpx.Response(200, content=b"ok")
        async with barrier:
            pass
        return httpx.Response(401)

    client = AsyncXNATClient(
        base_url="https://xnat.example.org",
        username="user",
        password="pass",
        session_token="STALE",
        auto_reauth=True,
        transport=httpx.MockTransport(handler),
    )

    async def worker() -> None:
        async with client.stream("GET", "/data/projects") as resp:
            assert resp.status_code == 200

    await asyncio.gather(worker(), worker())

    assert auth_posts == 1


async def test_parallel_401s_on_get_trigger_exactly_one_reauth() -> None:
    barrier = asyncio.Barrier(2)
    auth_posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_posts
        if request.url.path == "/data/JSESSION":
            auth_posts += 1
            return httpx.Response(200, text="FRESH")
        if request.headers.get("cookie") == "JSESSIONID=FRESH":
            return httpx.Response(200, json={"ok": True})
        async with barrier:
            pass
        return httpx.Response(401)

    client = AsyncXNATClient(
        base_url="https://xnat.example.org",
        username="user",
        password="pass",
        session_token="STALE",
        auto_reauth=True,
        transport=httpx.MockTransport(handler),
    )

    async def get_worker() -> None:
        resp = await client.get("/data/projects")
        assert resp.status_code == 200

    async def get_json_worker() -> None:
        data = await client.get_json("/data/subjects")
        assert data == {"ok": True}

    await asyncio.gather(get_worker(), get_json_worker())

    assert auth_posts == 1


async def test_502_502_then_200_succeeds_after_two_retries(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502, 200), max_retries=3)

    resp = await client.get("/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 3
    assert len(sleeps) == 2


async def test_502_exhaustion_raises_retry_exhausted(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(502, 502), max_retries=1)

    with pytest.raises(RetryExhaustedError) as exc:
        await client.get("/data/projects")

    assert isinstance(exc.value.last_error, ServerError)
    assert exc.value.last_error.status_code == 502
    assert len(calls) == 2
    assert len(sleeps) == 1


async def test_409_is_not_retried_and_raises_client_request_error(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(409), max_retries=3)

    with pytest.raises(ClientRequestError) as exc:
        await client.get("/data/projects")

    assert exc.value.status_code == 409
    assert len(calls) == 1
    assert sleeps == []


async def test_stream_503_then_200_succeeds_after_retry(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(503, 200), max_retries=3)

    async with client.stream("GET", "/data/projects") as resp:
        assert resp.status_code == 200

    assert len(calls) == 2
    assert len(sleeps) == 1


async def test_stream_persistent_503_raises_retry_exhausted(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(503, 503), max_retries=1)

    with pytest.raises(RetryExhaustedError):
        async with client.stream("GET", "/data/projects"):
            pass

    assert len(calls) == 2
    assert len(sleeps) == 1


async def test_500_on_post_is_ambiguous_and_not_retried(sleeps: list[float]) -> None:
    client, calls = _client(_status_sequence(500), max_retries=3)

    with pytest.raises(ServerError) as exc:
        await async_request(client, "POST", "/data/services/import")

    assert exc.value.status_code == 500
    assert exc.value.hint is not None and "may have partially executed" in exc.value.hint
    assert len(calls) == 1
    assert sleeps == []


async def test_429_honours_retry_after_delta_seconds(sleeps: list[float]) -> None:
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    client, calls = _client(handler, max_retries=1)

    resp = await client.get("/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 2
    assert sleeps == [7.0]


async def test_503_honours_retry_after_http_date(sleeps: list[float]) -> None:
    when = datetime.now(UTC) + timedelta(seconds=12)
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": format_datetime(when, usegmt=True)})
        return httpx.Response(200, json={"ok": True})

    client, calls = _client(handler, max_retries=1)

    resp = await client.get("/data/projects")

    assert resp.status_code == 200
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 9.0 <= sleeps[0] <= 12.0


async def test_connect_timeout_fails_fast_without_retrying() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client, calls = _client(handler, max_retries=3)

    with pytest.raises(XNATTimeoutError) as exc:
        await client.get("/data/projects")

    assert len(calls) == 1, "connect timeout must not be retried"
    assert exc.value.timeout == DEFAULT_CONNECT_TIMEOUT_SECONDS


async def test_connect_error_retries_then_raises_retry_exhausted(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, calls = _client(handler, max_retries=1)

    with pytest.raises(RetryExhaustedError):
        await client.get("/data/projects")

    assert len(calls) == 2


async def test_read_timeout_is_retried_then_raises_retry_exhausted(sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client, calls = _client(handler, max_retries=2)

    with pytest.raises(RetryExhaustedError) as exc:
        await client.get("/data/projects")

    assert len(calls) == 3
    assert not isinstance(exc.value.last_error, XNATTimeoutError)


async def test_stream_connect_timeout_translated_via_stream_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client, calls = _client(handler, max_retries=3)

    with pytest.raises(XNATTimeoutError):
        async with client.stream("GET", "/data/projects"):
            pass

    assert len(calls) == 1


async def test_mid_body_timeout_is_translated_but_not_retried() -> None:
    async def failing_aiter_bytes(chunk_size: int | None = None):
        yield b"partial"
        raise httpx.ReadTimeout("dropped mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"full body")

    client, calls = _client(handler, max_retries=3)

    with pytest.raises(XNATTimeoutError):
        async with client.stream("GET", "/data/files") as resp:
            resp.aiter_bytes = failing_aiter_bytes
            async for _chunk in resp.aiter_bytes():
                pass

    assert len(calls) == 1


async def test_body_arrives_incrementally() -> None:
    payload = b"".join(bytes([i % 256]) for i in range(4096))
    client, _ = _client(lambda r: httpx.Response(200, content=payload))

    chunks: list[bytes] = []
    async with client.stream("GET", "/data/files") as resp:
        async for chunk in resp.aiter_bytes(chunk_size=256):
            chunks.append(chunk)

    assert len(chunks) > 1
    assert b"".join(chunks) == payload


async def test_authenticate_posts_to_jsession_and_stores_token() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, text="ABC123SESSION"),
        username="user",
        password="pass",
    )

    token = await client.authenticate()

    assert token == "ABC123SESSION"
    assert client.session_token == "ABC123SESSION"
    assert client.is_authenticated
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/data/JSESSION"


async def test_authenticate_requires_credentials() -> None:
    client, calls = _client(lambda r: httpx.Response(200, text="X"))

    with pytest.raises(AuthenticationError):
        await client.authenticate()

    assert calls == []


async def test_authenticate_non_200_raises_authentication_error() -> None:
    client, _ = _client(
        lambda r: httpx.Response(500, text="boom"),
        username="user",
        password="pass",
    )

    with pytest.raises(AuthenticationError):
        await client.authenticate()


async def test_authenticate_html_body_is_invalid_credentials() -> None:
    client, _ = _client(
        lambda r: httpx.Response(200, text="<html><body>Login failed</body></html>"),
        username="user",
        password="bad",
    )

    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await client.authenticate()


async def test_authenticate_connect_timeout_maps_to_typed_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    client, _ = _client(handler, username="user", password="pass")

    with pytest.raises(XNATTimeoutError):
        await client.authenticate()


async def test_authenticate_connect_error_maps_to_server_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = _client(handler, username="user", password="pass")

    with pytest.raises(ServerUnreachableError):
        await client.authenticate()


async def test_authenticate_read_timeout_maps_to_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client(handler, username="user", password="pass")

    with pytest.raises(NetworkError):
        await client.authenticate()


async def test_context_manager_authenticates_when_password_given_and_no_token() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, text="FRESHTOKEN"),
        username="user",
        password="pass",
    )

    async with client as c:
        assert c.session_token == "FRESHTOKEN"

    assert calls[0].url.path == "/data/JSESSION"


async def test_context_manager_skips_login_with_existing_token() -> None:
    client, calls = _client(
        lambda r: httpx.Response(200, json={}),
        username="user",
        password="pass",
        session_token="EXISTING",
    )

    async with client:
        pass

    assert calls == [], "entering must not spend a login round-trip on a token-only client"


async def test_context_manager_closes_the_client() -> None:
    client, _ = _client(lambda r: httpx.Response(200, json={}))

    async with client as c:
        await c.get("/data/projects")

    assert client._client is None


async def test_aclose_is_idempotent() -> None:
    client, _ = _client(lambda r: httpx.Response(200, json={}))
    await client.get("/data/projects")

    await client.aclose()
    await client.aclose()

    assert client._client is None


async def test_from_profile_resolves_params_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_resolve(name: str | None = None, *, config_path: object = None) -> dict[str, object]:
        captured["name"] = name
        captured["config_path"] = config_path
        return {
            "base_url": "https://xnat.example.org",
            "username": "resolved-user",
            "auto_reauth": True,
        }

    monkeypatch.setattr("xnatctl.core.connect.resolve_client_params", fake_resolve)

    client = await AsyncXNATClient.from_profile("prod")

    assert captured["name"] == "prod"
    assert client.base_url == "https://xnat.example.org"
    assert client.username == "resolved-user"
    assert client.auto_reauth is True


async def test_from_profile_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_seconds = 0.2
    tick_interval = 0.01

    def slow_resolve(name: str | None = None, *, config_path: object = None) -> dict[str, object]:
        time.sleep(block_seconds)
        return {"base_url": "https://xnat.example.org", "auto_reauth": True}

    monkeypatch.setattr("xnatctl.core.connect.resolve_client_params", slow_resolve)

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(tick_interval)

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)

    await AsyncXNATClient.from_profile("prod")

    stop = True
    await ticker_task

    expected = block_seconds / tick_interval
    assert ticks >= expected * 0.5, (
        f"event loop starved during from_profile(): only {ticks} ticks in "
        f"~{block_seconds}s (expected ~{expected:.0f})"
    )


async def test_get_client_builds_default_tls_context_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_seconds = 0.2
    tick_interval = 0.01

    def slow_create_ssl_context(*args: object, **kwargs: object) -> ssl.SSLContext:
        time.sleep(block_seconds)
        return ssl.create_default_context()

    monkeypatch.setattr(
        "xnatctl.core.async_client.httpx.create_ssl_context", slow_create_ssl_context
    )

    client = AsyncXNATClient(base_url="https://xnat.example.org")

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(tick_interval)

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)

    try:
        await client._get_client()
    finally:
        stop = True
        await ticker_task
        await client.aclose()

    expected = block_seconds / tick_interval
    assert ticks >= expected * 0.5, (
        f"event loop starved while building the default TLS context: only "
        f"{ticks} ticks in ~{block_seconds}s (expected ~{expected:.0f})"
    )


async def test_httpx_verify_ca_bundle_takes_precedence_over_disabled_verify_ssl(
    tmp_path: object,
) -> None:
    import certifi

    client = AsyncXNATClient(
        base_url="https://xnat.example.org", verify_ssl=False, ca_bundle=certifi.where()
    )

    verify = await client.httpx_verify()

    assert isinstance(verify, ssl.SSLContext)


async def test_get_client_no_disabled_warning_with_ca_bundle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import certifi

    client = AsyncXNATClient(
        base_url="https://xnat.example.org", verify_ssl=False, ca_bundle=certifi.where()
    )
    with caplog.at_level(logging.WARNING, logger="xnatctl.core.async_client"):
        await client._get_client()
    try:
        assert not any("DISABLED" in r.message for r in caplog.records)
    finally:
        await client.aclose()
