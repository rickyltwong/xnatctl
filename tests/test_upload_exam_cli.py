"""Tests for session upload-exam CLI behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.models.progress import UploadSummary


class FakeClient:
    """Minimal authenticated client stub."""

    is_authenticated = True

    def whoami(self) -> dict[str, str]:
        return {"username": "tester"}


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
