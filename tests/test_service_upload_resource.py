"""Temp-file lifecycle tests for UploadService.upload_resource (ROB-12).

A directory source is zipped to a NamedTemporaryFile(delete=False) before
upload. That zip used to leak on every exit path: a 50 GB resource directory
left a 50 GB zip behind on each call, and `session upload-exam` calls this once
per resource directory in the exam.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.models.progress import OperationPhase
from xnatctl.services.uploads import UploadService


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

    with patch("xnatctl.services.uploads.tempfile.NamedTemporaryFile", tracking):
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
    with patch("xnatctl.services.uploads.httpx.Client", return_value=_mock_http(200)):
        summary = service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert summary.success is True
    assert temp_zips, "a temp zip should have been created for a directory source"
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip: {zip_path}"


def test_temp_zip_removed_when_upload_fails(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """The broad `except` returns a failure summary rather than raising, so the
    cleanup has to live in a `finally`, not on the success path."""
    with patch(
        "xnatctl.services.uploads.httpx.Client",
        side_effect=RuntimeError("connection exploded"),
    ):
        summary = service.upload_resource("XNAT_E00001", "BIDS", source_dir)

    assert summary.success is False
    assert temp_zips
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip on the failure path: {zip_path}"


def test_temp_zip_removed_when_setup_fails_before_the_request(
    service: UploadService, source_dir: Path, temp_zips: list[Path]
) -> None:
    """Cleanup must cover the setup work between zipping and the request too.

    That window (stat, progress reporting, param building) used to sit outside
    the try block, so a failure there leaked the zip even after ROB-12's finally
    was added.
    """

    def exploding_callback(progress: object) -> None:
        # An earlier callback fires before the zip exists; only blow up on the
        # UPLOADING phase, which is inside the window under test.
        if getattr(progress, "phase", None) is OperationPhase.UPLOADING:
            raise RuntimeError("callback exploded")

    summary = service.upload_resource(
        "XNAT_E00001", "BIDS", source_dir, progress_callback=exploding_callback
    )

    assert summary.success is False
    assert temp_zips
    for zip_path in temp_zips:
        assert not zip_path.exists(), f"leaked temp zip before upload: {zip_path}"


def test_file_source_is_not_deleted(
    service: UploadService, tmp_path: Path, temp_zips: list[Path]
) -> None:
    """Only the temp zip is cleaned up; a caller's own file must survive."""
    src = tmp_path / "payload.txt"
    src.write_text("keep me")

    with patch("xnatctl.services.uploads.httpx.Client", return_value=_mock_http(200)):
        summary = service.upload_resource("XNAT_E00001", "MISC", src)

    assert summary.success is True
    assert src.exists(), "a file source must never be unlinked"
    assert temp_zips == [], "no temp zip is needed for a file source"
