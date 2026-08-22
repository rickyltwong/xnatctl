"""`-o json` structured `TransferSummary` output for the transfer verbs.

Covers `session download`, `session upload` (single archive, directory batch,
gradual-DICOM), `session upload-dicom`, `scan download`, `resource upload`,
and `resource download`. `session upload-exam` has its own pinned JSON
contract (see test_exam_upload.py / test_exam_upload_service.py) and is
deliberately left untouched here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from conftest import authenticated_seams, make_authenticated_context
from test_session_download_failures import _download, _serve

from xnatctl.cli.main import cli
from xnatctl.models.progress import DownloadSummary, TransferSummary, UploadSummary, transfer_status
from xnatctl.services.downloads import DownloadOutcome, ScanResult

runner = CliRunner()


def _resolved_experiment(client: Any) -> None:
    """Mock the experiment-resolution GET `session download`/`scan download` issue first."""
    client.get_json.return_value = {
        "ResultSet": {
            "Result": [
                {
                    "ID": "XNAT_E00001",
                    "label": "SESS001",
                    "project": "TESTPROJ",
                    "subject_ID": "XNAT_S00001",
                }
            ]
        }
    }


class TestSessionDownloadJson:
    def test_dry_run_emits_a_plan(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["operation"] == "download"
        assert payload["session_id"] == "XNAT_E00001"

    def test_partial_failure_emits_a_summary_and_exits_nonzero(self, tmp_path: Path) -> None:
        """One scan failing must still print a structured summary, not just text."""
        for url in _serve(failing_scan="2"):
            result = _download(url, tmp_path, "-o", "json")

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "partial"
        assert summary.operation == "download"
        failed_items = [item for item in summary.items if item.status == "failed"]
        assert [item.id for item in failed_items] == ["2"]
        assert failed_items[0].error

    def test_clean_download_emits_success_status(self, tmp_path: Path) -> None:
        for url in _serve(failing_scan=None):
            result = _download(url, tmp_path, "-o", "json")

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert summary.scans == 3
        assert summary.files == 3
        assert all(item.status == "success" for item in summary.items)


class TestSessionUploadJson:
    def test_dry_run_emits_a_plan(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        archive = tmp_path / "archive.zip"
        archive.write_bytes(b"fake zip")

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(archive),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "--dry-run",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["operation"] == "upload"
        assert payload["session_id"] == "SESS"

    def test_single_archive_upload_emits_success_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        archive = tmp_path / "archive.zip"
        archive.write_bytes(b"fake zip content")

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.upload_archive_or_raise", return_value=None),
        ):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(archive),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert summary.files == 1
        assert summary.bytes == archive.stat().st_size

    def test_directory_upload_partial_failure(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "dicoms"
        source_dir.mkdir()
        (source_dir / "1.dcm").write_bytes(b"x")

        partial_summary = UploadSummary(
            success=False,
            total=2,
            succeeded=1,
            failed=1,
            duration=1.5,
            errors=["batch 1 failed: HTTP 500"],
            total_files=10,
            total_size_mb=2.0,
            batches_total=2,
            batches_succeeded=1,
            batches_failed=1,
            session_id="SESS",
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_parallel.return_value = partial_summary
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(source_dir),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "-o",
                    "json",
                    "-w",
                    "2",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "partial"
        # `total_files` counts everything attempted across all batches, not
        # only what succeeded -- not trustworthy as "transferred" unless the
        # whole run did, so it's null here rather than the misleading 10.
        assert summary.files is None
        # One aggregate item, not one fabricated per-batch item: `errors`
        # isn't keyed to batch index, so a per-batch breakdown would
        # misattribute which batch failed.
        assert len(summary.items) == 1
        assert summary.items[0].status == "failed"
        assert "batch 1 failed" in (summary.items[0].error or "")


class TestSessionUploadDicomJson:
    def test_dry_run_emits_a_plan(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "dicoms"
        source_dir.mkdir()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload-dicom",
                    str(source_dir),
                    "--host",
                    "xnat.example.org",
                    "--called-aet",
                    "XNAT",
                    "--dry-run",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["host"] == "xnat.example.org"


class TestScanDownloadJson:
    def test_dry_run_emits_a_plan(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-s",
                    "1,2",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["scans"] == ["1", "2"]

    def test_successful_download_emits_a_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.0,
            total_files=5,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-s",
                    "1",
                    "--out",
                    str(tmp_path),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert summary.files == 5


class TestResourceUploadDownloadJson:
    def test_resource_upload_emits_a_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        test_file = tmp_path / "test.nii.gz"
        test_file.write_bytes(b"fake nifti data")

        mock_service = MagicMock()

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.resources.ResourceService", return_value=mock_service),
        ):
            result = runner.invoke(
                cli,
                [
                    "resource",
                    "upload",
                    "XNAT_E00001",
                    "NIFTI",
                    str(test_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert summary.operation == "upload"
        assert summary.files == 1
        assert summary.bytes == test_file.stat().st_size

    def test_resource_download_emits_a_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        out_file = tmp_path / "out.zip"

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.resource.stream_to_file") as mock_stream,
        ):
            from xnatctl.services.downloads import StreamedFile

            mock_stream.return_value = StreamedFile(bytes_written=42, content_length=42)
            result = runner.invoke(
                cli,
                [
                    "resource",
                    "download",
                    "XNAT_E00001",
                    "NIFTI",
                    "--file",
                    str(out_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert summary.operation == "download"
        assert summary.bytes == 42


class TestTransferStatusAuthoritativeSuccess:
    """`success=False, succeeded=0, failed=0` (nothing attempted) must never
    read as "success" -- zero attempted is not zero failed. This was a live
    regression: gradual-DICOM and directory-parallel both return exactly
    this shape on a zero-file source, and the old count-only heuristic
    (`failed == 0 -> success`) reported success while exiting 1.
    """

    def test_authoritative_failure_with_nothing_attempted_is_failed(self) -> None:
        assert transfer_status(succeeded=0, failed=0, success=False) == "failed"

    def test_authoritative_success_with_nothing_attempted_is_success(self) -> None:
        assert transfer_status(succeeded=0, failed=0, success=True) == "success"

    def test_authoritative_failure_with_a_success_is_partial(self) -> None:
        assert transfer_status(succeeded=1, failed=0, success=False) == "partial"


class TestZeroFileUploadRegressions:
    """Live reproductions of the same regression through the CLI paths that
    actually hit it: an empty/unusable source directory returns
    `UploadSummary(success=False, succeeded=0, failed=0)`.
    """

    def test_gradual_zero_dicom_files_is_failed_not_success(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "empty"
        source_dir.mkdir()
        zero_summary = UploadSummary(
            success=False,
            total=0,
            succeeded=0,
            failed=0,
            duration=0.1,
            errors=["No DICOM files found"],
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_gradual.return_value = zero_summary
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(source_dir),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "--mode",
                    "gradual",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"

    def test_directory_parallel_zero_dicom_files_is_failed_not_success(
        self, tmp_path: Path
    ) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "empty"
        source_dir.mkdir()
        zero_summary = UploadSummary(
            success=False,
            total=0,
            succeeded=0,
            failed=0,
            duration=0.1,
            errors=["No DICOM files found"],
            batches_total=0,
            batches_succeeded=0,
            batches_failed=0,
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_parallel.return_value = zero_summary
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(source_dir),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"


class TestExceptionPathsStillEmitJson:
    """A runtime exception (not a returned failure summary) must still print
    exactly one JSON object to stdout in `-o json` mode before the command
    exits nonzero -- previously these paths went straight to
    @handle_errors and printed nothing on stdout at all.
    """

    def test_resource_upload_exception_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        test_file = tmp_path / "test.nii.gz"
        test_file.write_bytes(b"fake nifti data")

        mock_service = MagicMock()
        mock_service.upload_file.side_effect = RuntimeError("upstream exploded")

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.resources.ResourceService", return_value=mock_service),
        ):
            result = runner.invoke(
                cli,
                [
                    "resource",
                    "upload",
                    "XNAT_E00001",
                    "NIFTI",
                    str(test_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert summary.items[0].status == "failed"
        assert "upstream exploded" in (summary.items[0].error or "")

    def test_single_archive_upload_exception_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        archive = tmp_path / "archive.zip"
        archive.write_bytes(b"fake zip content")

        with (
            authenticated_seams(ctx, mock_client),
            patch(
                "xnatctl.services.upload.upload_archive_or_raise",
                side_effect=RuntimeError("import service rejected the archive"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(archive),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "import service rejected the archive" in summary.items[0].error


class TestMultiResourceSessionDownloadDedup:
    """Under -r/--exclude-resource, `on_scan_result` fires once per
    (scan, resource) pair -- the JSON summary must still report one item and
    one count per *scan*, not one per callback firing.
    """

    def test_two_resources_on_one_scan_collapse_to_one_item(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            # Same scan, two resource-tier callbacks -- the include-filter
            # tier's actual firing pattern.
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            on_scan_result(ScanResult(scan_id="1", ok=True, files=1, message="NIFTI"))
            return DownloadOutcome(succeeded=2, failed=[], files=3)

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "-r",
                    "DICOM",
                    "-r",
                    "NIFTI",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.scans == 1
        assert len(summary.items) == 1
        assert summary.items[0].id == "1"
        assert summary.items[0].status == "success"

    def test_one_failing_resource_fails_the_whole_scan_item(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            on_scan_result(ScanResult(scan_id="1", ok=False, files=0, message="NIFTI: 500"))
            return DownloadOutcome(succeeded=1, failed=[("1", "NIFTI: 500")], files=2)

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "-r",
                    "DICOM",
                    "-r",
                    "NIFTI",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.scans == 1
        assert len(summary.items) == 1
        assert summary.items[0].status == "failed"
        assert "NIFTI: 500" in summary.items[0].error
        # The whole scan is one failed item, so the overall verdict must be
        # "failed", not "partial" -- status has to come from the same
        # deduplicated per-scan bookkeeping as `items`, not from the
        # service's raw per-resource-task succeeded/failed counts (which
        # would read this as 1 succeeded task, 1 failed task -> "partial").
        assert summary.status == "failed"


class TestSecretRedactionInJson:
    """An exception message that echoes a request URL with a secret-shaped
    query value must never reach stdout unredacted -- `TransferItemResult`
    redacts `error` at construction, so no call site can forget.
    """

    def test_a_secret_query_value_in_an_exception_message_is_redacted(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        test_file = tmp_path / "test.nii.gz"
        test_file.write_bytes(b"fake nifti data")

        mock_service = MagicMock()
        mock_service.upload_file.side_effect = RuntimeError(
            "PUT https://xnat.example.org/data/upload?token=SECRET123 failed with 500"
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.resources.ResourceService", return_value=mock_service),
        ):
            result = runner.invoke(
                cli,
                [
                    "resource",
                    "upload",
                    "XNAT_E00001",
                    "NIFTI",
                    str(test_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        assert "SECRET123" not in result.stdout
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "token=***" in (summary.items[0].error or "")


class TestScanDownloadVerificationExceptionEmitsJson:
    """`--verify` blowing up must still produce a JSON summary, not just an
    exit code with nothing on stdout.
    """

    def test_verify_exception_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            total_files=5,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.side_effect = RuntimeError(
                "manifest fetch failed"
            )
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-s",
                    "1",
                    "--out",
                    str(tmp_path),
                    "--verify",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "manifest fetch failed" in (summary.items[0].error or "")


class TestUploadDicomPreflightRejectionEmitsJson:
    """A pre-flight rejection (before the upload_dicom_store try/except)
    must still emit JSON -- these guards run before any wrapper existed.
    """

    def test_archive_file_instead_of_directory_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        archive = tmp_path / "archive.zip"
        archive.write_bytes(b"not a directory")

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload-dicom",
                    str(archive),
                    "--host",
                    "xnat.example.org",
                    "--called-aet",
                    "XNAT",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "directory" in (summary.items[0].error or "").lower()


class TestSessionResourceFailureVisibleWithoutVerify:
    """A session resource that fails to download must show up as a failed
    item and flip the overall status -- previously this was swallowed to a
    stderr warning with no bookkeeping at all unless --verify was also on.
    """

    def test_a_failed_session_resource_flips_status_without_verify(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            return DownloadOutcome(succeeded=1, failed=[], files=2)

        mock_session_service = MagicMock()
        mock_session_service.experiment_resource_rows.return_value = [{"label": "QC"}]

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
            patch("xnatctl.cli.session.SessionService", return_value=mock_session_service),
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            mock_dl_cls.return_value.download_session_level_resources.side_effect = RuntimeError(
                "resource download failed"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--session-resources",
                    "-w",
                    "2",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status != "success"
        failed_ids = [item.id for item in summary.items if item.status == "failed"]
        assert "resource:QC" in failed_ids
        succeeded_ids = [item.id for item in summary.items if item.status == "success"]
        assert "1" in succeeded_ids  # the scan itself still succeeded


class TestSessionResourceInventoryFailureVisible:
    """A session-resources request whose up-front *listing* call fails --
    not just its download -- must still produce a failed item. Previously
    `requested_resource_labels` stayed empty, the per-label loop had
    nothing to iterate, and the whole session-resources request vanished
    from `items`: status "success", exit 0, despite nothing being fetched.
    """

    def test_persistent_listing_failure_flips_status_and_exit_code(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            return DownloadOutcome(succeeded=1, failed=[], files=2)

        mock_session_service = MagicMock()
        mock_session_service.experiment_resource_rows.side_effect = RuntimeError(
            "resource listing unavailable"
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
            patch("xnatctl.cli.session.SessionService", return_value=mock_session_service),
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            # The download's own listing (inside download_session_level_resources)
            # fails too -- the scenario the finding names explicitly.
            mock_dl_cls.return_value.download_session_level_resources.side_effect = RuntimeError(
                "resource listing unavailable"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--session-resources",
                    "-w",
                    "2",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status != "success"
        failed = {item.id: item for item in summary.items if item.status == "failed"}
        assert "resources" in failed
        assert "resource listing unavailable" in (failed["resources"].error or "")

    def test_listing_failed_but_one_resource_landed_before_the_other_failed(
        self, tmp_path: Path
    ) -> None:
        """Listing failed AND the download raised partway through -- but
        resource A landed before resource B blew up. The landed one must
        still get a success item alongside the failure item, so the run
        reads "partial", not a flat "failed" that erases a real success.
        """
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            return DownloadOutcome(succeeded=1, failed=[], files=2)

        def fake_download_session_level_resources(**kwargs: Any) -> list[tuple[str, Path]]:
            downloaded: list[tuple[str, Path]] = kwargs["downloaded"]
            # Resource A lands (appended in place, matching the real
            # service's contract) before resource B's request raises.
            downloaded.append(("A", tmp_path / "resources_A.zip"))
            raise RuntimeError("resource B failed: HTTP 500")

        mock_session_service = MagicMock()
        mock_session_service.experiment_resource_rows.side_effect = RuntimeError(
            "resource listing unavailable"
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
            patch("xnatctl.cli.session.SessionService", return_value=mock_session_service),
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            mock_dl_cls.return_value.download_session_level_resources.side_effect = (
                fake_download_session_level_resources
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--session-resources",
                    "-w",
                    "2",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "partial"
        by_id = {item.id: item for item in summary.items}
        assert by_id["resource:A"].status == "success"
        assert by_id["resources"].status == "failed"
        assert "resource B failed" in (by_id["resources"].error or "")


class TestSequentialPathWithFailedSessionResource:
    """The sequential (workers=1, no -r) session download path has no
    per-scan items, but a failed session resource must not make it look
    like the whole transfer failed -- the archive itself still succeeded.
    """

    def test_archive_success_plus_resource_failure_is_partial_not_failed(
        self, tmp_path: Path
    ) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        mock_session_service = MagicMock()
        mock_session_service.experiment_resource_rows.return_value = [{"label": "QC"}]

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
            patch("xnatctl.cli.session.SessionService", return_value=mock_session_service),
        ):
            mock_dl_cls.return_value.download_session_archive.return_value = tmp_path / "scans.zip"
            mock_dl_cls.return_value.download_session_level_resources.side_effect = RuntimeError(
                "resource download failed"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--session-resources",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.scans is None  # the sequential path never enumerates scans
        assert summary.status == "partial"
        ids_by_status = {item.status: item.id for item in summary.items}
        assert ids_by_status["success"] == "archive"
        assert ids_by_status["failed"] == "resource:QC"


class TestGradualPreflightHumanModeUnchanged:
    """The two documented preflight raises (ValueError, FileNotFoundError)
    must still print the original plain one-line error in table mode, not
    @handle_errors' "Unexpected error: ..." wrapper -- only the *set* of
    exceptions that get JSON output was meant to widen, not the text-mode
    message for these two.
    """

    def test_file_not_found_prints_the_original_message_and_exits_1(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_gradual.side_effect = FileNotFoundError(
                "Source not found: /no/such/path"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(tmp_path),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "--mode",
                    "gradual",
                ],
            )

        assert result.exit_code == 1
        assert "Error: Source not found: /no/such/path" in result.stderr
        assert "Unexpected error" not in result.stderr

    def test_file_not_found_still_emits_json_when_requested(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_gradual.side_effect = FileNotFoundError(
                "Source not found: /no/such/path"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(tmp_path),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "--mode",
                    "gradual",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "Source not found" in (summary.items[0].error or "")

    def test_an_undocumented_exception_still_goes_through_handle_errors(
        self, tmp_path: Path
    ) -> None:
        """Something other than the two documented raises (a corrupt ZIP,
        say) is NOT special-cased -- it re-raises to @handle_errors, which
        classifies it as an unexpected error, same as any other CLI path.
        """
        ctx, mock_client = make_authenticated_context()

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.upload.UploadService") as mock_upload_cls,
        ):
            mock_upload_cls.return_value.upload_dicom_gradual.side_effect = RuntimeError(
                "corrupt archive"
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload",
                    str(tmp_path),
                    "-P",
                    "TESTPROJ",
                    "-S",
                    "SUBJ",
                    "-E",
                    "SESS",
                    "--mode",
                    "gradual",
                ],
            )

        assert result.exit_code == 1
        assert "Unexpected error" in result.stderr
        assert "corrupt archive" in result.stderr


class TestSessionResourceListingHiccupDoesNotFalselyFail:
    """A transient failure in the CLI's own up-front resource-listing call
    must not read as a total failure when the download itself succeeded --
    its own internal listing worked fine and everything landed.
    """

    def test_listing_fails_but_download_succeeds_is_still_success(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        _resolved_experiment(mock_client)

        def fake_download_session_fast(**kwargs: Any) -> DownloadOutcome:
            on_start = kwargs["on_start"]
            on_scan_result = kwargs["on_scan_result"]
            on_start(1)
            on_scan_result(ScanResult(scan_id="1", ok=True, files=2, message="DICOM"))
            return DownloadOutcome(succeeded=1, failed=[], files=2)

        def fake_download_session_level_resources(**kwargs: Any) -> list[tuple[str, Path]]:
            downloaded: list[tuple[str, Path]] = kwargs["downloaded"]
            downloaded.append(("QC", tmp_path / "resources_QC.zip"))
            return downloaded

        mock_session_service = MagicMock()
        mock_session_service.experiment_resource_rows.side_effect = RuntimeError(
            "listing endpoint flaked"
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
            patch("xnatctl.cli.session.SessionService", return_value=mock_session_service),
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = fake_download_session_fast
            # The download's OWN internal listing works fine and delivers a
            # resource -- only our up-front CLI-side listing call flaked.
            mock_dl_cls.return_value.download_session_level_resources.side_effect = (
                fake_download_session_level_resources
            )
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--session-resources",
                    "-w",
                    "2",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "success"
        assert all(item.status == "success" for item in summary.items)
        succeeded_ids = {item.id for item in summary.items}
        assert "resource:QC" in succeeded_ids
        assert "resources" not in succeeded_ids  # no synthetic failure item


class TestUploadDicomTlsPreflightEmitsJson:
    """The two TLS-flag combination checks raise `click.UsageError` directly
    -- they used to bypass the preflight JSON helper entirely, since
    UsageError isn't caught by the try/except around upload_dicom_store.
    """

    def test_tls_key_without_cert_emits_json_before_usage_error(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "dicoms"
        source_dir.mkdir()
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake key")

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload-dicom",
                    str(source_dir),
                    "--host",
                    "xnat.example.org",
                    "--called-aet",
                    "XNAT",
                    "--tls-key",
                    str(key_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "--tls-key requires --tls-cert" in (summary.items[0].error or "")

    def test_tls_cert_without_tls_flag_emits_json_before_usage_error(self, tmp_path: Path) -> None:
        ctx, mock_client = make_authenticated_context()
        source_dir = tmp_path / "dicoms"
        source_dir.mkdir()
        cert_file = tmp_path / "cert.pem"
        cert_file.write_text("fake cert")

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "session",
                    "upload-dicom",
                    str(source_dir),
                    "--host",
                    "xnat.example.org",
                    "--called-aet",
                    "XNAT",
                    "--tls-cert",
                    str(cert_file),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        summary = TransferSummary.model_validate(payload)
        assert summary.status == "failed"
        assert "have no effect without --tls" in (summary.items[0].error or "")
