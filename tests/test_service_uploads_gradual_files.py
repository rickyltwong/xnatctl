from __future__ import annotations

import zipfile
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
    client.username = "user"
    client.password = "pass"
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
    client.username = "user"
    client.password = "pass"
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


def test_upload_dicom_gradual_files_ignores_non_dicom_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcm = tmp_path / "a.dcm"
    txt = tmp_path / "notes.txt"
    pdf = tmp_path / "report.pdf"
    hidden = tmp_path / ".hidden.dcm"
    dcm.write_text("x")
    txt.write_text("y")
    pdf.write_text("z")
    hidden.write_text("h")

    calls: list[Path] = []

    def fake_upload_one(**kwargs):
        calls.append(kwargs["file_path"])
        return (kwargs.get("display_path") or kwargs["file_path"].name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.username = "user"
    client.password = "pass"
    client.verify_ssl = True

    service = UploadService(client)
    summary = service.upload_dicom_gradual_files(
        files=[dcm, txt, pdf, hidden],
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        workers=1,
    )

    assert summary.success is True
    assert calls == [dcm]


def test_upload_dicom_gradual_directory_filters_to_dicom_like_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dcm = tmp_path / "scan1.dcm"
    ima = tmp_path / "scan2.ima"
    img = tmp_path / "scan3.img"
    raw = tmp_path / "IM00001"
    txt = tmp_path / "notes.txt"
    hidden = tmp_path / ".hidden.dcm"

    for path in (dcm, ima, img, raw, txt, hidden):
        path.write_text("payload")

    calls: list[Path] = []

    def fake_upload_one(**kwargs):
        calls.append(kwargs["file_path"])
        return (kwargs.get("display_path") or kwargs["file_path"].name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.username = "user"
    client.password = "pass"
    client.verify_ssl = True

    service = UploadService(client)
    summary = service.upload_dicom_gradual(
        source_path=tmp_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        workers=1,
    )

    assert summary.success is True
    assert Counter(calls) == Counter({dcm: 1, ima: 1, img: 1, raw: 1})
    assert txt not in calls
    assert hidden not in calls


def test_upload_dicom_gradual_zip_filters_to_dicom_like_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "dicoms.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/scan1.dcm", "dcm")
        zf.writestr("nested/scan2.ima", "ima")
        zf.writestr("nested/scan3.img", "img")
        zf.writestr("nested/IM00001", "raw")
        zf.writestr("nested/notes.txt", "txt")
        zf.writestr(".hidden.dcm", "hidden")

    calls: list[str] = []

    def fake_upload_one(**kwargs):
        display = kwargs.get("display_path") or kwargs["file_path"].name
        normalized = Path(display).as_posix()
        calls.append(normalized)
        return (normalized, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.username = "user"
    client.password = "pass"
    client.verify_ssl = True

    service = UploadService(client)
    summary = service.upload_dicom_gradual(
        source_path=archive,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        workers=1,
    )

    assert summary.success is True
    assert Counter(calls) == Counter(
        {
            "nested/scan1.dcm": 1,
            "nested/scan2.ima": 1,
            "nested/scan3.img": 1,
            "nested/IM00001": 1,
        }
    )
    assert "nested/notes.txt" not in calls
    assert ".hidden.dcm" not in calls


def test_upload_dicom_gradual_returns_no_dicom_files_for_non_dicom_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.txt").write_text("txt")

    def fake_upload_one(**kwargs):
        return (kwargs.get("display_path") or kwargs["file_path"].name, True, "")

    monkeypatch.setattr(uploads, "_upload_single_file_gradual", fake_upload_one)

    client = MagicMock()
    client.base_url = "https://example.org"
    client.session_token = "token"
    client.username = "user"
    client.password = "pass"
    client.verify_ssl = True

    service = UploadService(client)
    summary = service.upload_dicom_gradual(
        source_path=tmp_path,
        project="PROJ",
        subject="SUBJ",
        session="SESS",
        workers=1,
    )

    assert summary.success is False
    assert summary.total == 0
    assert summary.errors == ["No DICOM files found"]
