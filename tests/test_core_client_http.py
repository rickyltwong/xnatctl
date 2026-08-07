"""Transport-level tests for XNATClient pagination, convenience methods, and
the public ``transport=`` seam.

Retry/backoff numerics and status->exception mapping live in
``tests/test_core_client_retry.py``; this module deliberately does not
duplicate them. What it owns:

* the ``transport=`` constructor seam itself,
* ``paginate()`` — offsets on the wire and all three page boundaries,
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
    TimeoutError as XNATTimeoutError,
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


def page(count: int, *, start: int = 0) -> dict[str, object]:
    """Build an XNAT ResultSet page with ``count`` synthetic rows."""
    return {"ResultSet": {"Result": [{"ID": f"ITEM{start + i:04d}"} for i in range(count)]}}


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
# paginate()
# =============================================================================


class TestPaginate:
    def test_partial_final_page_stops_without_extra_request(self) -> None:
        """100 + 100 + 50 yields 250 items in exactly three requests."""
        pages = [page(100, start=0), page(100, start=100), page(50, start=200)]
        seq = iter(pages)
        client, calls = make_client(lambda _r: httpx.Response(200, json=next(seq)))

        items = list(client.paginate("/data/projects", page_size=100))

        assert len(items) == 250
        assert len(calls) == 3
        assert [query_of(c)["offset"][0] for c in calls] == ["0", "100", "200"]
        assert {query_of(c)["limit"][0] for c in calls} == {"100"}

    def test_exactly_full_final_page_costs_one_extra_empty_request(self) -> None:
        """A final page equal to page_size cannot be known to be last."""
        pages = [page(100, start=0), page(100, start=100), page(0)]
        seq = iter(pages)
        client, calls = make_client(lambda _r: httpx.Response(200, json=next(seq)))

        items = list(client.paginate("/data/projects", page_size=100))

        assert len(items) == 200
        assert len(calls) == 3, "the empty third page is what terminates the loop"
        assert query_of(calls[2])["offset"][0] == "200"

    def test_a_server_that_ignores_limit_does_not_loop_forever(self) -> None:
        """Regression: XNAT 1.9.2.1 returns the whole set for any limit.

        The old loop saw len(results) >= page_size, advanced the offset, and
        got the identical page back on every subsequent request -- an infinite
        generator that also re-yielded every row each time round. Found by the
        integration tier against a real server, on /data/projects.
        """
        client, calls = make_client(lambda _r: httpx.Response(200, json=page(7)))

        items = list(client.paginate("/data/projects", page_size=2))

        assert len(items) == 7, "the full result set is still delivered once"
        assert len(calls) == 1, "a server that ignores limit has no second page"

    def test_a_repeated_page_stops_the_loop(self) -> None:
        """The same regression when the collection is smaller than a page.

        Counting rows cannot detect it here: one row for a page size of one
        looks exactly like a legitimate full page, so the loop advanced the
        offset and got that same row back forever. This is the shape the live
        server actually presented -- one project, page_size=1 -- and it is why
        the row-count check alone was not enough.
        """
        client, calls = make_client(lambda _r: httpx.Response(200, json=page(1)))

        items = list(client.paginate("/data/projects", page_size=1))

        assert len(items) == 1, "the row is yielded once, not twice"
        assert len(calls) == 2, "one request, then one that proves it repeated"

    def test_a_repeated_page_is_not_yielded_twice(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, json=page(2)))

        ids = [item["ID"] for item in client.paginate("/data/projects", page_size=2)]

        assert ids == sorted(set(ids)), f"duplicate rows yielded: {ids}"

    def test_genuinely_paginating_servers_are_unaffected(self) -> None:
        """Distinct pages of equal size must still walk to the end."""
        pages = [page(2, start=0), page(2, start=2), page(1, start=4)]
        seq = iter(pages)
        client, calls = make_client(lambda _r: httpx.Response(200, json=next(seq)))

        items = list(client.paginate("/data/projects", page_size=2))

        assert len(items) == 5
        assert len(calls) == 3

    def test_empty_first_page_makes_exactly_one_request(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json=page(0)))

        items = list(client.paginate("/data/projects", page_size=100))

        assert items == []
        assert len(calls) == 1

    def test_missing_result_key_yields_nothing_and_does_not_raise(self) -> None:
        """Dot-navigation into an absent key degrades to empty, not KeyError."""
        client, calls = make_client(lambda _r: httpx.Response(200, json={"Unexpected": {}}))

        items = list(client.paginate("/data/projects", result_key="ResultSet.Result"))

        assert items == []
        assert len(calls) == 1

    def test_result_key_navigating_through_non_dict_yields_nothing(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(200, json={"ResultSet": "not-a-dict"}))

        assert list(client.paginate("/data/projects")) == []

    def test_custom_result_key_is_honoured(self) -> None:
        payload = {"items": [{"ID": "A"}, {"ID": "B"}]}
        client, _ = make_client(lambda _r: httpx.Response(200, json=payload))

        items = list(client.paginate("/data/x", page_size=100, result_key="items"))

        assert [i["ID"] for i in items] == ["A", "B"]

    def test_format_json_and_extra_params_are_sent(self) -> None:
        client, calls = make_client(lambda _r: httpx.Response(200, json=page(0)))

        list(client.paginate("/data/projects", params={"columns": "ID,label"}))

        q = query_of(calls[0])
        assert q["format"] == ["json"]
        assert q["columns"] == ["ID,label"]

    def test_caller_params_are_not_mutated(self) -> None:
        """Paginate copies its params; the caller's dict stays clean."""
        client, _ = make_client(lambda _r: httpx.Response(200, json=page(0)))
        params: dict[str, object] = {"columns": "ID"}

        list(client.paginate("/data/projects", params=params))

        assert params == {"columns": "ID"}

    def test_page_size_smaller_than_default(self) -> None:
        pages = [page(2, start=0), page(1, start=2)]
        seq = iter(pages)
        client, calls = make_client(lambda _r: httpx.Response(200, json=next(seq)))

        items = list(client.paginate("/data/projects", page_size=2))

        assert len(items) == 3
        assert [query_of(c)["offset"][0] for c in calls] == ["0", "2"]

    def test_is_lazy(self) -> None:
        """No request is issued until the generator is consumed."""
        client, calls = make_client(lambda _r: httpx.Response(200, json=page(0)))

        gen = client.paginate("/data/projects")

        assert calls == []
        next(gen, None)
        assert len(calls) == 1


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
        assert "1.8.10" in str(result.get("version", ""))


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

        assert info["username"] == "jdoe"
        assert info["firstname"] == "Jane"
        assert info["email"] == "jdoe@example.org"

    def test_html_response_is_not_treated_as_a_username(self) -> None:
        """A login page must not become the reported identity."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/xapi/users/username":
                return httpx.Response(200, text="<html><body>Login</body></html>")
            return httpx.Response(404)

        client, _ = make_client(handler, username="configured")

        assert client.whoami()["username"] == "configured"

    def test_falls_back_to_unknown_without_configured_username(self) -> None:
        client, _ = make_client(lambda _r: httpx.Response(404))

        info = client.whoami()

        assert info["username"] == "unknown"
        assert info["enabled"] is False


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


class TestPaginateTruncationIsAudible:
    """Stopping early is a guess, and the guess must be visible.

    The stop condition cannot tell "this endpoint does not paginate" (right to
    stop) from "this server honours limit but ignores offset" (stopping
    truncates). Logging that at DEBUG hid the difference from everyone who was
    not already debugging, so a short listing that should have been long would
    simply be believed.
    """

    def test_it_warns_rather_than_whispering(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        client, _ = make_client(lambda _r: httpx.Response(200, json=page(7)))

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.client"):
            list(client.paginate("/data/projects", page_size=2))

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an early stop was not surfaced above DEBUG"
        assert "incomplete" in warnings[0].getMessage()

    def test_a_normal_walk_stays_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        """The warning must not cry wolf on every ordinary listing."""
        import logging

        pages = [page(2, start=0), page(2, start=2), page(1, start=4)]
        seq = iter(pages)
        client, _ = make_client(lambda _r: httpx.Response(200, json=next(seq)))

        with caplog.at_level(logging.WARNING, logger="xnatctl.core.client"):
            list(client.paginate("/data/projects", page_size=2))

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
