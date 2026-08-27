"""Temp-file lifecycle tests for UploadService.upload_resource.

A directory source is zipped to a NamedTemporaryFile(delete=False) before
upload. That zip must be removed on every exit path: a leak leaves a 50 GB
zip behind for a 50 GB resource directory on each call, and `session
upload-exam` calls this once per resource directory in the exam.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.core.exceptions import BatchOperationError, SessionExpiredError, UploadError
from xnatctl.models.progress import OperationPhase, UploadProgress, UploadSummary
from xnatctl.services.upload import UploadService


@pytest.fixture
def temp_zips(tmp_path: Path) -> list[Path]:
    """Redirect the service's temp zips into tmp_path and record their paths."""
    created: list[Path] = []
    real = tempfile.NamedTemporaryFile

    def tracking(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        kwargs["dir"] = str(tmp_path)
        handle = real(*args, **kwargs)  # type: ignore[arg-type]
        created.append(Path(handle.name))
        return handle

    with patch("xnatctl.services.upload.resources.tempfile.NamedTemporaryFile", tracking):
        yield created


@pytest.fixture
def service() -> UploadService:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    client.session_token = "TOK"
    client.verify_ssl = True
    client.username = "user"
    client.password = "pass"
    return UploadService(client)


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    d = tmp_path / "resource_src"
    d.mkdir()
    (d / "a.txt").write_text("alpha")
    (d / "b.txt").write_text("beta")
    return d


def _mock_http(status: int = 200) -> MagicMock:
    """Patchable httpx.Client whose PUT returns ``status``."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = "ok" if status < 400 else "boom"
    http = MagicMock()
    http.put.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = http
    ctx.__exit__.return_value = False
    return ctx


def test_temp_zip_removed_after_successful_upload(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    with patch("xnatctl.services.upload.resources.httpx.Client", return_value=_mock_http(200)):
        summary = service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert summary.success is True
    assert temp_zips, "a temp zip should have been created for a directory source"
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip: {zip_path}"


def test_temp_zip_removed_when_upload_fails(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """A single-target upload raises on failure; the `finally` still cleans up.

    The cleanup lives in a `finally` rather than on the success path precisely
    so it covers the raising branch too.
    """
    with patch(
        "xnatctl.services.upload.resources.httpx.Client",
        side_effect=RuntimeError("connection exploded"),
    ):
        with pytest.raises(UploadError):
            service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert temp_zips
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip on the failure path: {zip_path}"


def test_temp_zip_removed_when_setup_fails_before_the_request(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """Cleanup must cover the setup work between zipping and the request too.

    That window (stat, progress reporting, param building) must sit inside
    the try block, or a failure there leaks the zip despite the finally.
    """

    def exploding_callback(progress: object) -> None:
        # An earlier callback fires before the zip exists; only blow up on the
        # UPLOADING phase, which is inside the window under test.
        if getattr(progress, "phase", None) is OperationPhase.UPLOADING:
            raise RuntimeError("callback exploded")

    with pytest.raises(UploadError):
        service.upload_resource(
            "XNAT_E00001", "BIDS", source_dir, progress_callback=exploding_callback
        )

    assert temp_zips
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip before upload: {zip_path}"


def test_file_source_is_not_deleted(
    service: UploadService, tmp_path: Path, temp_zips: list[Path]
) -> None:
    """Only the temp zip is cleaned up; a caller's own file must survive."""
    src = tmp_path / "payload.txt"
    src.write_text("keep me")

    with patch("xnatctl.services.upload.resources.httpx.Client", return_value=_mock_http(200)):
        summary = service.upload_resource("XNAT_E00001", "MISC", src)

    assert summary.success is True
    assert src.exists(), "a file source must never be unlinked"
    assert temp_zips == [], "no temp zip is needed for a file source"


def test_typed_failure_propagates_unwrapped(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """A typed client-layer failure passes through instead of being stringified."""
    with patch(
        "xnatctl.services.upload.resources.httpx.Client",
        side_effect=SessionExpiredError("https://xnat.example.org"),
    ):
        with pytest.raises(SessionExpiredError):
            service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip on the typed-failure path: {zip_path}"


def test_upload_summary_raise_for_status_raises_on_failed_batch() -> None:
    """UploadSummary.raise_for_status mirrors httpx.Response.raise_for_status."""
    summary = UploadSummary(
        success=False,
        total=4,
        succeeded=1,
        failed=3,
        duration=1.0,
        errors=["batch 2 rejected"],
    )

    with pytest.raises(BatchOperationError) as excinfo:
        summary.raise_for_status()

    err = excinfo.value
    assert err.succeeded == 1
    assert err.failed == 3
    assert err.errors == ["batch 2 rejected"]
    assert err.details["failed"] == 3
    assert "upload" in str(err)


def test_upload_summary_raise_for_status_noop_on_success() -> None:
    summary = UploadSummary(success=True, total=2, succeeded=2, failed=0, duration=1.0)
    assert summary.raise_for_status() is None


def test_unexpected_failure_wraps_with_cause_and_source_path(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """The wrapper preserves the original exception and names the source."""
    boom = RuntimeError("connection exploded")
    with patch("xnatctl.services.upload.resources.httpx.Client", side_effect=boom):
        with pytest.raises(UploadError) as excinfo:
            service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert excinfo.value.__cause__ is boom
    assert excinfo.value.details["file"] == str(source_dir)


def test_typed_failure_is_the_same_exception_object(
    service: UploadService, source_dir: Path
) -> None:
    """Pass-through means the identical object, not a lookalike."""
    exc = SessionExpiredError("https://xnat.example.org")
    with patch("xnatctl.services.upload.resources.httpx.Client", side_effect=exc):
        with pytest.raises(SessionExpiredError) as excinfo:
            service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert excinfo.value is exc


def test_archive_build_failure_wraps_and_cleans_the_temp_zip(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """A failed zip build is part of the upload contract and must not leak."""
    with patch(
        "xnatctl.services.upload.resources.shutil.make_archive",
        side_effect=OSError("No space left on device"),
    ):
        with pytest.raises(UploadError) as excinfo:
            service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert temp_zips, "the temp zip placeholder is created before the archive build"
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip after a failed build: {zip_path}"


def test_error_progress_fires_and_a_raising_callback_cannot_mask(
    service: UploadService, source_dir: Path
) -> None:
    """The failure notification is best-effort: it fires, and if the callback
    itself raises it is suppressed so the typed failure still propagates.
    """
    phases: list[OperationPhase] = []

    def callback(progress: UploadProgress) -> None:
        phases.append(progress.phase)
        if progress.phase is OperationPhase.ERROR:
            raise RuntimeError("callback exploded")

    exc = SessionExpiredError("https://xnat.example.org")
    with patch("xnatctl.services.upload.resources.httpx.Client", side_effect=exc):
        with pytest.raises(SessionExpiredError) as excinfo:
            service.upload_resource("XNAT_E00001", "BIDS", source_dir, progress_callback=callback)

    assert excinfo.value is exc
    assert OperationPhase.ERROR in phases
