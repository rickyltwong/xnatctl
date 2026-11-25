from __future__ import annotations

import logging

import httpx
import pytest

from xnatctl.core import retry
from xnatctl.core.retry import upload_with_retry


def _resp(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("POST", "https://x/import"))


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "xnatctl.core.cancellation.CancellationToken.sleep", lambda _self, _s: False
    )


@pytest.fixture
def debug_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="xnatctl.core.retry")
    return caplog


def _upload_400_then_200(body: str) -> None:
    responses = iter([_resp(400, body), _resp(200)])
    upload_with_retry(lambda: next(responses), label="t", max_retries=1)


def _snippet_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for m in caplog.messages if "retryable HTTP 400 body" in m]


def test_a_retryable_400_body_is_logged_on_one_line(
    monkeypatch: pytest.MonkeyPatch, debug_log: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(retry, "_retry_debug_snippets_emitted", 0)

    _upload_400_then_200("line one\nline two")

    assert _snippet_lines(debug_log) == ["t: retryable HTTP 400 body: line one line two"]


def test_an_empty_400_body_logs_no_snippet(
    monkeypatch: pytest.MonkeyPatch, debug_log: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(retry, "_retry_debug_snippets_emitted", 0)

    _upload_400_then_200("   ")

    assert _snippet_lines(debug_log) == []


def test_no_snippet_is_logged_once_the_quota_is_spent(
    monkeypatch: pytest.MonkeyPatch, debug_log: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(retry, "_retry_debug_snippets_emitted", retry._RETRY_DEBUG_MAX_SNIPPETS)

    _upload_400_then_200("transient")

    assert _snippet_lines(debug_log) == []
