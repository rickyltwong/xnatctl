"""Tests for session upload-exam CLI behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli


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
) -> None:
    with runner.isolated_filesystem():
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
