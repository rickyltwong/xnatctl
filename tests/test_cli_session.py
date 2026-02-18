"""Tests for xnatctl session CLI commands."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.common import Context
from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


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
# Session List
# =============================================================================


class TestSessionList:
    """Tests for `session list` command."""

    def test_session_list_happy_path(self, runner: CliRunner) -> None:
        """List sessions with results returns table output."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:mrSessionData",
                    },
                    {
                        "ID": "XNAT_E00002",
                        "label": "SESS002",
                        "subject_label": "SUB002",
                        "date": "2025-01-16",
                        "xsiType": "xnat:petSessionData",
                    },
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output
        assert "SESS001" in result.output

    def test_session_list_json_output(self, runner: CliRunner) -> None:
        """List sessions with --output json returns JSON."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:mrSessionData",
                    },
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "-o", "json"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_list_modality_filter(self, runner: CliRunner) -> None:
        """Modality filter excludes non-matching sessions."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "MR_SESS",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:MRSessionData",
                    },
                    {
                        "ID": "XNAT_E00002",
                        "label": "PET_SESS",
                        "subject_label": "SUB002",
                        "date": "2025-01-16",
                        "xsiType": "xnat:PETSessionData",
                    },
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                ["session", "list", "-P", "TESTPROJ", "--modality", "MR", "-o", "json"],
            )

        assert result.exit_code == 0
        assert "MR_SESS" in result.output
        assert "PET_SESS" not in result.output

    def test_session_list_subject_filter(self, runner: CliRunner) -> None:
        """Subject filter passes through to API params."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["session", "list", "-P", "TESTPROJ", "-S", "SUB001"]
            )

        assert result.exit_code == 0
        call_args = mock_client.get_json.call_args
        assert call_args[1]["params"]["subject_label"] == "SUB001"

    def test_session_list_no_project_error(self, runner: CliRunner) -> None:
        """Missing project with no default raises ClickException."""
        ctx, mock_client = _make_authenticated_context(default_project=None)

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list"])

        assert result.exit_code != 0
        assert "Project required" in result.output

    def test_session_list_default_project_fallback(self, runner: CliRunner) -> None:
        """Falls back to profile default_project when -P not given."""
        ctx, mock_client = _make_authenticated_context(default_project="FALLBACK")
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list"])

        assert result.exit_code == 0
        call_args = mock_client.get_json.call_args
        assert "/data/projects/FALLBACK/experiments" in call_args[0][0]

    def test_session_list_quiet(self, runner: CliRunner) -> None:
        """Quiet mode outputs IDs only."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:mrSessionData",
                    },
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "-q"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_list_empty_results(self, runner: CliRunner) -> None:
        """Empty result set does not error."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ"])

        assert result.exit_code == 0


# =============================================================================
# Session Show
# =============================================================================


class TestSessionShow:
    """Tests for `session show` command."""

    def test_session_show_by_id(self, runner: CliRunner) -> None:
        """Show session details by experiment ID."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.side_effect = [
            # whoami
            {"login": "user"},
            # session details
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "subject_label": "SUB001",
                            "project": "TESTPROJ",
                            "date": "2025-01-15",
                            "xsiType": "xnat:mrSessionData",
                        }
                    ]
                }
            },
            # scans
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "1", "type": "T1w", "series_description": "T1-weighted"}
                    ]
                }
            },
            # resources
            {"ResultSet": {"Result": []}},
        ]
        mock_client.whoami.side_effect = mock_client.get_json

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_show_with_project(self, runner: CliRunner) -> None:
        """Show session scoped to project uses project endpoint."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "subject_label": "SUB001",
                        "project": "TESTPROJ",
                        "date": "2025-01-15",
                        "xsiType": "xnat:mrSessionData",
                    }
                ]
            }
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["session", "show", "-E", "SESS001", "-P", "TESTPROJ"]
            )

        assert result.exit_code == 0
        first_get_call = mock_client.get_json.call_args_list[0]
        assert "/data/projects/TESTPROJ/experiments/SESS001" in first_get_call[0][0]

    def test_session_show_not_found(self, runner: CliRunner) -> None:
        """Non-existent session prints error and exits 1."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E99999"])

        assert result.exit_code == 1

    def test_session_show_json_output(self, runner: CliRunner) -> None:
        """JSON output includes scans and resources."""
        ctx, mock_client = _make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/scans"):
                return {
                    "ResultSet": {
                        "Result": [{"ID": "1", "type": "T1w", "series_description": "T1"}]
                    }
                }
            if url.endswith("/resources"):
                return {
                    "ResultSet": {
                        "Result": [{"label": "DICOM", "format": "DICOM", "file_count": "10"}]
                    }
                }
            return {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "subject_label": "SUB001",
                            "project": "TESTPROJ",
                            "date": "2025-01-15",
                            "xsiType": "xnat:mrSessionData",
                        }
                    ]
                }
            }

        mock_client.get_json.side_effect = _get_json_side

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli, ["session", "show", "-E", "XNAT_E00001", "-o", "json"]
            )

        assert result.exit_code == 0
        assert '"scans"' in result.output
        assert '"resources"' in result.output


# =============================================================================
# Session Download
# =============================================================================


class TestSessionDownload:
    """Tests for `session download` command."""

    def test_session_download_dry_run(self, runner: CliRunner, tmp_path) -> None:
        """Dry run previews download without fetching data."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
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

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "XNAT_E00001" in result.output

    def test_session_download_dry_run_no_project(self, runner: CliRunner, tmp_path) -> None:
        """Dry run without -P uses direct experiment endpoint."""
        ctx, mock_client = _make_authenticated_context(default_project=None)
        mock_client.get_json.return_value = {
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

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        first_get_call = mock_client.get_json.call_args_list[0]
        assert "/data/experiments/XNAT_E00001" in first_get_call[0][0]

    def test_session_download_session_not_found(self, runner: CliRunner, tmp_path) -> None:
        """Missing session exits with error."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "XNAT_E99999",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1

    def test_session_download_name_with_path_separator(self, runner: CliRunner, tmp_path) -> None:
        """Name with path separator is rejected."""
        ctx, mock_client = _make_authenticated_context()

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
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
                    "--name",
                    "bad/name",
                ],
            )

        assert result.exit_code != 0
        assert "path separators" in result.output

    def test_session_download_dry_run_label_resolution(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Dry run with label shows resolved ID."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS_LABEL",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
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
                    "session",
                    "download",
                    "-E",
                    "SESS_LABEL",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "Resolved ID: XNAT_E00001" in result.output

    def test_session_download_items_format_fallback(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Falls back to items/data_fields format when ResultSet is empty."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {"Result": []},
            "items": [
                {
                    "data_fields": {
                        "ID": "XNAT_E00001",
                        "project": "TESTPROJ",
                        "subject_ID": "XNAT_S00001",
                    }
                }
            ],
        }

        with patch("xnatctl.cli.common.Config.load", return_value=ctx.config), patch.object(
            Context, "get_client", return_value=mock_client
        ), patch("xnatctl.cli.common.AuthManager") as mock_auth_cls:
            mock_auth_cls.return_value = ctx.auth_manager
            result = runner.invoke(
                cli,
                [
                    "session",
                    "download",
                    "-E",
                    "SESS_LABEL",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output

    def test_session_download_no_subject_error(self, runner: CliRunner, tmp_path) -> None:
        """Error when subject cannot be determined."""
        ctx, mock_client = _make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "project": "TESTPROJ",
                        "subject_ID": "",
                        "subject_label": "",
                    }
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
                    "session",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-P",
                    "TESTPROJ",
                    "--out",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 1


# =============================================================================
# Session Help
# =============================================================================


class TestSessionHelp:
    """Tests for session subcommand help texts."""

    def test_session_list_help(self, runner: CliRunner) -> None:
        """session list --help shows expected options."""
        result = runner.invoke(cli, ["session", "list", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--subject" in result.output
        assert "--modality" in result.output

    def test_session_show_help(self, runner: CliRunner) -> None:
        """session show --help shows expected options."""
        result = runner.invoke(cli, ["session", "show", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--project" in result.output

    def test_session_download_help(self, runner: CliRunner) -> None:
        """session download --help shows expected options."""
        result = runner.invoke(cli, ["session", "download", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--out" in result.output
        assert "--dry-run" in result.output
        assert "--workers" in result.output
        assert "--unzip" in result.output

    def test_session_upload_help(self, runner: CliRunner) -> None:
        """session upload --help shows expected options."""
        result = runner.invoke(cli, ["session", "upload", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--subject" in result.output
        assert "--session" in result.output
        assert "--dry-run" in result.output
