"""Telling a transient 400 from a permanent one.

XNAT's import service returns 400 both for "two uploads met in the same
session, try again" and for "that project does not exist". They were treated
identically, so a mislabeled upload retried every file five times over 62
seconds of backoff, then did it again in each of two salvage passes.

The signatures are read off the messages compiled into the deployed XNAT
1.9.2.1, not invented here -- see the constants in ``services.uploads``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from xnatctl.services.uploads import (
    PERMANENT_400_SIGNATURES,
    TRANSIENT_400_SIGNATURES,
    RetryBudget,
    is_permanent_400,
    upload_with_retry,
)


def _resp(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("POST", "https://x/import"))


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff instead of sleeping it; the ladder is 62s."""
    slept: list[float] = []
    monkeypatch.setattr(
        "xnatctl.core.cancellation.CancellationToken.sleep",
        lambda _self, s: bool(slept.append(s)),
    )
    return slept


class TestClassification:
    """Every string here was harvested from the running server."""

    @pytest.mark.parametrize(
        "body",
        [
            "Session processing in progress: 20240101_120000.  Concurrent modification is discouraged.",
            "Duplicate archive attempt. Prearchive session already archiving.",
            "Duplicate archive attempt.  Destination session in use.",
        ],
    )
    def test_concurrency_errors_are_transient(self, body: str) -> None:
        assert is_permanent_400(body) is False

    @pytest.mark.parametrize(
        "body",
        [
            "unable to deduce session label",
            "Unable to identify subject.",
            "unable to identify destination project",
            "Subject SUB01 does not exist in project NOPE, but not allowed to create new "
            "subjects, sorry",
            "Session already exists, retry with overwrite enabled",
            "Invalid modification of session UID via archive process.",
            "Invalid modification of session project via archive process.",
            "Session already contains a scan (3) with the same UID and number.",
            "SUB01 Already Exists for another Subject",
            "src uri is invalid.",
            "Expected a catalog file, however it was missing.",
            "Scan 4 has 12 non-DICOM (or non-parsable DICOM) files",
        ],
    )
    def test_configuration_and_data_errors_are_permanent(self, body: str) -> None:
        assert is_permanent_400(body) is True

    def test_an_unrecognised_body_is_treated_as_retryable(self) -> None:
        """Drift must not turn a working upload into a failing one.

        A denylist errs toward slow; an allowlist would err toward refusing
        uploads that currently succeed whenever XNAT rewords a message.
        """
        assert is_permanent_400("Something no XNAT version has said yet") is False

    def test_an_empty_body_is_treated_as_retryable(self) -> None:
        assert is_permanent_400("") is False

    def test_transient_wins_when_both_appear(self) -> None:
        """Bodies can carry both; the self-clearing one decides."""
        body = (
            "Duplicate archive attempt. Prearchive session already archiving. "
            "Invalid modification of session UID via archive process."
        )
        assert is_permanent_400(body) is False

    def test_the_two_signature_sets_do_not_overlap(self) -> None:
        assert not set(TRANSIENT_400_SIGNATURES) & set(PERMANENT_400_SIGNATURES)

    def test_matching_is_case_insensitive(self) -> None:
        """XNAT is inconsistent: "Unable to identify subject" vs "unable to deduce"."""
        assert is_permanent_400("UNABLE TO DEDUCE SESSION LABEL") is True


class TestRetryBehaviour:
    def test_a_permanent_400_costs_exactly_one_attempt(self, no_real_backoff: list[float]) -> None:
        """The headline fix: no 62 seconds spent confirming a typo."""
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return _resp(400, "unable to identify destination project")

        resp = upload_with_retry(attempt, label="t")

        assert len(calls) == 1
        assert no_real_backoff == [], "slept on a permanent failure"
        assert resp.status_code == 400

    def test_a_transient_400_is_still_retried(self, no_real_backoff: list[float]) -> None:
        """The behaviour 400-is-retryable existed for. Must not regress."""
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return _resp(400, "Session processing in progress: 2024.")

        upload_with_retry(attempt, label="t", max_retries=3)

        assert len(calls) == 4
        assert no_real_backoff == [2, 4, 8]

    def test_an_unknown_400_is_still_retried(self, no_real_backoff: list[float]) -> None:
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return _resp(400, "brand new wording")

        upload_with_retry(attempt, label="t", max_retries=2)

        assert len(calls) == 3

    def test_other_statuses_are_unaffected(self, no_real_backoff: list[float]) -> None:
        """The permanent check must apply to 400 only, not to 503."""
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return _resp(503, "unable to identify subject")

        upload_with_retry(attempt, label="t", max_retries=2)

        assert len(calls) == 3, "a 503 body must not be read as a permanent 400"

    def test_a_body_that_cannot_be_read_does_not_raise(self, no_real_backoff: list[float]) -> None:
        """A decode failure must not turn an HTTP failure into an exception."""

        class Undecodable(httpx.Response):
            @property  # type: ignore[misc]
            def text(self) -> str:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

        def attempt() -> httpx.Response:
            return Undecodable(400, request=httpx.Request("POST", "https://x"))

        resp = upload_with_retry(attempt, label="t", max_retries=1)

        assert resp.status_code == 400


class TestRetryBudget:
    def test_claims_succeed_until_the_budget_runs_out(self) -> None:
        budget = RetryBudget(10)

        assert budget.claim(6) is True
        assert budget.claim(3) is True
        assert budget.claim(3) is False
        assert budget.exhausted is False

    def test_an_exhausted_budget_stops_the_ladder(self, no_real_backoff: list[float]) -> None:
        """The backstop for signature drift across XNAT versions."""
        calls: list[int] = []

        def attempt() -> httpx.Response:
            calls.append(1)
            return _resp(400, "unrecognised wording")

        # Enough for the first 2s rung only.
        upload_with_retry(attempt, label="t", max_retries=5, retry_budget=RetryBudget(2))

        assert no_real_backoff == [2]
        assert len(calls) == 2, "kept retrying past the budget"

    def test_the_budget_is_shared_across_files(self, no_real_backoff: list[float]) -> None:
        """It bounds the operation, not each file -- that is the point."""
        budget = RetryBudget(2)

        def attempt() -> httpx.Response:
            return _resp(400, "unrecognised wording")

        upload_with_retry(attempt, label="a", max_retries=5, retry_budget=budget)
        before = len(no_real_backoff)
        upload_with_retry(attempt, label="b", max_retries=5, retry_budget=budget)

        assert len(no_real_backoff) == before, "the second file got its own budget"

    def test_no_budget_means_no_ceiling(self, no_real_backoff: list[float]) -> None:
        def attempt() -> httpx.Response:
            return _resp(400, "unrecognised wording")

        upload_with_retry(attempt, label="t", max_retries=3)

        assert no_real_backoff == [2, 4, 8]


class TestWarmupCircuitBreaker:
    """A uniformly-rejected warmup means the rest will be rejected too."""

    @staticmethod
    def _service():
        from unittest.mock import MagicMock

        from xnatctl.core.client import XNATClient
        from xnatctl.services.uploads import UploadService

        client = MagicMock(spec=XNATClient)
        client.base_url = "https://x"
        client.verify_ssl = True
        client.session_token = "TOK"
        client.username = "u"
        client.password = "p"
        return UploadService(client)

    def test_a_uniformly_rejected_warmup_aborts_before_the_parallel_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xnatctl.services.uploads as uploads

        files = []
        for i in range(20):
            f = tmp_path / f"{i:03d}.dcm"
            f.write_bytes(b"DICM")
            files.append(f)

        attempts: list[str] = []

        def always_rejects(**kwargs: object) -> tuple[str, bool, str]:
            attempts.append(str(kwargs.get("file_path")))
            return ("n", False, "unable to identify destination project")

        monkeypatch.setattr(uploads, "_upload_single_file_gradual", always_rejects)

        summary = self._service().upload_dicom_gradual_files(
            files=files, project="NOPE", subject="S", session="E", workers=4
        )

        assert summary.success is False
        # It stopped at the warmup rather than working through all 20.
        assert len(attempts) < len(files), f"kept going: {len(attempts)} of {len(files)}"
        assert "Check the project, subject and session labels" in summary.errors[0]
        assert "unable to identify destination project" in summary.errors[0]

    def test_mixed_warmup_failures_do_not_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a *uniform* rejection is evidence about the remaining files.

        One bad file among good ones is a per-file problem, and stopping the
        whole upload for it would be worse than the bug being fixed.
        """
        import xnatctl.services.uploads as uploads

        files = []
        for i in range(20):
            f = tmp_path / f"{i:03d}.dcm"
            f.write_bytes(b"DICM")
            files.append(f)

        seen: list[str] = []

        def sometimes(**kwargs: object) -> tuple[str, bool, str]:
            path = str(kwargs.get("file_path"))
            seen.append(path)
            return ("n", True, "") if not path.endswith("000.dcm") else ("n", False, "bad file")

        monkeypatch.setattr(uploads, "_upload_single_file_gradual", sometimes)

        summary = self._service().upload_dicom_gradual_files(
            files=files, project="P", subject="S", session="E", workers=4
        )

        # Every file was attempted -- the run was not cut short. (``seen`` can
        # exceed ``files``: the salvage passes retry the one that failed.)
        assert set(seen) == {str(f) for f in files}, "aborted despite most warmup files succeeding"
        assert summary.succeeded == len(files) - 1
