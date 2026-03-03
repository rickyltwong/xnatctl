"""Tests for all-resource download support.

Covers:
- _extract_scan_zip helper function
- session download --resource / --exclude-resource / --session-resources flags
- scan download multiple --resource support
- download_scan service layer default (resource=None)
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.cli.session import _extract_scan_zip
from xnatctl.core.config import Config, Profile
from xnatctl.models.progress import DownloadSummary


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _make_authenticated_context(
    default_project: str | None = "TESTPROJ",
) -> tuple[Context, MagicMock]:
    """Build a Context with a mocked authenticated client."""
    ctx = Context()
    ctx.config = Config(
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="user",
                password="pass",
                default_project=default_project,
            ),
        },
    )
    mock_client = MagicMock()
    mock_client.is_authenticated = True
    mock_client.base_url = "https://xnat.example.org"
    mock_client.whoami.return_value = {"login": "user"}
    ctx.client = cast(Any, mock_client)
    ctx.auth_manager = MagicMock()
    return ctx, mock_client


# =============================================================================
# _extract_scan_zip tests
# =============================================================================


class TestExtractScanZip:
    """Tests for the _extract_scan_zip helper function."""

    def test_unfiltered_zip_multi_resource(self, tmp_path: Path) -> None:
        """Unfiltered ZIP with multiple resources preserves resource structure."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom data",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/thumb.jpg",
                b"jpeg data",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/brain.nii.gz",
                b"nifti data",
            )

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 3
        assert renamed == 0
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()
        assert (scan_base / "resources" / "SNAPSHOTS" / "files" / "thumb.jpg").exists()
        assert (scan_base / "resources" / "NII" / "files" / "brain.nii.gz").exists()

    def test_filtered_zip_single_resource(self, tmp_path: Path) -> None:
        """Filtered ZIP with resource_label puts all files under that label."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom data",
            )

        extracted, renamed = _extract_scan_zip(
            zip_path, scan_base, resource_label="DICOM",
        )

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()

    def test_exclude_resources(self, tmp_path: Path) -> None:
        """Excluded resources are not extracted."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/thumb.jpg",
                b"snap",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/brain.nii.gz",
                b"nifti",
            )

        extracted, _ = _extract_scan_zip(
            zip_path, scan_base, exclude_resources=frozenset({"SNAPSHOTS"}),
        )

        assert extracted == 2
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()
        assert (scan_base / "resources" / "NII" / "files" / "brain.nii.gz").exists()
        assert not (scan_base / "resources" / "SNAPSHOTS").exists()

    def test_exclude_multiple_resources(self, tmp_path: Path) -> None:
        """Multiple resources can be excluded simultaneously."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm", b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/t.jpg", b"snap",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/b.nii.gz", b"nii",
            )

        extracted, _ = _extract_scan_zip(
            zip_path, scan_base,
            exclude_resources=frozenset({"SNAPSHOTS", "NII"}),
        )

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "img.dcm").exists()

    def test_skips_hidden_files(self, tmp_path: Path) -> None:
        """Hidden files (starting with .) are not extracted."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/.DS_Store",
                b"macos",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert not (
            scan_base / "resources" / "DICOM" / "files" / ".DS_Store"
        ).exists()

    def test_duplicate_filenames_renamed(self, tmp_path: Path) -> None:
        """Duplicate filenames are renamed with __dup suffix."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        # Pre-create a file to trigger duplicate handling
        target = scan_base / "resources" / "DICOM" / "files"
        target.mkdir(parents=True)
        (target / "img.dcm").write_bytes(b"existing")

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm",
                b"new data",
            )

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert renamed == 1
        assert (target / "img.dcm").read_bytes() == b"existing"
        assert (target / "img__dup1.dcm").read_bytes() == b"new data"

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Path traversal attempts are silently skipped."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/../../evil.txt",
                b"evil",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/good.dcm",
                b"good",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        # Only the safe file should be extracted
        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "good.dcm").exists()
        assert not (tmp_path / "evil.txt").exists()

    def test_unknown_label_uses_fallback(self, tmp_path: Path) -> None:
        """Files without detectable resource label use UNKNOWN."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            # No resources/ or files/ in path
            zf.writestr("some/random/path/data.dat", b"data")

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert (
            scan_base / "resources" / "UNKNOWN" / "files" / "random" / "path" / "data.dat"
        ).exists()

    def test_empty_zip(self, tmp_path: Path) -> None:
        """Empty ZIP produces zero extractions."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w"):
            pass

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 0
        assert renamed == 0

    def test_directory_entries_skipped(self, tmp_path: Path) -> None:
        """Directory entries in ZIP are skipped."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("XNAT_E00001/scans/1/resources/DICOM/", b"")
            zf.writestr("XNAT_E00001/scans/1/resources/DICOM/files/", b"")
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm",
                b"data",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)
        assert extracted == 1

    def test_preserves_binary_content(self, tmp_path: Path) -> None:
        """Binary content is preserved through extraction."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"
        binary_content = bytes(range(256))

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/binary.dcm",
                binary_content,
            )

        _extract_scan_zip(zip_path, scan_base)

        result = scan_base / "resources" / "DICOM" / "files" / "binary.dcm"
        assert result.read_bytes() == binary_content


# =============================================================================
# Session download CLI flag tests
# =============================================================================


class TestSessionDownloadResourceFlags:
    """Tests for session download --resource / --exclude-resource flags."""

    def test_dry_run_with_resource_filter(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Dry run with --resource shows resource types."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "-r", "DICOM", "-r", "NII",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "DICOM" in result.output
        assert "NII" in result.output

    def test_dry_run_with_exclude_resource(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Dry run with --exclude-resource shows excluded types."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "--exclude-resource", "SNAPSHOTS",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "Exclude resources: SNAPSHOTS" in result.output

    def test_dry_run_with_session_resources(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Dry run with --session-resources shows flag status."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "--session-resources",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "Session resources: True" in result.output

    def test_resource_and_exclude_resource_mutual_exclusion(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """--resource and --exclude-resource cannot be combined."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "-r", "DICOM",
                    "--exclude-resource", "SNAPSHOTS",
                    "--dry-run",
                ],
            )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_include_resources_deprecation_warning(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """--include-resources emits DeprecationWarning and maps to --session-resources."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, pytest.warns(
            DeprecationWarning, match="--include-resources is deprecated",
        ):
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "--include-resources",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
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
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """--resource with workers=1 still uses parallel path."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.cli.session._download_session_fast",
        ) as mock_fast:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "-w", "1",
                    "-r", "DICOM",
                ],
            )

        assert result.exit_code == 0
        mock_fast.assert_called_once()
        call_kwargs = mock_fast.call_args[1]
        assert call_kwargs["include_resources"] == ("DICOM",)

    def test_exclude_resource_forces_parallel_path(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """--exclude-resource with workers=1 still uses parallel path."""
        ctx, mock_client = _make_authenticated_context()
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

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.cli.session._download_session_fast",
        ) as mock_fast:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session", "download",
                    "-E", "XNAT_E00001", "-P", "TESTPROJ",
                    "--out", str(tmp_path),
                    "-w", "1",
                    "--exclude-resource", "SNAPSHOTS",
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
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Multiple -r flags are rejected with a clear error."""
        ctx, mock_client = _make_authenticated_context()

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan", "download",
                    "-E", "XNAT_E00001",
                    "-s", "1",
                    "-r", "DICOM", "-r", "NII",
                    "--out", str(tmp_path),
                ],
            )

        assert result.exit_code != 0
        assert "Only one --resource" in result.output

    def test_single_resource_passes_filter(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Single -r passes resource filter to service."""
        ctx, mock_client = _make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True, total=1, succeeded=1, failed=0,
            duration=1.0, total_files=5, total_size_mb=10.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.services.downloads.DownloadService",
        ) as mock_dl_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            result = runner.invoke(
                cli,
                [
                    "scan", "download",
                    "-E", "XNAT_E00001",
                    "-s", "1",
                    "-r", "DICOM",
                    "--out", str(tmp_path),
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_dl_cls.return_value.download_scans.call_args[1]
        assert call_kwargs["resource"] == "DICOM"

    def test_no_resource_passes_none(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """No -r flag passes resource=None (all resources)."""
        ctx, mock_client = _make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True, total=1, succeeded=1, failed=0,
            duration=1.0, total_files=5, total_size_mb=10.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.services.downloads.DownloadService",
        ) as mock_dl_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            result = runner.invoke(
                cli,
                [
                    "scan", "download",
                    "-E", "XNAT_E00001",
                    "-s", "1",
                    "--out", str(tmp_path),
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_dl_cls.return_value.download_scans.call_args[1]
        assert call_kwargs["resource"] is None

    def test_multiple_resources_rejected_with_dry_run(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Multiple -r flags are rejected even with --dry-run."""
        ctx, mock_client = _make_authenticated_context()

        with patch(
            "xnatctl.cli.common.Config.load", return_value=ctx.config,
        ), patch.object(
            Context, "get_client", return_value=mock_client,
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan", "download",
                    "-E", "XNAT_E00001",
                    "-s", "1",
                    "-r", "DICOM", "-r", "NII",
                    "--out", str(tmp_path),
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
            success=True, total=1, succeeded=1, failed=0,
            duration=1.0, total_files=5, total_size_mb=10.0,
            output_path="/tmp/test", session_id="XNAT_E00001",
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
            success=True, total=1, succeeded=1, failed=0,
            duration=1.0, total_files=5, total_size_mb=10.0,
            output_path="/tmp/test", session_id="XNAT_E00001",
        )

        with patch.object(
            service, "download_resource", return_value=mock_summary,
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
