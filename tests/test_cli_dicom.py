"""Tests for the shipped ``dicom validate/inspect/list-tags/anonymize`` commands.

These were effectively untested. The pydicom-gated suite never ran in CI --
every test in it reported "could not import pydicom" and the run stayed green --
so the whole module sat at 16% while looking covered.

Every test here needs pydicom, which the ``dicom`` extra provides; without it
they skip, and that skip is what the min-deps CI leg exercises.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli

pytest.importorskip("pydicom")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_dicom(
    path: Path,
    *,
    patient_id: str | None = "PAT001",
    patient_name: str | None = "Test^Patient",
    modality: str | None = "MR",
    private_tag: bool = False,
) -> None:
    """Write a minimal valid DICOM file.

    Passing None for a field omits it, which is how the missing-required-tag
    cases are built.
    """
    import pydicom
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds = pydicom.Dataset()
    if patient_id is not None:
        ds.PatientID = patient_id
    if patient_name is not None:
        ds.PatientName = patient_name
    if modality is not None:
        ds.Modality = modality
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    if private_tag:
        ds.add_new(0x00091001, "LO", "PRIVATE-VALUE")

    ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True)


class TestValidate:
    def test_a_valid_file_reports_no_errors(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "good.dcm"
        _write_dicom(f)

        result = runner.invoke(cli, ["dicom", "validate", str(f)])

        assert result.exit_code == 0
        assert "Valid: 1" in result.output
        assert "Invalid: 0" in result.output

    def test_a_missing_required_tag_is_named(self, runner: CliRunner, tmp_path: Path) -> None:
        """Naming the tag is the point -- "invalid" alone is not actionable."""
        f = tmp_path / "nopatient.dcm"
        _write_dicom(f, patient_id=None)

        result = runner.invoke(cli, ["dicom", "validate", str(f)])

        assert "Invalid: 1" in result.output
        assert "Missing required tag: PatientID" in result.output

    def test_an_empty_required_value_is_distinguished_from_a_missing_tag(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A present-but-blank PatientID is a different fault from an absent one."""
        f = tmp_path / "blank.dcm"
        _write_dicom(f, patient_id="   ")

        result = runner.invoke(cli, ["dicom", "validate", str(f)])

        assert "Empty required tag value: PatientID" in result.output

    def test_a_non_dicom_file_is_reported_not_crashed(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "notdicom.txt"
        f.write_text("hello")

        result = runner.invoke(cli, ["dicom", "validate", str(f)])

        assert "Invalid: 1" in result.output
        assert "Traceback" not in result.output

    def test_a_directory_validates_its_files(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_dicom(tmp_path / "a.dcm")
        _write_dicom(tmp_path / "b.dcm")

        result = runner.invoke(cli, ["dicom", "validate", str(tmp_path)])

        assert "Validated 2 files" in result.output
        assert "Valid: 2" in result.output

    def test_recursive_reaches_subdirectories(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_dicom(tmp_path / "a.dcm")
        _write_dicom(tmp_path / "sub" / "b.dcm")

        flat = runner.invoke(cli, ["dicom", "validate", str(tmp_path)])
        deep = runner.invoke(cli, ["dicom", "validate", str(tmp_path), "-r"])

        assert "Validated 1 files" in flat.output
        assert "Validated 2 files" in deep.output

    def test_json_output_is_machine_readable(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_dicom(tmp_path / "a.dcm")
        _write_dicom(tmp_path / "bad.dcm", patient_id=None)

        result = runner.invoke(cli, ["dicom", "validate", str(tmp_path), "-o", "json"])

        payload = json.loads(result.output)
        assert payload["valid_count"] == 1
        assert payload["invalid_count"] == 1
        assert len(payload["files"]) == 2

    def test_quiet_lists_only_the_invalid_ones(self, runner: CliRunner, tmp_path: Path) -> None:
        _write_dicom(tmp_path / "good.dcm")
        _write_dicom(tmp_path / "bad.dcm", patient_id=None)

        result = runner.invoke(cli, ["dicom", "validate", str(tmp_path), "-q"])

        assert "bad.dcm" in result.output
        assert "good.dcm" not in result.output


class TestInspect:
    def test_tag_values_appear_in_the_output(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, patient_id="PAT042")

        result = runner.invoke(cli, ["dicom", "inspect", str(f)])

        assert result.exit_code == 0
        assert "PAT042" in result.output
        assert "MR" in result.output

    def test_specific_tags_can_be_selected(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, patient_id="PAT042")

        result = runner.invoke(cli, ["dicom", "inspect", str(f), "-t", "PatientID"])

        assert "PAT042" in result.output

    def test_json_output_parses(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, patient_id="PAT042")

        result = runner.invoke(cli, ["dicom", "inspect", str(f), "-o", "json"])

        payload = json.loads(result.output)
        assert payload["PatientID"] == "PAT042"

    def test_private_tags_are_opt_in(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, private_tag=True)

        without = runner.invoke(cli, ["dicom", "inspect", str(f), "-o", "json"])
        with_private = runner.invoke(cli, ["dicom", "inspect", str(f), "--private", "-o", "json"])

        assert "PRIVATE-VALUE" not in without.output
        assert "PRIVATE-VALUE" in with_private.output

    def test_a_non_dicom_file_exits_nonzero_without_a_traceback(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "notdicom.txt"
        f.write_text("hello")

        result = runner.invoke(cli, ["dicom", "inspect", str(f)])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "Not a valid DICOM file" in result.output


class TestListTags:
    def test_tags_are_listed(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, patient_id="PAT042")

        result = runner.invoke(cli, ["dicom", "list-tags", str(f)])

        assert result.exit_code == 0
        assert "PatientID" in result.output

    def test_private_tags_are_excluded_by_default(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f, private_tag=True)

        without = runner.invoke(cli, ["dicom", "list-tags", str(f), "-o", "json"])
        with_private = runner.invoke(cli, ["dicom", "list-tags", str(f), "--private", "-o", "json"])

        assert "PRIVATE-VALUE" not in without.output
        assert "PRIVATE-VALUE" in with_private.output

    def test_json_output_parses(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "a.dcm"
        _write_dicom(f)

        result = runner.invoke(cli, ["dicom", "list-tags", str(f), "-o", "json"])

        assert isinstance(json.loads(result.output), (list, dict))

    def test_a_non_dicom_file_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        f = tmp_path / "notdicom.txt"
        f.write_text("hello")

        result = runner.invoke(cli, ["dicom", "list-tags", str(f)])

        assert result.exit_code != 0
        assert "Traceback" not in result.output


class TestAnonymize:
    def test_patient_identifiers_are_replaced(self, runner: CliRunner, tmp_path: Path) -> None:
        """The whole point of the command: the original must not survive."""
        import pydicom

        src = tmp_path / "in.dcm"
        dst = tmp_path / "out.dcm"
        _write_dicom(src, patient_id="REAL-MRN-12345", patient_name="Real^Person")

        result = runner.invoke(
            cli,
            [
                "dicom",
                "anonymize",
                str(src),
                str(dst),
                "--patient-id",
                "ANON001",
                "--patient-name",
                "Anon^Subject",
            ],
        )

        assert result.exit_code == 0, result.output
        out = pydicom.dcmread(str(dst))
        assert out.PatientID == "ANON001"
        assert "REAL-MRN-12345" not in str(out.PatientID)
        assert "Real" not in str(out.PatientName)

    def test_dry_run_writes_nothing(self, runner: CliRunner, tmp_path: Path) -> None:
        src = tmp_path / "in.dcm"
        dst = tmp_path / "out.dcm"
        _write_dicom(src, patient_id="REAL-MRN")

        result = runner.invoke(
            cli,
            ["dicom", "anonymize", str(src), str(dst), "--patient-id", "ANON001", "--dry-run"],
        )

        assert result.exit_code == 0
        assert not dst.exists(), "dry-run wrote the output file"

    def test_private_tags_can_be_removed(self, runner: CliRunner, tmp_path: Path) -> None:
        """Private tags are a common route for identifiers to leak through."""
        import pydicom

        src = tmp_path / "in.dcm"
        dst = tmp_path / "out.dcm"
        _write_dicom(src, private_tag=True)

        result = runner.invoke(cli, ["dicom", "anonymize", str(src), str(dst), "--remove-private"])

        assert result.exit_code == 0, result.output
        out = pydicom.dcmread(str(dst))
        assert not any(elem.tag.is_private for elem in out)

    def test_a_directory_is_anonymized(self, runner: CliRunner, tmp_path: Path) -> None:
        src = tmp_path / "in"
        dst = tmp_path / "out"
        _write_dicom(src / "a.dcm", patient_id="REAL1")
        _write_dicom(src / "b.dcm", patient_id="REAL2")

        result = runner.invoke(
            cli, ["dicom", "anonymize", str(src), str(dst), "--patient-id", "ANON"]
        )

        assert result.exit_code == 0, result.output
        assert len(list(dst.rglob("*.dcm"))) == 2


class TestWithoutPydicom:
    """Every command must say how to fix a missing extra, not traceback."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["dicom", "validate", "."],
            ["dicom", "inspect", "."],
            ["dicom", "list-tags", "."],
        ],
    )
    def test_a_missing_extra_gives_an_actionable_error(
        self, runner: CliRunner, argv: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("xnatctl.cli.dicom_cmd.check_pydicom", lambda: False)

        result = runner.invoke(cli, argv)

        assert result.exit_code != 0
        assert "xnatctl[dicom]" in result.output
        assert "Traceback" not in result.output
