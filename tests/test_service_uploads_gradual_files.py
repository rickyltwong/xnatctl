from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import xnatctl.services.uploads as uploads
from xnatctl.services.uploads import UploadService


def test_upload_dicom_gradual_files_uses_explicit_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f1 = tmp_path / "a.dcm"
    f2 = tmp_path / "b.dcm"
    f3 = tmp_path / "c.dcm"
    f1.write_text("x")
    f2.write_text("y")
    f3.write_text("z")

    calls: list[Path] = []

    def fake_upload_one(**kwargs):
        calls.append(kwargs["file_path"])
        return (kwargs.get("display_path") or kwargs["file_path"].name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.verify_ssl = True

    service = UploadService(client)
    summary = service.upload_dicom_gradual_files(
        files=[f1, f2],
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        workers=1,
    )

    assert summary.success is True
    assert Counter(calls) == Counter({f1: 1, f2: 1})
    assert f3 not in calls


def test_upload_dicom_gradual_files_rejects_duplicate_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f1 = tmp_path / "a.dcm"
    f1.write_text("x")

    (tmp_path / "sub").mkdir()

    dup = tmp_path / "sub" / ".." / "a.dcm"

    def fake_upload_one(**kwargs):
        return (kwargs.get("display_path") or kwargs["file_path"].name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.verify_ssl = True

    service = UploadService(client)

    with pytest.raises(ValueError, match=r"(?i)duplicate"):
        service.upload_dicom_gradual_files(
            files=[f1, dup],
            project="PROJ",
            subject="SUBJ",
            session="SESS",
            workers=1,
        )
