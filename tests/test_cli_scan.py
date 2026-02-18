"""Tests for xnatctl scan CLI commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.models.progress import DownloadSummary


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _make_authenticated_context(
    default_project: str | None = "TESTPROJ",
) -> tuple[Context, MagicMock]:
    """Build a Context with a mocked authenticated client.

    Args:
        default_project: Default project for the profile.

    Returns:
        Tuple of (Context, mock_client).
    """
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
# Scan List
# =============================================================================


class TestScanList:
    """Tests for `scan list` command."""

    def test_scan_list_happy_path(self, runner: CliRunner) -> None:
        """List scans for a session returns table output."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    },
                    {
                        "ID": "2",
                        "type": "T2w",
                        "series_description": "T2-weighted",
                        "quality": "usable",
                        "frames": "32",
                        "note": "",
                    },
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "T1w" in result.output
        assert "T2w" in result.output

    def test_scan_list_with_project(self, runner: CliRunner) -> None:
        """List scans with -P scopes to project endpoint."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["scan", "list", "-E", "SESS001", "-P", "TESTPROJ"]
            )

        assert result.exit_code == 0
        call_url = mock_client.get_json.call_args[0][0]
        assert "/data/projects/TESTPROJ/experiments/SESS001/scans" in call_url

    def test_scan_list_without_project_uses_direct_endpoint(self, runner: CliRunner) -> None:
        """Without -P uses /data/experiments endpoint."""
        ctx, mock_client = _make_authenticated_context(default_project=None)
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        call_url = mock_client.get_json.call_args[0][0]
        assert "/data/experiments/XNAT_E00001/scans" in call_url

    def test_scan_list_default_project_fallback(self, runner: CliRunner) -> None:
        """Falls back to profile default_project for label resolution."""
        ctx, mock_client = _make_authenticated_context(default_project="FALLBACK")
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS_LABEL"])

        assert result.exit_code == 0
        call_url = mock_client.get_json.call_args[0][0]
        assert "/data/projects/FALLBACK/" in call_url

    def test_scan_list_json_output(self, runner: CliRunner) -> None:
        """JSON output returns scan data."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    }
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["scan", "list", "-E", "XNAT_E00001", "-o", "json"]
            )

        assert result.exit_code == 0
        assert "T1w" in result.output

    def test_scan_list_quiet(self, runner: CliRunner) -> None:
        """Quiet mode outputs IDs only."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    }
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["scan", "list", "-E", "XNAT_E00001", "-q"]
            )

        assert result.exit_code == 0
        assert "1" in result.output

    def test_scan_list_empty(self, runner: CliRunner) -> None:
        """Empty result set does not error."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0


# =============================================================================
# Scan Show
# =============================================================================


class TestScanShow:
    """Tests for `scan show` command."""

    def test_scan_show_happy_path(self, runner: CliRunner) -> None:
        """Show scan details by scan ID."""
        ctx, mock_client = _make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/resources"):
                return {
                    "ResultSet": {
                        "Result": [{"label": "DICOM"}, {"label": "NIFTI"}]
                    }
                }
            return {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "1",
                            "type": "T1w",
                            "series_description": "T1-weighted",
                            "quality": "usable",
                            "frames": "176",
                            "note": "",
                        }
                    ]
                }
            }

        mock_client.get_json.side_effect = _get_json_side

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "1"])

        assert result.exit_code == 0

    def test_scan_show_with_project(self, runner: CliRunner) -> None:
        """Show scan with -P scopes to project endpoint."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    }
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["scan", "show", "-E", "SESS001", "-P", "TESTPROJ", "1"]
            )

        assert result.exit_code == 0
        call_url = mock_client.get_json.call_args_list[0][0][0]
        assert "/data/projects/TESTPROJ/experiments/SESS001/scans/1" in call_url

    def test_scan_show_not_found(self, runner: CliRunner) -> None:
        """Non-existent scan prints error and exits 1."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "999"])

        assert result.exit_code == 1


# =============================================================================
# Scan Delete
# =============================================================================


class TestScanDelete:
    """Tests for `scan delete` command."""

    def test_scan_delete_dry_run(self, runner: CliRunner) -> None:
        """Dry run lists scans to delete without deleting."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1,2,3",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "3 scans" in result.output
        mock_client.delete.assert_not_called()

    def test_scan_delete_with_confirmation(self, runner: CliRunner) -> None:
        """Delete scans with -y skips prompt."""
        ctx, mock_client = _make_authenticated_context()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.delete.return_value = mock_response

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1,2",
                    "-y",
                ],
            )

        assert result.exit_code == 0
        assert mock_client.delete.call_count == 2

    def test_scan_delete_wildcard_dry_run(self, runner: CliRunner) -> None:
        """Wildcard '*' fetches all scan IDs in dry run."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "1"},
                    {"ID": "2"},
                    {"ID": "3"},
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "*",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "3 scans" in result.output

    def test_scan_delete_with_project(self, runner: CliRunner) -> None:
        """Delete with -P uses project-scoped endpoint."""
        ctx, mock_client = _make_authenticated_context()
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client.delete.return_value = mock_response

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "SESS001",
                    "-P",
                    "TESTPROJ",
                    "--scans",
                    "1",
                    "-y",
                ],
            )

        assert result.exit_code == 0
        delete_url = mock_client.delete.call_args[0][0]
        assert "/data/projects/TESTPROJ/experiments/SESS001/scans/1" in delete_url

    def test_scan_delete_failure(self, runner: CliRunner) -> None:
        """Failed delete reports error and exits 1."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.delete.side_effect = Exception("Server error")

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1",
                    "-y",
                ],
            )

        assert result.exit_code == 1

    def test_scan_delete_partial_failure(self, runner: CliRunner) -> None:
        """Mixed success/failure reports both and exits 1."""
        ctx, mock_client = _make_authenticated_context()
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_client.delete.side_effect = [mock_ok, Exception("fail")]

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1,2",
                    "-y",
                    "--no-parallel",
                ],
            )

        assert result.exit_code == 1
        assert "Deleted 1 scan" in result.output


# =============================================================================
# Scan Download
# =============================================================================


class TestScanDownload:
    """Tests for `scan download` command."""

    def test_scan_download_dry_run(self, runner: CliRunner, tmp_path) -> None:
        """Dry run previews download without fetching data."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "2 scans" in result.output

    def test_scan_download_dry_run_all(self, runner: CliRunner, tmp_path) -> None:
        """Dry run with '*' shows 'all scans'."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-s",
                    "*",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "all scans" in result.output

    def test_scan_download_dry_run_with_resource(self, runner: CliRunner, tmp_path) -> None:
        """Dry run with --resource shows resource type."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DICOM" in result.output

    def test_scan_download_name_with_path_separator(self, runner: CliRunner, tmp_path) -> None:
        """Name with path separator is rejected."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
                    "--name",
                    "bad/name",
                ],
            )

        assert result.exit_code != 0
        assert "path separators" in result.output

    def test_scan_download_happy_path(self, runner: CliRunner, tmp_path) -> None:
        """Successful download produces success message."""
        ctx, mock_client = _make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=10,
            total_size_mb=15.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.services.downloads.DownloadService"
        ) as mock_dl_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
        assert "Downloaded" in result.output

    def test_scan_download_failure(self, runner: CliRunner, tmp_path) -> None:
        """Failed download exits 1."""
        ctx, mock_client = _make_authenticated_context()
        mock_summary = DownloadSummary(
            success=False,
            total=1,
            succeeded=0,
            failed=1,
            duration=1.0,
            total_files=0,
            total_size_mb=0.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
            errors=["Connection timed out"],
        )

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.services.downloads.DownloadService"
        ) as mock_dl_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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

        assert result.exit_code == 1

    def test_scan_download_json_output(self, runner: CliRunner, tmp_path) -> None:
        """JSON output includes structured download summary."""
        ctx, mock_client = _make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.0,
            total_files=5,
            total_size_mb=10.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls, patch(
            "xnatctl.services.downloads.DownloadService"
        ) as mock_dl_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
        assert '"success"' in result.output
        assert "XNAT_E00001" in result.output


# =============================================================================
# Scan Help
# =============================================================================


class TestScanHelp:
    """Tests for scan subcommand help texts."""

    def test_scan_list_help(self, runner: CliRunner) -> None:
        """scan list --help shows expected options."""
        result = runner.invoke(cli, ["scan", "list", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--project" in result.output

    def test_scan_show_help(self, runner: CliRunner) -> None:
        """scan show --help shows expected options."""
        result = runner.invoke(cli, ["scan", "show", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "SCAN_ID" in result.output

    def test_scan_delete_help(self, runner: CliRunner) -> None:
        """scan delete --help shows expected options."""
        result = runner.invoke(cli, ["scan", "delete", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--dry-run" in result.output
        assert "--yes" in result.output

    def test_scan_download_help(self, runner: CliRunner) -> None:
        """scan download --help shows expected options."""
        result = runner.invoke(cli, ["scan", "download", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--out" in result.output
        assert "--dry-run" in result.output
        assert "--resource" in result.output
        assert "--unzip" in result.output
