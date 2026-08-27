"""Transport-level tests for XNATClient convenience methods and the public
``transport=`` seam.

Retry/backoff numerics and status->exception mapping live in
``tests/test_core_client_retry.py``; this module deliberately does not
duplicate them. What it owns:

* the ``transport=`` constructor seam itself,
* ``get_json`` / ``ping`` / ``whoami`` request shapes,
* connect/timeout failure mapping through a real httpx request cycle.

Everything runs through ``httpx.MockTransport``, so requests are real
``httpx.Request`` objects and assertions are about what would go on the wire.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from xnatctl.core.client import XNATClient
from xnatctl.core.exceptions import (
    AuthenticationError,
    NetworkError,
    RetryExhaustedError,
    ServerUnreachableError,
)
from xnatctl.core.exceptions import (
    RequestTimeoutError as XNATTimeoutError,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually sleep: retry backoff must not cost wall-clock here."""
    monkeypatch.setattr("xnatctl.core.client.time.sleep", lambda _s: None)


def make_client(
    handler: Handler,
    *,
    max_retries: int = 3,
    **kwargs: object,
) -> tuple[XNATClient, list[httpx.Request]]:
    """Build an XNATClient wired to ``handler`` via the public transport seam.

    Returns the client plus a list recording every request the transport saw.
    """
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = XNATClient(
        base_url="https://xnat.example.org",
        max_retries=max_retries,
        transport=httpx.MockTransport(recording),
        **kwargs,  # type: ignore[arg-type]
    )
    return client, calls


def query_of(request: httpx.Request) -> dict[str, list[str]]:
    """Parse a recorded request's query string."""
    return parse_qs(urlparse(str(request.url)).query)


# =============================================================================
# The transport seam itself
# =============================================================================


class TestTransportSeam:
    def test_transport_kwarg_is_used_for_requests(self) -> None:
        """A transport passed to the constructor serves the request."""
        client, calls = make_client(lambda _r: httpx.Response(200, json={"ok": True}))

        resp = client.get("/data/projects")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(calls) == 1
        assert calls[0].url.path == "/data/projects"

    def test_transport_defaults_to_none(self) -> None:
        """Omitting transport leaves httpx to build its default."""
        client = XNATClient(base_url="https://xnat.example.org")
        assert client.transport is None

    def test_base_url_is_applied_to_relative_paths(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json={}))

        client.get("/data/projects")

        assert str(calls[0].url).startswith("https://xnat.example.org/data/projects")


# =============================================================================
# Convenience methods
# =============================================================================


class TestGetJson:
    def test_injects_format_json(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json={"a": 1}))

        data = client.get_json("/data/projects")

        assert data == {"a": 1}
        assert query_of(calls[0])["format"] == ["json"]

    def test_preserves_caller_params(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json={}))

        client.get_json("/data/projects", params={"columns": "ID"})

        q = query_of(calls[0])
        assert q["columns"] == ["ID"]
        assert q["format"] == ["json"]


class TestPing:
    def test_returns_version_and_hits_buildinfo(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text="1.8.10"))

        result = client.ping()

        assert calls[0].url.path == "/xapi/siteConfig/buildInfo/version"
        assert result.version == "1.8.10"
        assert result.status == "ok"


class TestWhoami:
    def test_resolves_username_from_xapi_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/users/username":
                return httpx.Response(200, text="jdoe")
            if request.url.path == "/xapi/users/jdoe":
                return httpx.Response(
                    200,
                    json={
                        "username": "jdoe",
                        "firstName": "Jane",
                        "lastName": "Doe",
                        "email": "jdoe@example.org",
                        "enabled": True,
                    },
                )
            return httpx.Response(404)

        client, _ = make_client(handler)

        info = client.whoami()

        assert info.username == "jdoe"
        assert info.firstname == "Jane"
        assert info.email == "jdoe@example.org"

    def test_html_response_is_not_treated_as_a_username(self) -> None:
        """A login page must not become the reported identity."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/users/username":
                return httpx.Response(200, text="<html><body>Login</body></html>")
            return httpx.Response(404)

        client, _ = make_client(handler, username="configured")

        assert client.whoami().username == "configured"

    def test_falls_back_to_unknown_without_configured_username(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(404))

        info = client.whoami()

        assert info.username == "unknown"
        assert info.enabled is False


# =============================================================================
# Transport failure mapping (through a real request cycle)
# =============================================================================


class TestTransportFailureMapping:
    def test_connect_error_retries_then_raises_retry_exhausted(self) -> None:
        """No raw httpx error escapes the client."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, calls = make_client(handler, max_retries=1)

        with pytest.raises(RetryExhaustedError):
            client.get("/data/projects")

        assert len(calls) == 2, "initial attempt plus one retry"

    def test_connect_error_with_no_retries_still_raises_typed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, calls = make_client(handler, max_retries=0)

        with pytest.raises(RetryExhaustedError):
            client.get("/data/projects")

        assert len(calls) == 1

    def test_connect_timeout_fails_fast_without_retrying(self) -> None:
        """A blackholed host must fail in seconds, not after backoff."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        client, calls = make_client(handler, max_retries=3)

        with pytest.raises(XNATTimeoutError):
            client.get("/data/projects")

        assert len(calls) == 1, "connect timeout must not be retried"

    def test_connect_timeout_is_not_swallowed_by_the_timeout_branch(self) -> None:
        """ConnectTimeout subclasses TimeoutException; ordering must favour it."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        client, _ = make_client(handler, max_retries=3)

        with pytest.raises(XNATTimeoutError) as exc:
            client.get("/data/projects")

        assert not isinstance(exc.value, NetworkError)

    def test_read_timeout_is_retried_then_raises_retry_exhausted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        client, calls = make_client(handler, max_retries=2)

        with pytest.raises(RetryExhaustedError):
            client.get("/data/projects")

        assert len(calls) == 3, "initial attempt plus two retries"

    def test_recovers_when_the_transport_stops_failing(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"ok": True})

        client, calls = make_client(handler, max_retries=3)

        assert client.get("/data/projects").json() == {"ok": True}
        assert len(calls) == 3


# =============================================================================
# Authentication and session wiring
# =============================================================================


class TestAuthenticate:
    def test_posts_to_jsession_and_stores_token(self) -> None:
        client, calls = make_client(
            lambda _r: httpx.Response(200, text="ABC123SESSION"),
            username="user",
            password="pass",
        )

        token = client.authenticate()

        assert token == "ABC123SESSION"
        assert client.session_token == "ABC123SESSION"
        assert client.is_authenticated
        assert calls[0].method == "POST"
        assert calls[0].url.path == "/data/JSESSION"

    def test_requires_credentials(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text="X"))

        with pytest.raises(AuthenticationError):
            client.authenticate()

        assert calls == [], "no request is made without credentials"

    def test_non_200_raises_authentication_error(self) -> None:
        client, _ = make_client(
            lambda _r: httpx.Response(500, text="boom"),
            username="user",
            password="pass",
        )

        with pytest.raises(AuthenticationError):
            client.authenticate()

    def test_html_body_is_treated_as_invalid_credentials(self) -> None:
        """XNAT answers a bad login with a 200 + login page, not a 401."""
        client, _ = make_client(
            lambda _r: httpx.Response(200, text="<html><body>Login failed</body></html>"),
            username="user",
            password="bad",
        )

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            client.authenticate()

    def test_connect_timeout_maps_to_typed_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connect timed out", request=request)

        client, _ = make_client(handler, username="user", password="pass")

        with pytest.raises(XNATTimeoutError):
            client.authenticate()

    def test_connect_error_maps_to_server_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, _ = make_client(handler, username="user", password="pass")

        with pytest.raises(ServerUnreachableError):
            client.authenticate()

    def test_read_timeout_maps_to_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client, _ = make_client(handler, username="user", password="pass")

        with pytest.raises(NetworkError):
            client.authenticate()


class TestSessionWiring:
    def test_session_token_is_sent_as_jsessionid_cookie(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json={}))
        client.session_token = "TOK"

        client.get("/data/projects")

        assert "JSESSIONID=TOK" in calls[0].headers.get("cookie", "")

    def test_basic_auth_used_when_no_session_token(self) -> None:
        client, calls = make_client(
            lambda _r: httpx.Response(200, json={}),
            username="user",
            password="pass",
        )

        client.get("/data/projects")

        assert calls[0].headers.get("authorization", "").startswith("Basic ")

    def test_session_token_takes_precedence_over_basic_auth(self) -> None:
        client, calls = make_client(
            lambda _r: httpx.Response(200, json={}),
            username="user",
            password="pass",
        )
        client.session_token = "TOK"

        client.get("/data/projects")

        assert "authorization" not in calls[0].headers

    def test_invalidate_session_deletes_and_clears_token(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text=""))
        client.session_token = "TOK"

        client.invalidate_session()

        assert client.session_token is None
        assert not client.is_authenticated
        assert calls[0].method == "DELETE"
        assert calls[0].url.path == "/data/JSESSION"

    def test_invalidate_session_clears_token_even_if_logout_fails(self) -> None:
        """Best-effort logout: a server error must not leave a stale token."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, _ = make_client(handler)
        client.session_token = "TOK"

        client.invalidate_session()

        assert client.session_token is None

    def test_invalidate_session_is_a_noop_without_a_token(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, text=""))

        client.invalidate_session()

        assert calls == []

    def test_close_is_idempotent(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, json={}))
        client.get("/data/projects")

        client.close()
        client.close()

        assert client._client is None

    def test_context_manager_closes_the_client(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, json={}))

        with client as c:
            c.get("/data/projects")

        assert client._client is None


# =============================================================================
# The exception contract, enumerated
# =============================================================================


class TestNoHttpxExceptionEscapes:
    """No httpx exception may leave XNATClient. Checked by enumeration.

    The individual mappings are covered elsewhere. What is checked here is the
    *closure* of the contract, because the failure mode is an omission rather
    than a wrong answer: httpx.TooManyRedirects was missing from both transport
    tuples and escaped raw, past the typed dispatch in cli/common.py, and
    reached the user as a traceback. Every test naming a specific exception
    passed the whole time -- nothing was asserting that the list was complete.

    A redirect loop is not a hypothetical for this tool: an uninitialized XNAT
    bounces essentially every request to /setup, and so does a login-wall proxy
    or a misconfigured siteUrl.

    Discovered by walking httpx's exception tree rather than by listing names,
    so an exception added by a future httpx release fails here instead of in
    somebody's terminal.
    """

    @staticmethod
    def _transport_exceptions() -> list[type[httpx.HTTPError]]:
        """Every concrete httpx error a transport can raise during a request."""

        def leaves(cls: type) -> list[type]:
            subs = cls.__subclasses__()
            return [cls] if not subs else [leaf for sub in subs for leaf in leaves(sub)]

        # Rooted at RequestError, not TransportError: TooManyRedirects and
        # DecodingError sit outside TransportError, and rooting the walk there
        # is what let the original leak hide. HTTPStatusError is excluded
        # because raise_for_status() raises it, never a transport.
        return [c for c in leaves(httpx.RequestError) if not issubclass(c, httpx.HTTPStatusError)]

    def test_the_enumeration_is_not_empty(self) -> None:
        """Guards the test itself: a bad walk would vacuously pass everything."""
        found = self._transport_exceptions()

        assert len(found) >= 8, f"only found {[c.__name__ for c in found]}"
        assert httpx.TooManyRedirects in found, "the regression case must be covered"

    @pytest.mark.parametrize("exc_type", _transport_exceptions.__func__(), ids=lambda c: c.__name__)
    def test_it_is_translated(self, exc_type: type[httpx.HTTPError]) -> None:
        from xnatctl.core.exceptions import XNATCtlError

        def handler(_request: httpx.Request) -> httpx.Response:
            raise exc_type("simulated")

        client, _ = make_client(handler, max_retries=1)

        with pytest.raises(XNATCtlError):
            client.get_json("/data/projects")

    def test_a_redirect_loop_is_not_retried(self) -> None:
        """Retrying a loop just makes the user wait to see the same failure."""

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.TooManyRedirects("simulated")

        client, calls = make_client(handler, max_retries=3)

        with pytest.raises(NetworkError):
            client.get_json("/data/projects")

        assert len(calls) == 1
