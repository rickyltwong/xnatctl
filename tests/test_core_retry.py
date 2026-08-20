"""Tests for the generic retry primitive in ``core/retry.py``.

The status-code sets, backoff helpers, and the upload response ladder that
also live there are covered where their consumers are tested
(``test_core_client_retry.py``, ``test_uploaders_common.py``,
``test_upload_400_policy.py``); this file covers ``retry_call`` itself.
"""

from __future__ import annotations

import pytest

from xnatctl.core.retry import retry_call


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps and return immediately."""
    recorded: list[float] = []
    monkeypatch.setattr("xnatctl.core.retry.time.sleep", lambda s: recorded.append(s))
    return recorded


class _Transient(Exception):
    pass


def _retry_transient(exc: BaseException) -> bool:
    return isinstance(exc, _Transient)


class TestRetryCall:
    def test_returns_the_result_on_first_success(self, sleeps: list[float]) -> None:
        assert retry_call(lambda: 42, retryable=_retry_transient) == 42
        assert sleeps == []

    def test_retries_until_success(self, sleeps: list[float]) -> None:
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise _Transient("not yet")
            return "done"

        assert retry_call(fn, retryable=_retry_transient, max_attempts=3) == "done"
        assert len(calls) == 3

    def test_a_non_retryable_exception_propagates_immediately(self, sleeps: list[float]) -> None:
        calls: list[int] = []

        def fn() -> None:
            calls.append(1)
            raise RuntimeError("bug")

        with pytest.raises(RuntimeError, match="bug"):
            retry_call(fn, retryable=_retry_transient, max_attempts=5)
        assert len(calls) == 1
        assert sleeps == []

    def test_exhaustion_raises_the_last_exception(self, sleeps: list[float]) -> None:
        calls: list[int] = []

        def fn() -> None:
            calls.append(1)
            raise _Transient(f"attempt {len(calls)}")

        with pytest.raises(_Transient, match="attempt 3"):
            retry_call(fn, retryable=_retry_transient, max_attempts=3)
        assert len(calls) == 3

    def test_backoff_doubles_from_the_base(self, sleeps: list[float]) -> None:
        def fn() -> None:
            raise _Transient("always")

        with pytest.raises(_Transient):
            retry_call(fn, retryable=_retry_transient, max_attempts=4, backoff_base=5.0)
        assert sleeps == [5.0, 10.0, 20.0]

    def test_no_sleep_after_the_final_attempt(self, sleeps: list[float]) -> None:
        def fn() -> None:
            raise _Transient("always")

        with pytest.raises(_Transient):
            retry_call(fn, retryable=_retry_transient, max_attempts=1)
        assert sleeps == []

    def test_rejects_a_nonsensical_attempt_count(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            retry_call(lambda: 1, retryable=_retry_transient, max_attempts=0)

    @pytest.mark.parametrize("bad_base", [-1.0, float("nan"), float("inf")])
    def test_rejects_a_nonsensical_backoff_base(self, bad_base: float) -> None:
        """A bad delay must fail loudly up front, not mask the real exception."""
        with pytest.raises(ValueError, match="backoff_base"):
            retry_call(lambda: 1, retryable=_retry_transient, backoff_base=bad_base)
