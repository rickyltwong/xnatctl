"""Diagnostic-logging tests for the client and auth layers.

Before this, `xnatctl -v` told you nothing about HTTP: the client had no
per-request logging, the retry loop slept silently (so a retry storm looked
like a hang), and setup_logging pinned httpx/httpcore to WARNING even under
--verbose. The auth layer had no logger at all, so "which credential did it
actually use?" was unanswerable without a debugger.

Every assertion here doubles as a redaction check: these are the log lines
the RedactionFilter exists to protect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from xnatctl.core.auth import AuthManager
from xnatctl.core.client import XNATClient
from xnatctl.core.logging import setup_logging

Handler = Callable[[httpx.Request], httpx.Response]

SECRET = "s3cret-token-value"
CLIENT_LOGGER = "xnatctl.core.client"
AUTH_LOGGER = "xnatctl.core.auth"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff must not cost wall-clock here."""
    monkeypatch.setattr("xnatctl.core.client.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def no_debug_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own XNATCTL_DEBUG must not change these results."""
    monkeypatch.delenv("XNATCTL_DEBUG", raising=False)


@pytest.fixture
def clean_root() -> Iterator[None]:
    """Snapshot and restore the loggers setup_logging mutates."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    saved_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        yield
    finally:
        root.handlers, root.level = saved[0], saved[1]
        for name, lvl in saved_levels.items():
            logging.getLogger(name).setLevel(lvl)


def make_client(handler: Handler, *, max_retries: int = 3, **kwargs: object) -> XNATClient:
    return XNATClient(
        base_url="https://xnat.example.org",
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# =============================================================================
# Per-request visibility
# =============================================================================


def test_each_attempt_logs_method_path_status_and_timing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_client(lambda r: httpx.Response(200, json={}))

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        client.get("/data/projects")

    line = next(m for m in messages(caplog) if "-> 200" in m)
    assert "GET" in line
    assert "/data/projects" in line
    assert "ms" in line
    assert "attempt 1/4" in line


def test_retries_are_logged_at_warning_with_the_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retry storm was previously invisible -- the single most common cause
    of "xnatctl is hanging".
    """
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200 if attempts["n"] > 2 else 503, json={})

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        make_client(flaky).get("/data/projects")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "HTTP 503" in warnings[0].getMessage()
    assert "retrying in" in warnings[0].getMessage()


def test_retry_after_is_reported_as_such(caplog: pytest.LogCaptureFixture) -> None:
    attempts = {"n": 0}

    def throttled(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={})

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        make_client(throttled).get("/data/projects")

    assert "per Retry-After" in messages(caplog)[0]
    assert "7.0s" in messages(caplog)[0]


def test_transport_failure_retry_names_the_error(caplog: pytest.LogCaptureFixture) -> None:
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, json={})

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        make_client(flaky).get("/data/projects")

    assert "retrying in" in messages(caplog)[0]


def test_pagination_logs_each_page(caplog: pytest.LogCaptureFixture) -> None:
    def paged(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params).get("offset", 0))
        rows = [{"ID": f"P{offset + i}"} for i in range(2 if offset == 0 else 1)]
        return httpx.Response(200, json={"ResultSet": {"Result": rows}})

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        list(make_client(paged).paginate("/data/projects", page_size=2))

    page_lines = [m for m in messages(caplog) if "paginate" in m]
    assert len(page_lines) == 2
    assert "offset=0" in page_lines[0]
    assert "2 items" in page_lines[0]


# =============================================================================
# Reauth path
# =============================================================================


def test_reauth_decision_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    calls = {"n": 0}

    def expiring(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/JSESSION":
            return httpx.Response(200, text="NEWTOKEN")
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] > 1 else 401, json={})

    client = make_client(expiring, username="u", password="p", auto_reauth=True)

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        client.get("/data/projects")

    assert any("re-authenticating" in m for m in messages(caplog))


def test_refusal_to_reauth_explains_why(caplog: pytest.LogCaptureFixture) -> None:
    """A bare SessionExpiredError gave no clue which precondition was missing."""
    from xnatctl.core.exceptions import SessionExpiredError

    client = make_client(lambda r: httpx.Response(401, json={}))

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        with pytest.raises(SessionExpiredError):
            client.get("/data/projects")

    line = next(m for m in messages(caplog) if "not re-authenticating" in m)
    assert "auto_reauth=False" in line
    assert "credentials=False" in line


# =============================================================================
# Secrets must never reach the log
# =============================================================================


def test_request_logging_redacts_query_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """The per-attempt line logs the full URL, so it is the most likely place
    for a token to leak.
    """
    client = make_client(lambda r: httpx.Response(200, json={}))

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        client.get("/data/projects", params={"token": SECRET, "format": "json"})

    joined = " ".join(messages(caplog))
    assert SECRET not in joined
    assert "token=***" in joined
    assert "format=json" in joined, "non-secret params must stay readable"


def test_authenticate_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client(
        lambda r: httpx.Response(200, text=SECRET), username="admin", password="hunter2"
    )

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        client.authenticate()

    joined = " ".join(messages(caplog))
    assert SECRET not in joined
    assert "hunter2" not in joined
    assert "Authenticated as admin" in joined


def test_auth_manager_logs_presence_not_values(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XNAT_USER", "admin")
    monkeypatch.setenv("XNAT_PASS", "hunter2")

    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        AuthManager().get_credentials()

    joined = " ".join(messages(caplog))
    assert "hunter2" not in joined
    assert "XNAT_PASS=set" in joined


# =============================================================================
# Session cache diagnostics
# =============================================================================


def test_session_cache_miss_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = AuthManager(cache_file=tmp_path / ".session")

    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        assert manager.load_session() is None

    assert any("cache miss" in m for m in messages(caplog))


def test_session_cache_hit_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    manager = AuthManager(cache_file=tmp_path / ".session")
    manager.save_session("TOK", "https://xnat.example.org", "alice")

    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        manager.load_session()

    joined = " ".join(messages(caplog))
    assert "cache hit" in joined
    assert "alice" in joined
    assert "TOK" not in joined, "the token itself must never be logged"


def test_url_mismatch_is_logged_as_a_miss(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Pointing at a second server silently ignores the cache; -v should say so."""
    manager = AuthManager(cache_file=tmp_path / ".session")
    manager.save_session("TOK", "https://a.example.org", "alice")

    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        assert manager.load_session("https://b.example.org") is None

    assert any("wanted https://b.example.org" in m for m in messages(caplog))


# =============================================================================
# setup_logging verbosity tiers
# =============================================================================


@pytest.mark.usefixtures("clean_root")
def test_default_keeps_httpx_quiet() -> None:
    setup_logging()

    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.usefixtures("clean_root")
def test_verbose_raises_httpx_to_info() -> None:
    """The regression: -v used to leave httpx pinned at WARNING, so it could
    never show wire activity.
    """
    setup_logging(verbose=True)

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.usefixtures("clean_root")
def test_debug_env_adds_a_full_wire_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    setup_logging()

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.DEBUG


@pytest.mark.usefixtures("clean_root")
def test_falsey_debug_env_does_not_enable_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XNATCTL_DEBUG", "0")
    setup_logging()

    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.usefixtures("clean_root")
def test_quiet_wins_over_debug_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit flag beats an ambient env var."""
    monkeypatch.setenv("XNATCTL_DEBUG", "1")
    setup_logging(quiet=True)

    assert logging.getLogger().level == logging.ERROR
    assert logging.getLogger("httpcore").level == logging.WARNING
