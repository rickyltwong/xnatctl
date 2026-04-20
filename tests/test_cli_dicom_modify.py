"""Tests for xnatctl dicom modify command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _write_fake_dicom(path: Path, patient_id: str = "ORIG") -> None:
    """Create a minimal valid DICOM file using pydicom.

    Args:
        path: Target file path.
        patient_id: PatientID value to set.
    """
    pydicom = pytest.importorskip("pydicom")
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds = pydicom.Dataset()
    ds.PatientID = patient_id
    ds.PatientName = "Test^Patient"
    ds.Modality = "MR"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"  # MR Image Storage

    ds.is_little_endian = True
    ds.is_implicit_VR = False

    file_meta = pydicom.Dataset()
    file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = file_meta
    ds.preamble = b"\x00" * 128

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path))


# =========================================================================
# Argument validation
# =========================================================================


class TestModifyValidation:
    """Tests for tag pair parsing and keyword validation."""

    def test_missing_tag_option(self, runner: CliRunner, tmp_path: Path) -> None:
        """--tag is required."""
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)
        result = runner.invoke(cli, ["dicom", "modify", str(dcm)])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_invalid_keyword_value_format(self, runner: CliRunner, tmp_path: Path) -> None:
        """Tag without '=' is rejected."""
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)
        result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "PatientID"])
        assert result.exit_code != 0
        assert "KEYWORD=VALUE" in result.output

    def test_unknown_keyword_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        """Unrecognised DICOM keyword is rejected."""
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)
        result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "FakeTag=abc"])
        assert result.exit_code != 0
        assert "Unknown DICOM keyword" in result.output

    def test_empty_keyword_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        """Empty keyword (=VALUE) is rejected."""
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)
        result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "=foo"])
        assert result.exit_code != 0
        assert "Empty keyword" in result.output


# =========================================================================
# Single-file modification
# =========================================================================


class TestModifySingleFile:
    """Tests for modifying a single DICOM file."""

    def test_modify_single_tag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Modify one tag on a single file."""
        pydicom = pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm, patient_id="ORIG")

        result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "PatientID=NEW001"])
        assert result.exit_code == 0
        assert "Modified 1" in result.output

        ds = pydicom.dcmread(str(dcm), stop_before_pixels=True)
        assert ds.PatientID == "NEW001"

    def test_modify_multiple_tags(self, runner: CliRunner, tmp_path: Path) -> None:
        """Modify multiple tags at once."""
        pydicom = pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        result = runner.invoke(
            cli,
            [
                "dicom",
                "modify",
                str(dcm),
                "-t",
                "PatientID=ANON",
                "-t",
                "StudyDescription=Demo",
            ],
        )
        assert result.exit_code == 0

        ds = pydicom.dcmread(str(dcm), stop_before_pixels=True)
        assert ds.PatientID == "ANON"
        assert ds.StudyDescription == "Demo"

    def test_value_with_equals_sign(self, runner: CliRunner, tmp_path: Path) -> None:
        """Values containing '=' are preserved."""
        pydicom = pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "StudyDescription=a=b=c"])
        assert result.exit_code == 0

        ds = pydicom.dcmread(str(dcm), stop_before_pixels=True)
        assert ds.StudyDescription == "a=b=c"


# =========================================================================
# Dry-run
# =========================================================================


class TestModifyDryRun:
    """Tests for --dry-run behaviour."""

    def test_dry_run_no_write(self, runner: CliRunner, tmp_path: Path) -> None:
        """Dry run reports changes but leaves file untouched."""
        pydicom = pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm, patient_id="ORIG")

        result = runner.invoke(
            cli, ["dicom", "modify", str(dcm), "-t", "PatientID=NEW", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Would modify 1" in result.output

        ds = pydicom.dcmread(str(dcm), stop_before_pixels=True)
        assert ds.PatientID == "ORIG"


# =========================================================================
# Backup
# =========================================================================


class TestModifyBackup:
    """Tests for --backup flag."""

    def test_backup_created(self, runner: CliRunner, tmp_path: Path) -> None:
        """A .bak file is created when --backup is used."""
        pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm, patient_id="ORIG")

        result = runner.invoke(
            cli, ["dicom", "modify", str(dcm), "-t", "PatientID=NEW", "--backup"]
        )
        assert result.exit_code == 0
        assert (tmp_path / "test.dcm.bak").exists()

    def test_no_backup_without_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """No .bak created without --backup."""
        pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "PatientID=NEW"])
        assert not (tmp_path / "test.dcm.bak").exists()


# =========================================================================
# Directory / recursive
# =========================================================================


class TestModifyDirectory:
    """Tests for directory and recursive processing."""

    def test_directory_non_recursive(self, runner: CliRunner, tmp_path: Path) -> None:
        """Modify files in a flat directory."""
        pydicom = pytest.importorskip("pydicom")
        for i in range(3):
            _write_fake_dicom(tmp_path / f"file{i}.dcm")

        result = runner.invoke(cli, ["dicom", "modify", str(tmp_path), "-t", "PatientID=BATCH"])
        assert result.exit_code == 0
        assert "Modified 3" in result.output

        for i in range(3):
            ds = pydicom.dcmread(str(tmp_path / f"file{i}.dcm"), stop_before_pixels=True)
            assert ds.PatientID == "BATCH"

    def test_recursive_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Recurse into subdirectories with -r."""
        pydicom = pytest.importorskip("pydicom")
        _write_fake_dicom(tmp_path / "a.dcm")
        _write_fake_dicom(tmp_path / "sub" / "b.dcm")

        result = runner.invoke(cli, ["dicom", "modify", str(tmp_path), "-r", "-t", "PatientID=REC"])
        assert result.exit_code == 0
        assert "Modified 2" in result.output

        for p in [tmp_path / "a.dcm", tmp_path / "sub" / "b.dcm"]:
            ds = pydicom.dcmread(str(p), stop_before_pixels=True)
            assert ds.PatientID == "REC"

    def test_non_dicom_skipped(self, runner: CliRunner, tmp_path: Path) -> None:
        """Non-DICOM files are skipped, not errored."""
        pytest.importorskip("pydicom")
        _write_fake_dicom(tmp_path / "good.dcm")
        (tmp_path / "readme.txt").write_text("not dicom")

        result = runner.invoke(cli, ["dicom", "modify", str(tmp_path), "-t", "PatientID=X"])
        assert result.exit_code == 0
        assert "skipped 1" in result.output.lower()


# =========================================================================
# JSON output
# =========================================================================


class TestModifyJsonOutput:
    """Tests for --output json mode."""

    def test_json_output(self, runner: CliRunner, tmp_path: Path) -> None:
        """JSON output contains expected structure."""
        pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        result = runner.invoke(
            cli, ["dicom", "modify", str(dcm), "-t", "PatientID=J", "-o", "json"]
        )
        assert result.exit_code == 0

        import json

        data = json.loads(result.output)
        assert data["modified"] == 1
        assert data["skipped"] == 0
        assert data["failed"] == 0
        assert len(data["files"]) == 1
        assert data["files"][0]["status"] == "modified"


# =========================================================================
# No pydicom fallback
# =========================================================================


class TestModifyFailedFiles:
    """Tests for write-error handling (failed counter and exit code)."""

    def test_write_error_reports_failed(self, runner: CliRunner, tmp_path: Path) -> None:
        """A write error during save is reported as 'failed' with non-zero exit."""
        pydicom_mod = pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        with patch("pydicom.FileDataset.save_as", side_effect=OSError("disk full")):
            result = runner.invoke(
                cli, ["dicom", "modify", str(dcm), "-t", "PatientID=X"]
            )
        assert result.exit_code == 1
        assert "failed 1" in result.output.lower()

    def test_write_error_json(self, runner: CliRunner, tmp_path: Path) -> None:
        """JSON output includes failed count on write error."""
        pytest.importorskip("pydicom")
        dcm = tmp_path / "test.dcm"
        _write_fake_dicom(dcm)

        with patch("pydicom.FileDataset.save_as", side_effect=OSError("disk full")):
            result = runner.invoke(
                cli,
                ["dicom", "modify", str(dcm), "-t", "PatientID=X", "-o", "json"],
            )
        assert result.exit_code == 1

        import json

        data = json.loads(result.output)
        assert data["failed"] == 1
        assert data["files"][0]["status"] == "failed"


# =========================================================================
# No pydicom fallback
# =========================================================================


class TestModifyNoPydicom:
    """Tests for behaviour when pydicom is not installed."""

    def test_error_without_pydicom(self, runner: CliRunner, tmp_path: Path) -> None:
        """Command exits with error when pydicom is missing."""
        dcm = tmp_path / "dummy.dcm"
        dcm.write_bytes(b"\x00" * 256)

        with patch("xnatctl.cli.dicom_cmd.check_pydicom", return_value=False):
            result = runner.invoke(cli, ["dicom", "modify", str(dcm), "-t", "PatientID=X"])
        assert result.exit_code == 1
        assert "pydicom not installed" in result.output
