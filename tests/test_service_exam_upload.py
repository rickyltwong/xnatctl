from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from xnatctl.models.progress import UploadSummary
from xnatctl.services.exam_upload import (
    ExamOutcome,
    ExamUploadResult,
    ExamUploadService,
)


def _make_exam(
    root: Path, *, dicom: bool = True, resource_dir: bool = True, misc: bool = True
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if dicom:
        scan = root / "DICOM" / "scan"
        scan.mkdir(parents=True)
        (scan / "a.dcm").write_text("payload")
    if resource_dir:
        physio = root / "Physio"
        physio.mkdir()
        (physio / "trace.txt").write_text("payload")
    if misc:
        (root / "notes.txt").write_text("payload")
    return root


def _ok_summary(n: int) -> UploadSummary:
    return UploadSummary(success=True, total=n, succeeded=n, failed=0, duration=0.0, errors=[])


class _UploadStub:
    last_files: tuple[Path, ...] | None = None

    def __init__(self, client: Any) -> None:
        self.client = client

    def upload_dicom_gradual_files(self, *, files: tuple[Path, ...], **_: Any) -> UploadSummary:
        _UploadStub.last_files = files
        return _ok_summary(len(files))


class _ResourceSpy:
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, client: Any) -> None:
        self.client = client

    def create(self, *, session_id: str, resource_label: str, project: str) -> None:
        _ResourceSpy.calls.append(("create", {"label": resource_label, "session": session_id}))

    def upload_directory(
        self, *, session_id: str, resource_label: str, directory_path: Path, project: str
    ) -> None:
        _ResourceSpy.calls.append(("upload_directory", {"label": resource_label}))

    def upload_file(
        self,
        *,
        session_id: str,
        resource_label: str,
        file_path: Path,
        project: str,
        extract: bool,
    ) -> None:
        _ResourceSpy.calls.append(("upload_file", {"label": resource_label, "extract": extract}))


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> None:
    _UploadStub.last_files = None
    _ResourceSpy.calls = []
    monkeypatch.setattr("xnatctl.services.upload.UploadService", _UploadStub)
    monkeypatch.setattr("xnatctl.services.resources.ResourceService", _ResourceSpy)
    monkeypatch.setattr(time, "sleep", lambda _: None)


def _service() -> ExamUploadService:
    return ExamUploadService(MagicMock())


def _upload(
    service: ExamUploadService,
    plan: Any,
    **overrides: Any,
) -> ExamUploadResult:
    kwargs: dict[str, Any] = {
        "project": "P",
        "subject": "S",
        "session": "E",
        "workers": 4,
        "direct_archive": True,
        "skip_resources": False,
        "attach_only": False,
        "wait": 30,
        "wait_interval": 5,
    }
    kwargs.update(overrides)
    return service.upload_exam(plan, **kwargs)


def test_plan_classifies_and_validates(tmp_path: Path) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")

    assert plan.resource_labels == ("Physio",)
    assert plan.misc_label == "MISC"
    assert len(plan.classification.dicom_files) == 1
    assert len(plan.classification.misc_files) == 1


def test_happy_path_uploads_dicom_and_attaches_resources(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: "EXPT_1")

    result = _upload(service, plan)

    assert result.outcome is ExamOutcome.COMPLETE
    assert result.dicom_uploaded == 1
    assert result.attached_resource_dirs == 1
    assert result.attached_misc_files == 1
    assert _UploadStub.last_files == plan.classification.dicom_files
    assert _ResourceSpy.calls == [
        ("create", {"label": "Physio", "session": "EXPT_1"}),
        ("upload_directory", {"label": "Physio"}),
        ("create", {"label": "MISC", "session": "EXPT_1"}),
        ("upload_file", {"label": "MISC", "extract": True}),
    ]


def test_no_dicom_is_an_error_outcome(tmp_path: Path) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam", dicom=False), "MISC")

    result = _upload(service, plan)

    assert result.outcome is ExamOutcome.NO_DICOM
    assert result.error_message == f"No DICOM files found under: {plan.exam_root}"


def test_a_failed_dicom_upload_aggregates_the_error(tmp_path: Path, monkeypatch) -> None:
    class _FailingUpload(_UploadStub):
        def upload_dicom_gradual_files(self, *, files: tuple[Path, ...], **_: Any) -> UploadSummary:
            return UploadSummary(
                success=False,
                total=3,
                succeeded=1,
                failed=2,
                duration=0.0,
                errors=["boom-a", "boom-b", "boom-c", "boom-d"],
            )

    monkeypatch.setattr("xnatctl.services.upload.UploadService", _FailingUpload)
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")

    result = _upload(service, plan)

    assert result.outcome is ExamOutcome.DICOM_FAILED
    assert result.error_message == "DICOM upload failed (1/3 succeeded): boom-a; boom-b; boom-c"


def test_attach_only_skips_dicom_upload(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: "EXPT_1")

    result = _upload(service, plan, attach_only=True)

    assert result.outcome is ExamOutcome.COMPLETE
    assert result.attach_only is True
    assert result.dicom_uploaded == 0
    assert _UploadStub.last_files is None


def test_attach_only_with_no_dicom_is_not_an_error(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam", dicom=False), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: "EXPT_1")

    result = _upload(service, plan, attach_only=True)

    assert result.outcome is ExamOutcome.COMPLETE
    assert result.error_message is None
    assert result.dicom_total == 0


def test_skip_resources_returns_no_resources(tmp_path: Path) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")

    result = _upload(service, plan, skip_resources=True)

    assert result.outcome is ExamOutcome.NO_RESOURCES
    assert _ResourceSpy.calls == []


def test_nothing_attachable_returns_no_resources(tmp_path: Path) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam", resource_dir=False, misc=False), "MISC")

    result = _upload(service, plan)

    assert result.outcome is ExamOutcome.NO_RESOURCES
    assert result.to_json_dict()["resources"] == {
        "skipped": False,
        "resource_dirs": 0,
        "misc_files": 0,
        "misc_label": "MISC",
    }


def test_unarchived_session_degrades_to_a_pending_result(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: None)

    result = _upload(service, plan, wait=0)

    assert result.outcome is ExamOutcome.NOT_ARCHIVED
    assert result.pending == 2
    assert result.rerun == (
        f"xnatctl session upload-exam '{plan.exam_root}' -P P -S S -E E --attach-only"
    )
    assert _ResourceSpy.calls == []


def test_wait_loop_sleeps_and_rechecks_until_archived(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")

    resolutions = iter([None, None, "EXPT_1"])
    resolve_calls: list[str] = []

    def fake_resolve(project: str, session: str) -> str | None:
        resolve_calls.append(session)
        return next(resolutions)

    monkeypatch.setattr(service, "_resolve_experiment_id", fake_resolve)

    ticks = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr("xnatctl.services.exam_upload.time.monotonic", lambda: next(ticks))
    sleeps: list[float] = []
    monkeypatch.setattr("xnatctl.services.exam_upload.time.sleep", sleeps.append)

    result = _upload(service, plan, wait=30, wait_interval=5)

    assert result.outcome is ExamOutcome.COMPLETE
    assert sleeps == [5, 5]
    assert len(resolve_calls) == 3


def test_wait_loop_gives_up_at_the_deadline(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: None)

    ticks = iter([0.0, 9999.0])
    monkeypatch.setattr("xnatctl.services.exam_upload.time.monotonic", lambda: next(ticks))

    result = _upload(service, plan, wait=30, wait_interval=5)

    assert result.outcome is ExamOutcome.NOT_ARCHIVED


def _assert_json_payload(result: ExamUploadResult, expected: dict[str, Any]) -> None:
    assert result.to_json_dict() == expected
    assert json.dumps(result.to_json_dict(), indent=2) == json.dumps(expected, indent=2)


def test_json_shape_complete(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: "EXPT_1")

    result = _upload(service, plan)

    _assert_json_payload(
        result,
        {
            "project": "P",
            "subject": "S",
            "session": "E",
            "exam_root": str(plan.exam_root),
            "dicom": {"skipped": False, "total": 1, "uploaded": 1},
            "resources": {
                "skipped": False,
                "resource_dirs": 1,
                "misc_files": 1,
                "misc_label": "MISC",
            },
        },
    )


def test_json_shape_no_resources_skipped(tmp_path: Path) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")

    result = _upload(service, plan, skip_resources=True)

    _assert_json_payload(
        result,
        {
            "project": "P",
            "subject": "S",
            "session": "E",
            "exam_root": str(plan.exam_root),
            "dicom": {"skipped": False, "total": 1, "uploaded": 1},
            "resources": {
                "skipped": True,
                "resource_dirs": 0,
                "misc_files": 0,
                "misc_label": "MISC",
            },
        },
    )


def test_json_shape_not_archived(tmp_path: Path, monkeypatch) -> None:
    service = _service()
    plan = service.plan(_make_exam(tmp_path / "exam"), "MISC")
    monkeypatch.setattr(service, "_resolve_experiment_id", lambda project, session: None)

    result = _upload(service, plan, wait=0)

    _assert_json_payload(
        result,
        {
            "project": "P",
            "subject": "S",
            "session": "E",
            "exam_root": str(plan.exam_root),
            "dicom": {"skipped": False, "total": 1, "uploaded": 1},
            "resources": {
                "skipped": False,
                "attached": False,
                "pending": 2,
                "resource_dirs": 0,
                "misc_files": 0,
                "misc_label": "MISC",
                "reason": "session not archived before wait timeout",
                "rerun": (
                    f"xnatctl session upload-exam '{plan.exam_root}' -P P -S S -E E --attach-only"
                ),
            },
        },
    )
