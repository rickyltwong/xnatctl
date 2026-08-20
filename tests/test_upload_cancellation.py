"""Ctrl+C must actually stop a parallel operation.

Before this, every batch was submitted to the pool up front and the interrupt
unwound into ``ThreadPoolExecutor.__exit__``, whose ``shutdown(wait=True)``
runs the entire queue to completion first. The user asked it to stop and it
kept uploading.

Synchronisation here is by :class:`threading.Event`, never by sleeping and
hoping -- these are thread tests and the suite has a 120s per-test backstop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.cancellation import NULL_TOKEN, CancellationToken, cancellable_pool


class TestCancellationToken:
    def test_starts_uncancelled(self) -> None:
        assert CancellationToken().cancelled is False

    def test_cancel_sets_the_flag(self) -> None:
        token = CancellationToken()
        token.cancel()
        assert token.cancelled is True

    def test_cancel_is_idempotent(self) -> None:
        token = CancellationToken()
        token.cancel()
        token.cancel()
        assert token.cancelled is True

    def test_sleep_returns_false_when_it_runs_to_completion(self) -> None:
        assert CancellationToken().sleep(0.01) is False

    def test_sleep_returns_immediately_once_cancelled(self) -> None:
        """The point of the whole exercise: no waiting out a backoff."""
        token = CancellationToken()
        token.cancel()

        started = time.monotonic()
        cancelled = token.sleep(30)

        assert cancelled is True
        assert time.monotonic() - started < 1.0

    def test_sleep_wakes_when_another_thread_cancels(self) -> None:
        token = CancellationToken()
        entered = threading.Event()
        result: list[bool] = []

        def sleeper() -> None:
            entered.set()
            result.append(token.sleep(30))

        thread = threading.Thread(target=sleeper)
        thread.start()
        assert entered.wait(5), "sleeper thread never started"
        token.cancel()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert result == [True]

    def test_null_token_is_never_cancelled(self) -> None:
        assert NULL_TOKEN.cancelled is False


class TestCancellablePool:
    def test_queued_work_never_runs_after_an_interrupt(self) -> None:
        """The core guarantee: the backlog is dropped, not drained.

        One worker occupies the single thread and blocks. The remaining 49 sit
        in the queue. An interrupt in the body must strand them.
        """
        first_running = threading.Event()
        release = threading.Event()
        ran: list[int] = []
        lock = threading.Lock()

        def work(n: int) -> int:
            with lock:
                ran.append(n)
            if n == 0:
                first_running.set()
                release.wait(10)
            return n

        with pytest.raises(KeyboardInterrupt):
            with cancellable_pool(1) as (executor, token):
                for n in range(50):
                    executor.submit(work, n)
                assert first_running.wait(5), "first task never started"
                release.set()
                raise KeyboardInterrupt

        # Task 0 was already running, so it completes. A couple more may have
        # been picked up in the race before the queue was dropped; what matters
        # is that the backlog did not drain.
        assert len(ran) < 50, f"the whole queue ran anyway: {len(ran)} tasks"
        assert token.cancelled is True

    def test_the_token_is_cancelled_for_workers(self) -> None:
        """Workers can see the request, so in-flight units can bail early."""
        observed: list[bool] = []
        running = threading.Event()

        def work(token: CancellationToken) -> None:
            running.set()
            # Waiting on the token is the mechanism itself: the worker is
            # released by the cancellation, not by a test-only event.
            token.sleep(10)
            observed.append(token.cancelled)

        with pytest.raises(KeyboardInterrupt):
            with cancellable_pool(2) as (executor, token):
                executor.submit(work, token)
                assert running.wait(5), "worker never started"
                raise KeyboardInterrupt

        # The pool is joined on the way out, so the worker has already run.
        assert observed == [True]

    def test_normal_completion_runs_everything(self) -> None:
        """No interrupt, no behaviour change: this replaces the plain pool."""
        with cancellable_pool(4) as (executor, token):
            futures = [executor.submit(lambda n=n: n * 2) for n in range(20)]
            results = sorted(f.result() for f in as_completed(futures))

        assert results == [n * 2 for n in range(20)]
        assert token.cancelled is False

    def test_workers_finish_before_the_body_returns(self) -> None:
        """Callers delete temp archives in their own ``finally``.

        Returning while a worker still holds one open would trade a slow exit
        for a corrupt one, so the pool is always joined.
        """
        done = threading.Event()

        with cancellable_pool(2) as (executor, _token):
            executor.submit(done.set)

        assert done.is_set()

    def test_an_ordinary_exception_also_cancels(self) -> None:
        """Not only KeyboardInterrupt -- any escape means stop queuing work."""
        with pytest.raises(ValueError):
            with cancellable_pool(2) as (_executor, token):
                raise ValueError("boom")

        assert token.cancelled is True

    def test_a_shared_token_links_two_pools(self) -> None:
        """The gradual upload's retry pass reuses the main pass's token."""
        token = CancellationToken()

        with cancellable_pool(2, token) as (_executor, first):
            assert first is token

        token.cancel()

        with cancellable_pool(2, token) as (_executor, second):
            assert second.cancelled is True


class TestUploadRetryLadderIsCancellable:
    """The retry backoff is where an interrupt used to hang.

    The ladder is 2+4+8+16+32s. Without a cancellable wait, a Ctrl+C during a
    failing upload waits out the remaining rungs per in-flight batch.
    """

    @staticmethod
    def _always_503() -> httpx.Response:
        return httpx.Response(503, request=httpx.Request("POST", "https://x/import"))

    def test_a_cancelled_token_stops_the_ladder_immediately(self) -> None:
        """Already cancelled: no request is made, and the reason is not faked.

        Reaching the end with no response is normally "all retries exhausted",
        which would blame the server for the user's Ctrl+C. Cancellation gets
        its own typed error instead.
        """
        from xnatctl.core.exceptions import OperationCancelledError
        from xnatctl.core.retry import upload_with_retry

        token = CancellationToken()
        token.cancel()
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return self._always_503()

        started = time.monotonic()
        with pytest.raises(OperationCancelledError) as excinfo:
            upload_with_retry(attempt, label="t", cancel_token=token)
        elapsed = time.monotonic() - started

        assert calls == []
        assert elapsed < 1.0
        assert "exhausted" not in str(excinfo.value)

    def test_cancelling_mid_ladder_abandons_the_remaining_rungs(self) -> None:
        from xnatctl.core.retry import upload_with_retry

        token = CancellationToken()
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                token.cancel()
            return self._always_503()

        started = time.monotonic()
        resp = upload_with_retry(attempt, label="t", cancel_token=token)
        elapsed = time.monotonic() - started

        # One attempt, then the 2s backoff returns immediately and the loop
        # breaks rather than climbing to 4, 8, 16, 32.
        assert calls == [1]
        assert elapsed < 1.0
        # The last response is still returned -- cancellation is not a crash.
        assert resp.status_code == 503

    def test_without_a_token_the_ladder_is_unchanged(self, monkeypatch) -> None:
        """The default must not alter existing retry behaviour."""
        from xnatctl.core.retry import upload_with_retry

        slept: list[float] = []
        monkeypatch.setattr(
            "xnatctl.core.cancellation.CancellationToken.sleep",
            lambda _self, s: bool(slept.append(s)),
        )
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return self._always_503()

        upload_with_retry(attempt, label="t", max_retries=3)

        assert len(calls) == 4, "should still make every attempt"
        assert slept == [2, 4, 8], "and still climb the same backoff ladder"


class TestBatchWorkerHonoursCancellation:
    def test_a_cancelled_batch_is_marked_cancelled_not_failed(self, tmp_path) -> None:
        """A dropped batch is not an error the server rejected.

        Reporting it as a failure would tell the user their data was refused
        when in fact they stopped it themselves.
        """
        from xnatctl.services.uploads import _create_and_upload_batch

        token = CancellationToken()
        token.cancel()
        source = tmp_path / "src"
        source.mkdir()
        dicom = source / "a.dcm"
        dicom.write_bytes(b"x")

        result = _create_and_upload_batch(
            batch=[dicom],
            archive_path=tmp_path / "batch.zip",
            source_path=source,
            archive_format="zip",
            base_url="https://x",
            username="u",
            password="p",
            session_token=None,
            verify_ssl=True,
            timeout=30,
            batch_id=7,
            project="P",
            subject="S",
            session="E",
            import_handler="DICOM-zip",
            ignore_unparsable=True,
            overwrite="none",
            direct_archive=False,
            cancel_token=token,
        )

        assert result.cancelled is True
        assert result.success is False
        assert result.batch_id == 7
        # No archive was built: the point is that it did no work at all.
        assert not (tmp_path / "batch.zip").exists()
        assert result.archive_size == 0


def test_cancellation_maps_to_the_user_cancelled_exit_code() -> None:
    """A cancelled run must not look like a general error to a script."""
    from xnatctl.cli.common import ExitCode, exit_code_for
    from xnatctl.core.exceptions import OperationCancelledError

    assert exit_code_for(OperationCancelledError("upload")) == ExitCode.USER_CANCELLED


@contextmanager
def _null_scope() -> Iterator[None]:
    """Stand in for the thread-local HTTP client scope; nothing to set up."""
    yield


def _service() -> Any:
    """An UploadService whose client is never actually called."""
    from xnatctl.core.client import XNATClient
    from xnatctl.services.uploads import UploadService

    client = MagicMock(spec=XNATClient)
    client.base_url = "https://xnat.example.org"
    client.session_token = "tok"
    client.verify_ssl = True
    client.username = None
    client.password = None
    return UploadService(client)


class TestTheSalvagePassIsAlsoCancellable:
    """The retry pass shipped with cancellation missing.

    ``upload_dicom_gradual_files`` makes a second, lower-concurrency pass over
    files that failed the first time. Its pool was built from the shared token
    but ``_submit_retry`` never passed ``cancel_token`` down, so every worker
    ran on ``NULL_TOKEN``: after Ctrl+C each in-flight file sat out its whole
    2+4+8+16+32s retry ladder before the pool could shut down. Precisely the
    wait cooperative cancellation exists to remove -- reintroduced in the one
    pass whose files are already known to be failing, which is where a user is
    most likely to give up and press Ctrl+C.

    The end-to-end check that signed this feature off only exercised the main
    pass, so it saw nothing.
    """

    def test_the_retry_worker_is_given_the_shared_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.uploads as uploads
        from xnatctl.core.cancellation import NULL_TOKEN

        # The first five files are the warmup batch; they must succeed or the
        # circuit-breaker aborts the whole upload before any salvage happens.
        warmup = 5
        failing = {"0007.dcm", "0008.dcm"}
        captured: list[tuple[str, object]] = []

        def fake_upload(**kwargs: object) -> tuple[str, bool, str]:
            name = Path(str(kwargs["file_path"])).name
            captured.append((name, kwargs.get("cancel_token")))
            already_tried = sum(1 for seen, _tok in captured if seen == name) > 1
            ok = name not in failing or already_tried  # succeeds on the retry
            return (name, ok, "" if ok else "transient 400")

        monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload)
        monkeypatch.setattr(uploads, "_gradual_http_clients_scope", _null_scope)

        files = []
        for i in range(warmup + 5):
            path = tmp_path / f"{i:04d}.dcm"
            path.write_bytes(b"\0" * 128 + b"DICM")
            files.append(path)

        _service().upload_dicom_gradual_files(
            files=files, project="P", subject="S", session="E", workers=2
        )

        retries = [tok for name, tok in captured if name in failing][2:]

        assert retries, "the salvage pass never ran; this test would prove nothing"
        assert NULL_TOKEN not in retries, (
            "the salvage pass runs uncancellable: Ctrl+C would wait out every retry ladder"
        )
        # Baseline is a main-pass file, not captured[0]: the first five are the
        # sequential warmup, which deliberately carries no token (Ctrl+C
        # interrupts it on the main thread directly).
        main_pass_tokens = [tok for name, tok in captured[5:] if name not in failing]
        assert main_pass_tokens, "no main-pass upload to compare against"
        assert all(tok is main_pass_tokens[0] for tok in retries), (
            "the salvage pass uses a different token than the main pass"
        )
