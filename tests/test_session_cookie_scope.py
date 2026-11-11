"""The JSESSIONID cookie reaches the configured XNAT host and nothing else.

Both clients follow redirects, and httpx rebuilds a redirected request's
cookies from the client's cookie jar. A JSESSIONID stored without a domain
matched every host, so a 302 from the server (an open redirect, a
misconfigured proxy) handed a live session to whichever host it named.

Every test drives real redirect hops through ``httpx.MockTransport``, so the
assertions cover httpx's own redirect and cookie-jar logic, not a stand-in.
The dotless and IP host cases guard the other direction: scoping the cookie
must not stop it reaching ``localhost`` or an IP-addressed server.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from xnatctl.core.async_client import AsyncXNATClient
from xnatctl.core.client import XNATClient

pytestmark = pytest.mark.asyncio

TOKEN = "CANARYTOKEN"

Handler = Callable[[httpx.Request], httpx.Response]


def _redirect_once(location: str) -> Handler:
    """302 the first request to ``location``; answer everything else 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/projects":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, json={})

    return handler


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _carries_token(request: httpx.Request) -> bool:
    return f"JSESSIONID={TOKEN}" in request.headers.get("cookie", "")


async def _calls(
    mode: str, base_url: str, handler: Handler, *, stream: bool = False
) -> list[httpx.Request]:
    """GET /data/projects with a session token; return every request sent."""
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    if mode == "sync":
        client = XNATClient(
            base_url=base_url,
            session_token=TOKEN,
            max_retries=0,
            transport=httpx.MockTransport(record),
        )
        if stream:
            with client.stream("GET", "/data/projects") as resp:
                resp.read()
        else:
            client.get("/data/projects")
        client.close()
    else:

        async def arecord(request: httpx.Request) -> httpx.Response:
            return record(request)

        aclient = AsyncXNATClient(
            base_url=base_url,
            session_token=TOKEN,
            max_retries=0,
            transport=httpx.MockTransport(arecord),
        )
        if stream:
            async with aclient.stream("GET", "/data/projects") as aresp:
                await aresp.aread()
        else:
            await aclient.get("/data/projects")
        await aclient.aclose()
    return calls


MODES = pytest.mark.parametrize("mode", ["sync", "async"])


@MODES
async def test_cross_host_redirect_does_not_carry_session(mode: str) -> None:
    calls = await _calls(
        mode, "https://xnat.example.org", _redirect_once("https://evil.example.net/steal")
    )

    assert [c.url.host for c in calls] == ["xnat.example.org", "evil.example.net"]
    assert _carries_token(calls[0])
    assert not _carries_token(calls[1])


@MODES
async def test_subdomain_redirect_does_not_carry_session(mode: str) -> None:
    """Host-only means exact host: Python's default policy would match subdomains."""
    calls = await _calls(
        mode, "https://xnat.example.org", _redirect_once("https://evil.xnat.example.org/steal")
    )

    assert calls[1].url.host == "evil.xnat.example.org"
    assert not _carries_token(calls[1])


@MODES
async def test_same_origin_redirect_keeps_session(mode: str) -> None:
    calls = await _calls(
        mode, "https://xnat.example.org", _redirect_once("/data/projects/elsewhere")
    )

    assert [c.url.host for c in calls] == ["xnat.example.org", "xnat.example.org"]
    assert all(_carries_token(c) for c in calls)


@MODES
async def test_https_session_is_not_sent_over_a_plain_http_redirect(mode: str) -> None:
    calls = await _calls(
        mode, "https://xnat.example.org", _redirect_once("http://xnat.example.org/data/x")
    )

    assert calls[1].url.scheme == "http"
    assert not _carries_token(calls[1])


@MODES
async def test_stream_redirect_does_not_carry_session(mode: str) -> None:
    """stream() sends an explicit Cookie header; httpx rebuilds it on redirect."""
    calls = await _calls(
        mode,
        "https://xnat.example.org",
        _redirect_once("https://evil.example.net/steal"),
        stream=True,
    )

    assert _carries_token(calls[0])
    assert not _carries_token(calls[1])


@MODES
@pytest.mark.parametrize(
    "location",
    ["https://evil.example.net/steal", "https://evil.xnat.example.org/steal"],
    ids=["other-host", "subdomain"],
)
async def test_server_set_session_cookie_does_not_follow_redirect(mode: str, location: str) -> None:
    """XNAT's own Set-Cookie: JSESSIONID is a live session too."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/projects":
            return httpx.Response(
                302,
                headers={"Location": location, "Set-Cookie": "JSESSIONID=SERVERSET; Path=/"},
            )
        return httpx.Response(200, json={})

    calls = await _calls(mode, "https://xnat.example.org", handler)

    assert calls[1].url.host == httpx.URL(location).host
    assert "SERVERSET" not in calls[1].headers.get("cookie", "")


@MODES
async def test_http_to_https_upgrade_on_same_host_keeps_session(mode: str) -> None:
    """The one cross-origin hop httpx itself trusts with Authorization."""
    calls = await _calls(
        mode, "http://xnat.example.org", _redirect_once("https://xnat.example.org/data/x")
    )

    assert calls[1].url.scheme == "https"
    assert _carries_token(calls[1])


@MODES
async def test_redirect_to_another_port_does_not_carry_session(mode: str) -> None:
    calls = await _calls(
        mode, "https://xnat.example.org", _redirect_once("https://xnat.example.org:8443/x")
    )

    assert calls[1].url.port == 8443
    assert not _carries_token(calls[1])


@MODES
@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "https://XNAT.Example.ORG:8443/xnat",
        "https://täst.example.org",
    ],
    ids=["dotless-host", "ipv4", "ipv6", "mixed-case-port-and-context-path", "idna-host"],
)
async def test_session_reaches_every_kind_of_configured_host(mode: str, base_url: str) -> None:
    calls = await _calls(mode, base_url, _ok)

    assert _carries_token(calls[0])


async def test_logout_redirect_does_not_carry_session() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "xnat.example.org":
            return httpx.Response(307, headers={"Location": "https://evil.example.net/steal"})
        return httpx.Response(200)

    client = XNATClient(
        base_url="https://xnat.example.org",
        session_token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    client.invalidate_session()

    assert [c.url.host for c in calls] == ["xnat.example.org", "evil.example.net"]
    assert _carries_token(calls[0])
    assert not _carries_token(calls[1])
