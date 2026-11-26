from __future__ import annotations

import threading
import time
from concurrent.futures import as_completed
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

        assert len(ran) < 50, f"the whole queue ran anyway: {len(ran)} tasks"
        assert token.cancelled is True

    def test_the_token_is_cancelled_for_workers(self) -> None:
        observed: list[bool] = []
        running = threading.Event()

        def work(token: CancellationToken) -> None:
            running.set()
            token.sleep(10)
            observed.append(token.cancelled)

        with pytest.raises(KeyboardInterrupt):
            with cancellable_pool(2) as (executor, token):
                executor.submit(work, token)
                assert running.wait(5), "worker never started"
                raise KeyboardInterrupt

        assert observed == [True]

    def test_normal_completion_runs_everything(self) -> None:
        with cancellable_pool(4) as (executor, token):
            futures = [executor.submit(lambda n=n: n * 2) for n in range(20)]
            results = sorted(f.result() for f in as_completed(futures))

        assert results == [n * 2 for n in range(20)]
        assert token.cancelled is False

    def test_workers_finish_before_the_body_returns(self) -> None:
        done = threading.Event()

        with cancellable_pool(2) as (executor, _token):
            executor.submit(done.set)

        assert done.is_set()

    def test_an_ordinary_exception_also_cancels(self) -> None:
        with pytest.raises(ValueError):
            with cancellable_pool(2) as (_executor, token):
                raise ValueError("boom")

        assert token.cancelled is True

    def test_a_shared_token_links_two_pools(self) -> None:
        token = CancellationToken()

        with cancellable_pool(2, token) as (_executor, first):
            assert first is token

        token.cancel()

        with cancellable_pool(2, token) as (_executor, second):
            assert second.cancelled is True


class TestUploadRetryLadderIsCancellable:
    @staticmethod
    def _always_503() -> httpx.Response:
        return httpx.Response(503, request=httpx.Request("POST", "https://x/import"))

    def test_a_cancelled_token_stops_the_ladder_immediately(self) -> None:
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

        assert calls == [1]
        assert elapsed < 1.0
        assert resp.status_code == 503

    def test_without_a_token_the_ladder_is_unchanged(self, monkeypatch) -> None:
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
        from xnatctl.services.upload.rest_batch import (
            _BatchUploadConfig,
            _create_and_upload_batch,
        )

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
            batch_id=7,
            config=_BatchUploadConfig(
                client=MagicMock(server_version=None, base_url="https://x"),
                username="u",
                password="p",
                session_token=None,
                verify_ssl=True,
                timeout=30,
                project="P",
                subject="S",
                session="E",
                import_handler="DICOM-zip",
                ignore_unparsable=True,
                overwrite="none",
                direct_archive=False,
            ),
            cancel_token=token,
        )

        assert result.cancelled is True
        assert result.success is False
        assert result.batch_id == 7
        assert not (tmp_path / "batch.zip").exists()
        assert result.archive_size == 0


def test_cancellation_maps_to_the_user_cancelled_exit_code() -> None:
    from xnatctl.cli.common import ExitCode, exit_code_for
    from xnatctl.core.exceptions import OperationCancelledError

    assert exit_code_for(OperationCancelledError("upload")) == ExitCode.USER_CANCELLED


def _service() -> Any:
    from xnatctl.core.client import XNATClient
    from xnatctl.services.upload import UploadService

    client = MagicMock(spec=XNATClient)
    client.base_url = "https://xnat.example.org"
    client.session_token = "tok"
    client.verify_ssl = True
    client.username = None
    client.password = None
    client.server_version = None
    return UploadService(client)


class TestTheSalvagePassIsAlsoCancellable:
    def test_the_retry_worker_is_given_the_shared_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.upload.gradual as uploads
        from xnatctl.core.cancellation import NULL_TOKEN

        warmup = 5
        failing = {"0007.dcm", "0008.dcm"}
        captured: list[tuple[str, object]] = []

        def fake_upload(**kwargs: object) -> tuple[str, bool, str]:
            name = Path(str(kwargs["file_path"])).name
            captured.append((name, kwargs.get("cancel_token")))
            already_tried = sum(1 for seen, _tok in captured if seen == name) > 1
            ok = name not in failing or already_tried
            return (name, ok, "" if ok else "transient 400")

        monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload)

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
        main_pass_tokens = [tok for name, tok in captured[5:] if name not in failing]
        assert main_pass_tokens, "no main-pass upload to compare against"
        assert all(tok is main_pass_tokens[0] for tok in retries), (
            "the salvage pass uses a different token than the main pass"
        )
