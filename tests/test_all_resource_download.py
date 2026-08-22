"""Tests for all-resource download support.

Covers:
- session download --resource / --exclude-resource / --session-resources flags
- scan download multiple --resource support
- download_scan service layer default (resource=None)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import authenticated_seams, make_authenticated_context

from xnatctl.cli.main import cli
from xnatctl.models.progress import DownloadSummary
from xnatctl.services.downloads import DownloadOutcome

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


# =============================================================================
# Session download CLI flag tests
# =============================================================================


class TestSessionDownloadResourceFlags:
    """Tests for session download --resource / --exclude-resource flags."""

    def test_dry_run_with_resource_filter(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Dry run with --resource shows resource types."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

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
                    "-r",
                    "DICOM",
                    "-r",
                    "NII",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "DICOM" in result.output
        assert "NII" in result.output

    def test_dry_run_with_exclude_resource(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Dry run with --exclude-resource shows excluded types."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

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
                    "--exclude-resource",
                    "SNAPSHOTS",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "Exclude resources: SNAPSHOTS" in result.output

    def test_dry_run_with_session_resources(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Dry run with --session-resources shows flag status."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

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
                    "--session-resources",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "Session resources: True" in result.output

    def test_resource_and_exclude_resource_mutual_exclusion(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """--resource and --exclude-resource cannot be combined."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

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
                    "-r",
                    "DICOM",
                    "--exclude-resource",
                    "SNAPSHOTS",
                    "--dry-run",
                ],
            )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_include_resources_deprecation_warning(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """--include-resources warns on stderr and maps to --session-resources.

        It used to warn through ``warnings.warn(DeprecationWarning)``, which
        Python hides by default -- so the deprecation was invisible to the
        people who needed to act on it.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

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
                    "--include-resources",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "--include-resources is deprecated" in result.stderr
        assert "will be removed in 0.5.0" in result.stderr
        assert "use --session-resources instead" in result.stderr
        # --include-resources maps to session_resources=True
        assert "Session resources: True" in result.output

    def test_help_shows_new_flags(self, runner: CliRunner) -> None:
        """Help text includes --resource, --exclude-resource, --session-resources."""
        result = runner.invoke(cli, ["session", "download", "--help"])

        assert result.exit_code == 0
        assert "--resource" in result.output
        assert "--exclude-resource" in result.output
        assert "--session-resources" in result.output

    def test_help_hides_include_resources(self, runner: CliRunner) -> None:
        """Help text does not show deprecated --include-resources."""
        result = runner.invoke(cli, ["session", "download", "--help"])

        assert result.exit_code == 0
        assert "--include-resources" not in result.output

    def test_resource_filter_forces_parallel_path(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """--resource with workers=1 still uses parallel path."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

        with (
            authenticated_seams(ctx, mock_client),
            patch(
                "xnatctl.services.downloads.DownloadService.download_session_fast",
                # A bare MagicMock's .failed is truthy, and the command now
                # reads that field to decide its exit code -- a stand-in has to
                # honour the return contract or it fakes a failed download.
                return_value=DownloadOutcome(succeeded=1, failed=[], files=3),
            ) as mock_fast,
        ):
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
                    "-w",
                    "1",
                    "-r",
                    "DICOM",
                ],
            )

        assert result.exit_code == 0
        mock_fast.assert_called_once()
        call_kwargs = mock_fast.call_args[1]
        assert call_kwargs["include_resources"] == ("DICOM",)

    def test_exclude_resource_forces_parallel_path(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """--exclude-resource with workers=1 still uses parallel path."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                ]
            }
        }

        with (
            authenticated_seams(ctx, mock_client),
            patch(
                "xnatctl.services.downloads.DownloadService.download_session_fast",
                # A bare MagicMock's .failed is truthy, and the command now
                # reads that field to decide its exit code -- a stand-in has to
                # honour the return contract or it fakes a failed download.
                return_value=DownloadOutcome(succeeded=1, failed=[], files=3),
            ) as mock_fast,
        ):
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
                    "-w",
                    "1",
                    "--exclude-resource",
                    "SNAPSHOTS",
                ],
            )

        assert result.exit_code == 0
        mock_fast.assert_called_once()
        call_kwargs = mock_fast.call_args[1]
        assert call_kwargs["exclude_resources"] == ("SNAPSHOTS",)


# =============================================================================
# Scan download CLI flag tests
# =============================================================================


class TestScanDownloadMultiResource:
    """Tests for scan download multiple --resource support."""

    def test_multiple_resources_rejected(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Multiple -r flags are rejected with a clear error."""
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
                    "1",
                    "-r",
                    "DICOM",
                    "-r",
                    "NII",
                    "--out",
                    str(tmp_path),
                ],
            )

        assert result.exit_code != 0
        assert "Only one --resource" in result.output

    def test_single_resource_passes_filter(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Single -r passes resource filter to service."""
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            total_files=5,
            total_size_mb=10.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch(
                "xnatctl.services.downloads.DownloadService",
            ) as mock_dl_cls,
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
                    "-r",
                    "DICOM",
                    "--out",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_dl_cls.return_value.download_scans.call_args[1]
        assert call_kwargs["resource"] == "DICOM"

    def test_no_resource_passes_none(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """No -r flag passes resource=None (all resources)."""
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            total_files=5,
            total_size_mb=10.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch(
                "xnatctl.services.downloads.DownloadService",
            ) as mock_dl_cls,
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
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_dl_cls.return_value.download_scans.call_args[1]
        assert call_kwargs["resource"] is None

    def test_multiple_resources_rejected_with_dry_run(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Multiple -r flags are rejected even with --dry-run."""
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
                    "1",
                    "-r",
                    "DICOM",
                    "-r",
                    "NII",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code != 0
        assert "Only one --resource" in result.output


# =============================================================================
# DownloadService.download_scan tests
# =============================================================================


class TestDownloadScanDefault:
    """Tests for download_scan service method with resource=None."""

    def test_resource_none_delegates_to_download_scans(self) -> None:
        """download_scan with resource=None delegates to download_scans."""
        from xnatctl.services.downloads import DownloadService

        mock_client = MagicMock()
        service = DownloadService(mock_client)
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            total_files=5,
            total_size_mb=10.0,
            output_path="/tmp/test",
            session_id="XNAT_E00001",
        )

        with patch.object(service, "download_scans", return_value=mock_summary) as mock_scans:
            result = service.download_scan(
                session_id="XNAT_E00001",
                scan_id="1",
                output_dir=Path("/tmp/test"),
                project="TESTPROJ",
                resource=None,
            )

        mock_scans.assert_called_once_with(
            session_id="XNAT_E00001",
            scan_ids=["1"],
            output_dir=Path("/tmp/test"),
            project="TESTPROJ",
            resource=None,
            progress_callback=None,
        )
        assert result.success is True

    def test_resource_string_delegates_to_download_resource(self) -> None:
        """download_scan with resource string delegates to download_resource."""
        from xnatctl.services.downloads import DownloadService

        mock_client = MagicMock()
        service = DownloadService(mock_client)
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=1.0,
            total_files=5,
            total_size_mb=10.0,
            output_path="/tmp/test",
            session_id="XNAT_E00001",
        )

        with patch.object(
            service,
            "download_resource",
            return_value=mock_summary,
        ) as mock_res:
            result = service.download_scan(
                session_id="XNAT_E00001",
                scan_id="1",
                output_dir=Path("/tmp/test"),
                project="TESTPROJ",
                resource="DICOM",
            )

        mock_res.assert_called_once_with(
            session_id="XNAT_E00001",
            resource_label="DICOM",
            output_dir=Path("/tmp/test"),
            scan_id="1",
            project="TESTPROJ",
            progress_callback=None,
        )
        assert result.success is True
