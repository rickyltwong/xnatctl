"""ROB-01: failed transfers must exit nonzero in every output format.

Before the fix, ``raise SystemExit(1)`` lived only inside the table-output
branch of each upload helper, so ``-o json`` printed a ``success: false``
summary and returned 0 -- automation gated on exit code saw success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from xnatctl.cli.common import Context
from xnatctl.cli.session import (
    _upload_dicom_store,
    _upload_directory_parallel,
    _upload_gradual_dicom,
)
from xnatctl.core.output import OutputFormat
from xnatctl.models.progress import UploadSummary
from xnatctl.services.uploads import DICOMStoreSummary


class _FakeClient:
    base_url = "https://xnat.example.org"
    session_token = "tok"

    @property
    def is_authenticated(self) -> bool:
        return True


def _ctx(fmt: OutputFormat) -> Context:
    ctx = Context()
    ctx.client = cast(Any, _FakeClient())
    ctx.output_format = fmt
    ctx.quiet = True
    return ctx


def _upload_summary(*, success: bool) -> UploadSummary:
    return UploadSummary(
        success=success,
        total=10,
        succeeded=10 if success else 4,
        failed=0 if success else 6,
        duration=1.0,
        errors=[] if success else ["batch 2 failed: HTTP 500"],
        total_files=10,
        total_size_mb=1.0,
        batches_succeeded=1 if success else 0,
        batches_failed=0 if success else 1,
    )


def _store_summary(*, success: bool, tmp_path: Path) -> DICOMStoreSummary:
    return DICOMStoreSummary(
        total_files=10,
        sent=10 if success else 4,
        failed=0 if success else 6,
        log_dir=tmp_path,
        workspace=tmp_path,
        success=success,
    )


def _patch_service(monkeypatch: pytest.MonkeyPatch, method: str, summary: Any) -> None:
    class FakeService:
        def __init__(self, client: Any) -> None:
            pass

    def _return(self: Any, *args: Any, **kwargs: Any) -> Any:
        return summary

    setattr(FakeService, method, _return)
    monkeypatch.setattr("xnatctl.services.uploads.UploadService", FakeService)


@pytest.mark.parametrize("fmt", [OutputFormat.JSON, OutputFormat.TABLE])
def test_gradual_failed_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, fmt, tmp_path, capsys
) -> None:
    _patch_service(monkeypatch, "upload_dicom_gradual", _upload_summary(success=False))
    with pytest.raises(SystemExit) as exc:
        _upload_gradual_dicom(_ctx(fmt), tmp_path, "PROJ", "SUBJ", "SESS")
    assert exc.value.code == 1
    if fmt is OutputFormat.JSON:
        assert '"success"' in capsys.readouterr().out


@pytest.mark.parametrize("fmt", [OutputFormat.JSON, OutputFormat.TABLE])
def test_gradual_success_exits_zero(monkeypatch: pytest.MonkeyPatch, fmt, tmp_path) -> None:
    _patch_service(monkeypatch, "upload_dicom_gradual", _upload_summary(success=True))
    _upload_gradual_dicom(_ctx(fmt), tmp_path, "PROJ", "SUBJ", "SESS")  # no SystemExit


@pytest.mark.parametrize("fmt", [OutputFormat.JSON, OutputFormat.TABLE])
def test_parallel_failed_exits_nonzero(monkeypatch: pytest.MonkeyPatch, fmt, tmp_path) -> None:
    _patch_service(monkeypatch, "upload_dicom_parallel", _upload_summary(success=False))
    with pytest.raises(SystemExit) as exc:
        _upload_directory_parallel(
            _ctx(fmt), tmp_path, "PROJ", "SUBJ", "SESS", "u", "p", 4, 4, "tar", "none", True, False
        )
    assert exc.value.code == 1


def test_parallel_success_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_service(monkeypatch, "upload_dicom_parallel", _upload_summary(success=True))
    _upload_directory_parallel(
        _ctx(OutputFormat.JSON),
        tmp_path,
        "PROJ",
        "SUBJ",
        "SESS",
        "u",
        "p",
        4,
        4,
        "tar",
        "none",
        True,
        False,
    )


@pytest.mark.parametrize("fmt", [OutputFormat.JSON, OutputFormat.TABLE])
def test_cstore_failed_exits_nonzero(monkeypatch: pytest.MonkeyPatch, fmt, tmp_path) -> None:
    _patch_service(
        monkeypatch, "upload_dicom_store", _store_summary(success=False, tmp_path=tmp_path)
    )
    with pytest.raises(SystemExit) as exc:
        _upload_dicom_store(_ctx(fmt), tmp_path, "host", 8104, "AET", "XNATCTL", 4)
    assert exc.value.code == 1


def test_cstore_success_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_service(
        monkeypatch, "upload_dicom_store", _store_summary(success=True, tmp_path=tmp_path)
    )
    _upload_dicom_store(_ctx(OutputFormat.JSON), tmp_path, "host", 8104, "AET", "XNATCTL", 4)
