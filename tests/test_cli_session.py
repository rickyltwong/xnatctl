"""Tests for xnatctl session CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from conftest import (
    AuthenticatedCLI,
    authenticated_seams,
    make_authenticated_context,
)

from xnatctl.cli.main import cli
from xnatctl.core.exceptions import ResourceNotFoundError, ServerError


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


# =============================================================================
# Session List
# =============================================================================


class TestSessionList:
    """Tests for `session list` command."""

    def test_session_list_happy_path(self, runner: CliRunner) -> None:
        """List sessions with results returns table output."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output
        assert "SESS001" in result.output

    def test_session_list_json_output(self, runner: CliRunner) -> None:
        """List sessions with --output json returns JSON."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "-o", "json"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_list_modality_filter(self, runner: CliRunner) -> None:
        """Modality filter excludes non-matching sessions."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                ["session", "list", "-P", "TESTPROJ", "--modality", "MR", "-o", "json"],
            )

        assert result.exit_code == 0
        assert "MR_SESS" in result.output
        assert "PET_SESS" not in result.output

    def test_session_list_modality_free_text(self, runner: CliRunner) -> None:
        """--modality accepts any free-text value, case-insensitively, and
        actually filters by it.

        A click.Choice(["MR", "PET", "CT", "EEG"]) would reject anything
        else with a usage error; XNAT sessions can carry
        arbitrary modality strings (US, XA, CR, MG, ...). Mixed rows so a
        filter that silently passed everything through would be caught.
        `xnat:usSessionData` is a real XNAT xsiType, confirmed against the
        xnat-web schema.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "US_SESS",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:usSessionData",
                    },
                    {
                        "ID": "XNAT_E00002",
                        "label": "MR_SESS",
                        "subject_label": "SUB002",
                        "date": "2025-01-16",
                        "xsiType": "xnat:mrSessionData",
                    },
                    {
                        "ID": "XNAT_E00003",
                        "label": "CR_SESS",
                        "subject_label": "SUB003",
                        "date": "2025-01-17",
                        "xsiType": "xnat:crSessionData",
                    },
                ]
            }
        }

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "--modality", "us"])

        assert result.exit_code == 0
        assert "US_SESS" in result.output
        assert "MR_SESS" not in result.output
        assert "CR_SESS" not in result.output

    def test_session_list_modality_oct_matches_real_opt_sessiondata(
        self, runner: CliRunner
    ) -> None:
        """XNAT archives OCT sessions as `xnat:optSessionData` (OPT is
        DICOM's modality code for Ophthalmic Tomography), confirmed against
        the xnat-web schema and this project's own 0.2.11 fix for the same
        xsiType. `--modality OCT` (what users actually say) must match it.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "OCT_SESS",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:optSessionData",
                    },
                    {
                        "ID": "XNAT_E00002",
                        "label": "MR_SESS",
                        "subject_label": "SUB002",
                        "date": "2025-01-16",
                        "xsiType": "xnat:mrSessionData",
                    },
                ]
            }
        }

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "--modality", "OCT"])

        assert result.exit_code == 0
        assert "OCT_SESS" in result.output
        assert "MR_SESS" not in result.output

    def test_session_list_modality_pet_excludes_petmr(self, runner: CliRunner) -> None:
        """PETMR is its own xsiType (`xnat:petmrSessionData`) -- --modality
        PET must not also match combined PET/MR sessions.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E00001",
                        "label": "PET_SESS",
                        "subject_label": "SUB001",
                        "date": "2025-01-15",
                        "xsiType": "xnat:petSessionData",
                    },
                    {
                        "ID": "XNAT_E00002",
                        "label": "PETMR_SESS",
                        "subject_label": "SUB002",
                        "date": "2025-01-16",
                        "xsiType": "xnat:petmrSessionData",
                    },
                ]
            }
        }

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "--modality", "PET"])

        assert result.exit_code == 0
        assert "PET_SESS" in result.output
        assert "PETMR_SESS" not in result.output

    def test_session_list_subject_filter(self, runner: CliRunner) -> None:
        """Subject filter passes through to API params."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "-S", "SUB001"])

        assert result.exit_code == 0
        call_args = mock_client.get_json.call_args
        assert call_args[1]["params"]["subject_label"] == "SUB001"

    def test_session_list_no_project_error(self, runner: CliRunner) -> None:
        """Missing project with no default raises ClickException."""
        ctx, mock_client = make_authenticated_context(default_project=None)

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list"])

        assert result.exit_code != 0
        assert "Project required" in result.output

    def test_session_list_default_project_fallback(self, runner: CliRunner) -> None:
        """Falls back to profile default_project when -P not given."""
        ctx, mock_client = make_authenticated_context(default_project="FALLBACK")
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list"])

        assert result.exit_code == 0
        call_args = mock_client.get_json.call_args
        assert "/data/projects/FALLBACK/experiments" in call_args[0][0]

    def test_session_list_quiet(self, runner: CliRunner) -> None:
        """Quiet mode outputs IDs only."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ", "-q"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_list_empty_results(self, runner: CliRunner) -> None:
        """Empty result set does not error."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "list", "-P", "TESTPROJ"])

        assert result.exit_code == 0


# =============================================================================
# Session Show
# =============================================================================


class TestSessionShow:
    """Tests for `session show` command."""

    def test_session_show_by_id(self, runner: CliRunner) -> None:
        """Show session details by experiment ID."""
        ctx, mock_client = make_authenticated_context()
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
                    "Result": [{"ID": "1", "type": "T1w", "series_description": "T1-weighted"}]
                }
            },
            # resources
            {"ResultSet": {"Result": []}},
            # shared projects
            {"ResultSet": {"Result": []}},
        ]
        mock_client.whoami.side_effect = mock_client.get_json

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "XNAT_E00001" in result.output

    def test_session_show_with_project(self, runner: CliRunner) -> None:
        """Show session scoped to project uses project endpoint."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "SESS001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        first_get_call = mock_client.get_json.call_args_list[0]
        assert "/data/projects/TESTPROJ/experiments/SESS001" in first_get_call[0][0]

    def test_session_show_non_mr_resolves_scan_xsi(self, runner: CliRunner) -> None:
        """Non-MR session (e.g. EEG) resolves scan xsiType for scan listing."""
        ctx, mock_client = make_authenticated_context()
        call_count = 0

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if url.endswith("/scans"):
                return {
                    "ResultSet": {
                        "Result": [
                            {"ID": "1", "type": "EEG", "series_description": "EEG recording"}
                        ]
                    }
                }
            if url.endswith("/resources"):
                return {"ResultSet": {"Result": []}}
            return {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00010",
                            "label": "EEG_SESS",
                            "subject_label": "SUB001",
                            "project": "TESTPROJ",
                            "date": "2025-06-01",
                            "xsiType": "xnat:eegSessionData",
                        }
                    ]
                }
            }

        mock_client.get_json.side_effect = _get_json_side

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00010", "-o", "json"])

        assert result.exit_code == 0
        # Find the scans call and verify xsiType param was passed
        for call in mock_client.get_json.call_args_list:
            if "/scans" in call[0][0]:
                params = call[1].get("params", {})
                assert params.get("xsiType") == "xnat:eegScanData"
                break
        else:
            pytest.fail("No /scans call found")

    def test_session_show_not_found(self, runner: CliRunner) -> None:
        """Non-existent session prints error and exits 1."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E99999"])

        assert result.exit_code == 1

    def test_session_show_json_output(self, runner: CliRunner) -> None:
        """JSON output includes scans and resources."""
        ctx, mock_client = make_authenticated_context()

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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001", "-o", "json"])

        assert result.exit_code == 0
        assert '"scans"' in result.output
        assert '"resources"' in result.output

    def test_session_show_tsv_output(self, runner: CliRunner) -> None:
        """`-o tsv` emits real tab-separated blocks, not the Rich table view."""
        ctx, mock_client = make_authenticated_context()

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
            if url.endswith("/projects"):
                # Shared-projects lookup -- empty so this test's block count
                # stays focused on scans/resources; TestSessionShare below
                # covers a non-empty shares block.
                return {"ResultSet": {"Result": []}}
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001", "-o", "tsv"])

        assert result.exit_code == 0
        assert "[Session:" not in result.output
        assert "\x1b" not in result.output
        assert "┃" not in result.output and "│" not in result.output
        blocks = result.output.strip("\n").split("\n\n")
        assert len(blocks) == 3
        session_lines = blocks[0].splitlines()
        assert session_lines[0] == "id\tlabel\tsubject\tproject\tdate\txsi_type"
        assert session_lines[1].split("\t")[0] == "XNAT_E00001"
        scan_lines = blocks[1].splitlines()
        assert scan_lines[0] == "id\ttype\tseries\tquality\tframes"
        assert scan_lines[1].split("\t")[:2] == ["1", "T1w"]
        resource_lines = blocks[2].splitlines()
        assert resource_lines[0] == "label\tformat\tcount\tsize"
        assert resource_lines[1].split("\t")[0] == "DICOM"

    def test_session_show_scans_listing_failure_is_not_silent(self, runner: CliRunner) -> None:
        """A scan-listing failure must be visible, not indistinguishable from "no scans".

        A bare ``except Exception: scans = []`` around the scans fetch
        would render a transient 500 identically -- silently -- to a
        session that genuinely has no scans.
        """
        ctx, mock_client = make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/scans"):
                raise ServerError(500, "GET", url)
            if url.endswith("/resources"):
                return {"ResultSet": {"Result": []}}
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "Warning: could not list scans" in result.output

    def test_session_show_resources_listing_failure_is_not_silent(self, runner: CliRunner) -> None:
        """A resource-listing failure must be visible, not indistinguishable from "no resources".

        A bare ``except Exception: resources = []`` around the resources
        fetch would render a transient 500 identically -- silently -- to a
        session that genuinely has no resources.
        """
        ctx, mock_client = make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/scans"):
                return {"ResultSet": {"Result": []}}
            if url.endswith("/resources"):
                raise ServerError(500, "GET", url)
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["session", "show", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "Warning: could not list resources" in result.output


# =============================================================================
# Session Download
# =============================================================================


class TestSessionDownload:
    """Tests for `session download` command."""

    def test_session_download_dry_run(self, runner: CliRunner, tmp_path) -> None:
        """Dry run previews download without fetching data."""
        ctx, mock_client = make_authenticated_context()
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
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "XNAT_E00001" in result.output

    def test_session_download_dry_run_no_project(self, runner: CliRunner, tmp_path) -> None:
        """Dry run without -P uses direct experiment endpoint."""
        ctx, mock_client = make_authenticated_context(default_project=None)
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

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()

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
                    "--name",
                    "bad/name",
                ],
            )

        assert result.exit_code != 0
        assert "path separator" in result.output

    def test_session_download_name_empty_string_is_rejected(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """An explicit ``--name ""`` must fail validation, not silently fall
        back to the session_id -- a truthy `if name:` check would skip
        validation entirely for an empty string and let ``name or
        session_id`` pick the fallback without ever reporting the caller's
        bad input.
        """
        ctx, mock_client = make_authenticated_context()

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
                    "--name",
                    "",
                ],
            )

        assert result.exit_code != 0
        assert "empty" in result.output.lower()

    def test_session_download_dry_run_label_resolution(self, runner: CliRunner, tmp_path) -> None:
        """Dry run with label shows resolved ID."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
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

    def test_session_download_items_format_fallback(self, runner: CliRunner, tmp_path) -> None:
        """Falls back to items/data_fields format when ResultSet is empty."""
        ctx, mock_client = make_authenticated_context()
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

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()
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
                ],
            )

        assert result.exit_code == 1


# =============================================================================
# Session Help
# =============================================================================


class TestSessionHelp:
    """Tests for session subcommand help texts."""

    def test_session_list_help(self, runner: CliRunner) -> None:
        """Session list --help shows expected options."""
        result = runner.invoke(cli, ["session", "list", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--subject" in result.output
        assert "--modality" in result.output

    def test_session_show_help(self, runner: CliRunner) -> None:
        """Session show --help shows expected options."""
        result = runner.invoke(cli, ["session", "show", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--project" in result.output

    def test_session_download_help(self, runner: CliRunner) -> None:
        """Session download --help shows expected options."""
        result = runner.invoke(cli, ["session", "download", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--out" in result.output
        assert "--dry-run" in result.output
        assert "--workers" in result.output
        assert "--extract" in result.output

    def test_session_upload_help(self, runner: CliRunner) -> None:
        """Session upload --help shows expected options."""
        result = runner.invoke(cli, ["session", "upload", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--subject" in result.output
        assert "--experiment" in result.output
        assert "--dry-run" in result.output

    def test_session_upload_exam_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["session", "upload-exam", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--subject" in result.output
        assert "--experiment" in result.output
        assert "--dry-run" in result.output


def _resolved_experiment_document(
    experiment_id: str = "XNAT_E00001",
    label: str = "SESS001",
    project: str = "TESTPROJ",
    subject_id: str = "XNAT_S00001",
    subject_label: str = "SUB001",
    xsi_type: str = "xnat:mrSessionData",
) -> dict:
    """A format=json document HierarchyService.resolve_experiment can parse.

    Includes ``subject_ID`` (the accession ID), not just ``subject_label`` --
    ``SessionService.set_vars`` needs the resolved subject *ID* to build its
    write path (see its docstring), and ``Session.subject_id`` is aliased
    from ``subject_ID`` specifically, not from the label.
    """
    return {
        "ResultSet": {
            "Result": [
                {
                    "ID": experiment_id,
                    "label": label,
                    "subject_ID": subject_id,
                    "subject_label": subject_label,
                    "project": project,
                    "date": "2025-01-15",
                    "xsiType": xsi_type,
                }
            ]
        }
    }


class TestSessionShare:
    """Tests for `session share`."""

    def test_share_puts_experiment_projects_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            [
                "session",
                "share",
                "-E",
                "XNAT_E00001",
                "--into",
                "OTHERPROJ",
                "--label",
                "SESS_SHARED",
                "--yes",
            ]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/experiments/XNAT_E00001/projects/OTHERPROJ", params={"label": "SESS_SHARED"}
        )

    def test_share_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["session", "share", "-E", "XNAT_E00001", "--into", "OTHERPROJ"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_share_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()

        result = authenticated_cli.invoke(
            ["session", "share", "-E", "XNAT_E00001", "--into", "OTHERPROJ", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_share_not_found(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("session", "GONE")

        result = authenticated_cli.invoke(
            ["session", "share", "-E", "GONE", "--into", "OTHERPROJ", "--yes"]
        )

        assert result.exit_code != 0
        assert "Session not found: GONE" in result.output

    def test_share_rejects_invalid_label(self, authenticated_cli: AuthenticatedCLI) -> None:
        """A --label containing a path/URL-reserved character fails locally, not on the wire."""
        result = authenticated_cli.invoke(
            [
                "session",
                "share",
                "-E",
                "XNAT_E00001",
                "--into",
                "OTHERPROJ",
                "--label",
                "bad%label",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        assert "Invalid experiment label" in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_share_rejects_invalid_source_project(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            [
                "session",
                "share",
                "-E",
                "XNAT_E00001",
                "-P",
                "bad project",
                "--into",
                "OTHERPROJ",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        assert "Invalid project" in result.output
        authenticated_cli.client.put.assert_not_called()


class TestSessionUnshare:
    """Tests for `session unshare`."""

    def test_unshare_deletes_experiment_projects_path(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()
        authenticated_cli.client.delete.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["session", "unshare", "-E", "XNAT_E00001", "--from", "OTHERPROJ", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with(
            "/data/experiments/XNAT_E00001/projects/OTHERPROJ"
        )

    def test_unshare_rejects_invalid_source_project(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            [
                "session",
                "unshare",
                "-E",
                "XNAT_E00001",
                "-P",
                "bad project",
                "--from",
                "OTHERPROJ",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        assert "Invalid project" in result.output
        authenticated_cli.client.delete.assert_not_called()

    def test_unshare_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()

        result = authenticated_cli.invoke(
            ["session", "unshare", "-E", "XNAT_E00001", "--from", "OTHERPROJ", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_not_called()

    def test_unshare_from_primary_project_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Real execution refuses unsharing FROM the session's own primary project."""
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document(
            project="TESTPROJ"
        )

        result = authenticated_cli.invoke(
            ["session", "unshare", "-E", "XNAT_E00001", "--from", "TESTPROJ", "--yes"]
        )

        assert result.exit_code != 0
        assert "primary project" in result.output
        authenticated_cli.client.delete.assert_not_called()

    def test_unshare_from_primary_project_refused_dry_run_agrees(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """`--dry-run` must refuse the same case execution refuses, not report success.

        A dry-run that returns before the primary-project comparison would
        report "would remove" for a call execution actually refuses
        (because XNAT would delete the session outright).
        """
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document(
            project="TESTPROJ"
        )

        result = authenticated_cli.invoke(
            ["session", "unshare", "-E", "XNAT_E00001", "--from", "TESTPROJ", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "primary project" in result.output
        assert "Would remove" not in result.output
        authenticated_cli.client.delete.assert_not_called()


class TestSessionShowSharedProjects:
    """Regression tests for the `session show` shared-projects extension."""

    def test_json_includes_shared_projects(self, authenticated_cli: AuthenticatedCLI) -> None:
        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/scans"):
                return {"ResultSet": {"Result": []}}
            if url.endswith("/resources"):
                return {"ResultSet": {"Result": []}}
            if url.endswith("/projects"):
                return {
                    "ResultSet": {
                        "Result": [
                            {"ID": "TESTPROJ", "label": "SESS001"},
                            {"ID": "OTHERPROJ", "label": "XNAT_E00001"},
                        ]
                    }
                }
            return _resolved_experiment_document()

        authenticated_cli.client.get_json.side_effect = _get_json_side

        result = authenticated_cli.invoke(["session", "show", "-E", "XNAT_E00001", "-o", "json"])

        assert result.exit_code == 0
        assert '"shared_projects"' in result.output
        assert "OTHERPROJ" in result.output
        # The primary project (TESTPROJ) is filtered out of the shares list.
        payload = json.loads(result.output)
        assert payload["shared_projects"] == [{"project": "OTHERPROJ", "label": "XNAT_E00001"}]


class TestSessionVars:
    """Tests for `session vars` (read)."""

    def _fields_document(self, fields: list[tuple[str, str]]) -> dict:
        return {
            "items": [
                {
                    "data_fields": {
                        "ID": "XNAT_E00001",
                        "label": "SESS001",
                        "subject_ID": "XNAT_S00001",
                    },
                    "meta": {"xsi:type": "xnat:mrSessionData"},
                    "children": [
                        {
                            "field": "fields/field",
                            "items": [
                                {"data_fields": {"name": name, "field": value}, "children": []}
                                for name, value in fields
                            ],
                        }
                    ],
                }
            ]
        }

    def test_vars_table(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            _resolved_experiment_document(),
            self._fields_document([("studytag", "phase1")]),
        ]

        result = authenticated_cli.invoke(["session", "vars", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "studytag" in result.output
        assert "phase1" in result.output

    def test_vars_json(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            _resolved_experiment_document(),
            self._fields_document([("studytag", "phase1")]),
        ]

        result = authenticated_cli.invoke(["session", "vars", "-E", "XNAT_E00001", "-o", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == [{"name": "studytag", "value": "phase1"}]

    def test_vars_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            _resolved_experiment_document(),
            self._fields_document([("studytag", "phase1"), ("cohort", "A")]),
        ]

        result = authenticated_cli.invoke(["session", "vars", "-E", "XNAT_E00001", "-q"])

        assert result.exit_code == 0
        assert result.output.strip().splitlines() == ["studytag", "cohort"]

    def test_vars_not_found(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("session", "GONE")

        result = authenticated_cli.invoke(["session", "vars", "-E", "GONE"])

        assert result.exit_code != 0
        assert "Session not found: GONE" in result.output

    def test_vars_missing_experiment_is_usage_error(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(["session", "vars"])

        assert result.exit_code != 0
        authenticated_cli.client.get_json.assert_not_called()

    def test_vars_rejects_invalid_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        """`-P` must be validated here the same way the subject equivalent validates it."""
        result = authenticated_cli.invoke(
            ["session", "vars", "-E", "XNAT_E00001", "-P", "bad project"]
        )

        assert result.exit_code != 0
        assert "Invalid project" in result.output
        authenticated_cli.client.get_json.assert_not_called()


class TestSessionVarsSet:
    """Tests for `session vars set`."""

    def test_set_single_pair(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["session", "vars", "set", "-E", "XNAT_E00001", "studytag=phase1", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/projects/TESTPROJ/subjects/XNAT_S00001/experiments/XNAT_E00001",
            params={
                "xsiType": "xnat:mrSessionData",
                "xnat:mrSessionData/fields/field[name=studytag]/field": "phase1",
            },
        )

    def test_set_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["session", "vars", "set", "-E", "XNAT_E00001", "studytag=phase1"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_experiment_document()

        result = authenticated_cli.invoke(
            ["session", "vars", "set", "-E", "XNAT_E00001", "studytag=phase1", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_set_dry_run_session_not_found_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """`--dry-run` must refuse a session it can't resolve, matching execution.

        A dry-run that returns before resolving the session would report
        "would set" for a session that doesn't exist.
        """
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("session", "GONE")

        result = authenticated_cli.invoke(
            ["session", "vars", "set", "-E", "GONE", "studytag=phase1", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "Would set" not in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_set_rejects_invalid_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        """`-P` must be validated here the same way the subject equivalent validates it."""
        result = authenticated_cli.invoke(
            [
                "session",
                "vars",
                "set",
                "-E",
                "XNAT_E00001",
                "-P",
                "bad project",
                "studytag=phase1",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        assert "Invalid project" in result.output
        authenticated_cli.client.get_json.assert_not_called()
        authenticated_cli.client.put.assert_not_called()

    def test_set_no_pairs_is_usage_error(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["session", "vars", "set", "-E", "XNAT_E00001", "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_duplicate_key_is_usage_error(self, authenticated_cli: AuthenticatedCLI) -> None:
        """Silently keeping only the last value would send less than the user asked for."""
        result = authenticated_cli.invoke(
            [
                "session",
                "vars",
                "set",
                "-E",
                "XNAT_E00001",
                "studytag=one",
                "studytag=two",
                "--yes",
            ]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_dry_run_rejects_invalid_field_name(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run must reject what execution rejects, not report a false success."""
        result = authenticated_cli.invoke(
            ["session", "vars", "set", "-E", "XNAT_E00001", "a/b=value", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "custom variable name" in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_set_batch_applies_to_every_id(
        self, authenticated_cli: AuthenticatedCLI, tmp_path: Path
    ) -> None:
        batch_file = tmp_path / "ids.txt"
        batch_file.write_text("XNAT_E00001\nXNAT_E00002\n")
        authenticated_cli.client.get_json.side_effect = [
            _resolved_experiment_document(experiment_id="XNAT_E00001"),
            _resolved_experiment_document(experiment_id="XNAT_E00002"),
        ]
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["session", "vars", "set", "--batch", str(batch_file), "studytag=phase1", "--yes"]
        )

        assert result.exit_code == 0
        assert authenticated_cli.client.put.call_count == 2
