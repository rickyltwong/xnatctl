"""Tests for session upload-exam CLI behavior."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.progress import UploadSummary


class FakeClient:
    """Minimal authenticated client stub."""

    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}

    def get_json(self, path: str, *, params: dict[str, object] | None = None) -> object:
        raise NotImplementedError(f"FakeClient.get_json not implemented for: {path}")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()

    def fake_get_client(self) -> FakeClient:
        return client

    monkeypatch.setattr("xnatctl.cli.common.Context.get_client", fake_get_client)
    return client


def test_session_upload_exam_dry_run_prints_physio_and_misc(
    runner: CliRunner,
    auth_client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with runner.isolated_filesystem():
        monkeypatch.setenv("HOME", str(Path.cwd()))
        exam_root = Path("exam")
        dicom_dir = exam_root / "DICOM" / "scan"
        dicom_dir.mkdir(parents=True)
        (dicom_dir / "a.dcm").write_text("payload")

        physio_dir = exam_root / "Physio"
        physio_dir.mkdir(parents=True)
        (physio_dir / "trace.txt").write_text("payload")
        (exam_root / "notes.txt").write_text("payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload-exam",
                str(exam_root),
                "-P",
                "P",
                "-S",
                "S",
                "-E",
                "E",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Physio" in result.output
        assert "MISC" in result.output


def test_session_upload_exam_only_dicoms_does_not_resolve_experiment_id(
    runner: CliRunner,
    auth_client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UploadServiceSpy:
        """Spy UploadService to avoid network and force success."""

        last_instance: UploadServiceSpy | None = None
        last_files_len: int | None = None

        def __init__(self, client: FakeClient) -> None:
            self.client = client
            UploadServiceSpy.last_instance = self

        def upload_dicom_gradual_files(
            self,
            *,
            files: tuple[Path, ...],
            project: str,
            subject: str,
            session: str,
            workers: int,
        ) -> UploadSummary:
            UploadServiceSpy.last_files_len = len(files)
            return UploadSummary(
                success=True,
                total=len(files),
                succeeded=len(files),
                failed=0,
                duration=0.0,
                errors=[],
            )

    monkeypatch.setattr("xnatctl.services.uploads.UploadService", UploadServiceSpy)

    with runner.isolated_filesystem():
        monkeypatch.setenv("HOME", str(Path.cwd()))
        exam_root = Path("exam")
        dicom_dir = exam_root / "DICOM" / "scan"
        dicom_dir.mkdir(parents=True)
        (dicom_dir / "a.dcm").write_text("payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload-exam",
                str(exam_root),
                "-P",
                "P",
                "-S",
                "S",
                "-E",
                "E",
            ],
        )

        assert result.exit_code == 0, result.output
        assert UploadServiceSpy.last_instance is not None
        assert UploadServiceSpy.last_files_len == 1


def test_session_upload_exam_wait_for_archive_default_succeeds(
    runner: CliRunner,
    auth_client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UploadServiceStub:
        """Stub UploadService to avoid network and force success."""

        def __init__(self, client: FakeClient) -> None:
            self.client = client

        def upload_dicom_gradual_files(
            self,
            *,
            files: tuple[Path, ...],
            project: str,
            subject: str,
            session: str,
            workers: int,
        ) -> UploadSummary:
            return UploadSummary(
                success=True,
                total=len(files),
                succeeded=len(files),
                failed=0,
                duration=0.0,
                errors=[],
            )

    class ResourceServiceSpy:
        """Spy ResourceService to record resource attachment calls."""

        calls: list[tuple[str, dict[str, object]]] = []

        def __init__(self, client: FakeClient) -> None:
            self.client = client

        def create(self, *, session_id: str, resource_label: str, project: str) -> None:
            ResourceServiceSpy.calls.append(
                (
                    "create",
                    {
                        "session_id": session_id,
                        "resource_label": resource_label,
                        "project": project,
                    },
                )
            )

        def upload_directory(
            self,
            *,
            session_id: str,
            resource_label: str,
            directory_path: Path,
            project: str,
        ) -> None:
            ResourceServiceSpy.calls.append(
                (
                    "upload_directory",
                    {
                        "session_id": session_id,
                        "resource_label": resource_label,
                        "directory_name": directory_path.name,
                        "project": project,
                    },
                )
            )

        def upload_file(
            self,
            *,
            session_id: str,
            resource_label: str,
            file_path: Path,
            project: str,
        ) -> None:
            ResourceServiceSpy.calls.append(
                (
                    "upload_file",
                    {
                        "session_id": session_id,
                        "resource_label": resource_label,
                        "file_path": file_path,
                        "project": project,
                    },
                )
            )

    monkeypatch.setattr("xnatctl.services.uploads.UploadService", UploadServiceStub)
    monkeypatch.setattr("xnatctl.services.resources.ResourceService", ResourceServiceSpy)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    resolve_calls = {"count": 0}
    resolved_experiment_id = "EXPT_123"

    def fake_get_json(path: str, *, params: dict[str, object] | None = None) -> object:
        if path != "/data/projects/P/experiments/E":
            raise AssertionError(f"Unexpected get_json path: {path}")

        resolve_calls["count"] += 1
        if resolve_calls["count"] == 1:
            raise ResourceNotFoundError("resource", path)

        return {"ResultSet": {"Result": [{"ID": resolved_experiment_id}]}}

    monkeypatch.setattr(auth_client, "get_json", fake_get_json)

    with runner.isolated_filesystem():
        monkeypatch.setenv("HOME", str(Path.cwd()))
        exam_root = Path("exam")
        dicom_dir = exam_root / "DICOM" / "scan"
        dicom_dir.mkdir(parents=True)
        (dicom_dir / "a.dcm").write_text("payload")

        physio_dir = exam_root / "Physio"
        physio_dir.mkdir(parents=True)
        (physio_dir / "trace.txt").write_text("payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload-exam",
                str(exam_root),
                "-P",
                "P",
                "-S",
                "S",
                "-E",
                "E",
            ],
        )

        assert result.exit_code == 0, result.output
        assert resolve_calls["count"] == 2
        assert ResourceServiceSpy.calls == [
            (
                "create",
                {
                    "session_id": resolved_experiment_id,
                    "resource_label": "Physio",
                    "project": "P",
                },
            ),
            (
                "upload_directory",
                {
                    "session_id": resolved_experiment_id,
                    "resource_label": "Physio",
                    "directory_name": "Physio",
                    "project": "P",
                },
            ),
        ]


def test_session_upload_exam_wait_for_archive_timeout_click_error(
    runner: CliRunner,
    auth_client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UploadServiceStub:
        """Stub UploadService to avoid network and force success."""

        def __init__(self, client: FakeClient) -> None:
            self.client = client

        def upload_dicom_gradual_files(
            self,
            *,
            files: tuple[Path, ...],
            project: str,
            subject: str,
            session: str,
            workers: int,
        ) -> UploadSummary:
            return UploadSummary(
                success=True,
                total=len(files),
                succeeded=len(files),
                failed=0,
                duration=0.0,
                errors=[],
            )

    monkeypatch.setattr("xnatctl.services.uploads.UploadService", UploadServiceStub)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    monotonic_calls = {"i": 0}

    def fake_monotonic() -> float:
        monotonic_calls["i"] += 1
        return 0.0 if monotonic_calls["i"] == 1 else 9999.0

    monkeypatch.setattr("xnatctl.cli.session.time.monotonic", fake_monotonic)

    def fake_get_json(path: str, *, params: dict[str, object] | None = None) -> object:
        if path != "/data/projects/P/experiments/E":
            raise AssertionError(f"Unexpected get_json path: {path}")
        raise ResourceNotFoundError("resource", path)

    monkeypatch.setattr(auth_client, "get_json", fake_get_json)

    with runner.isolated_filesystem():
        monkeypatch.setenv("HOME", str(Path.cwd()))
        exam_root = Path("exam")
        dicom_dir = exam_root / "DICOM" / "scan"
        dicom_dir.mkdir(parents=True)
        (dicom_dir / "a.dcm").write_text("payload")

        physio_dir = exam_root / "Physio"
        physio_dir.mkdir(parents=True)
        (physio_dir / "trace.txt").write_text("payload")

        result = runner.invoke(
            cli,
            [
                "session",
                "upload-exam",
                str(exam_root),
                "-P",
                "P",
                "-S",
                "S",
                "-E",
                "E",
                "--wait-timeout",
                "1",
                "--wait-interval",
                "1",
            ],
        )

        assert "No such option" not in result.output
        assert result.exit_code == 1
        assert "ResourceNotFoundError" not in result.output
        assert "Timed out waiting for archived experiment ID for session 'E'" in result.output
