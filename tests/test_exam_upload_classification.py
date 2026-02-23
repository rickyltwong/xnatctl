from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


def _touch(path: Path, payload: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)


class _ExamRootClassification(Protocol):
    dicom_files: Sequence[Path]
    resource_dirs: Sequence[Path]
    misc_files: Sequence[Path]


def _classify(exam_root: Path) -> _ExamRootClassification:
    from xnatctl.core.exam import classify_exam_root

    return classify_exam_root(exam_root)


def test_classify_exam_root_splits_dicom_resources_and_misc(tmp_path: Path) -> None:
    exam = tmp_path / "P001_20240517"
    exam.mkdir()

    # DICOM series directory
    _touch(exam / "1_series" / "a.dcm")
    _touch(exam / "1_series" / "b")  # extensionless DICOM-like

    # Top-level non-DICOM directories
    _touch(exam / "Physio" / "trace.txt")
    _touch(exam / "Protocol" / "proto.pdf")

    # Top-level non-DICOM file
    _touch(exam / "notes.txt", payload="hello")

    result = _classify(exam)

    dicom_rel = {p.relative_to(exam).as_posix() for p in result.dicom_files}
    assert "1_series/a.dcm" in dicom_rel
    assert "1_series/b" in dicom_rel

    res_labels = {p.name for p in result.resource_dirs}
    assert res_labels == {"Physio", "Protocol"}

    misc_names = {p.name for p in result.misc_files}
    assert misc_names == {"notes.txt"}


def test_classify_exam_root_ignores_hidden_paths(tmp_path: Path) -> None:
    exam = tmp_path / "EXAM"
    exam.mkdir()

    _touch(exam / ".hidden" / "x.dcm")
    _touch(exam / ".DS_Store")
    _touch(exam / "series" / "ok.dcm")

    result = _classify(exam)

    rel = {p.relative_to(exam).as_posix() for p in result.dicom_files}
    assert rel == {"series/ok.dcm"}
