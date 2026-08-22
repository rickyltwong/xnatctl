"""Tests for xnatctl scan CLI commands."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import authenticated_seams, make_authenticated_context

from xnatctl.cli.main import cli
from xnatctl.models.progress import DownloadSummary, VerificationReport


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


# =============================================================================
# Scan List
# =============================================================================


def _exp_metadata(
    xsi_type: str = "xnat:mrSessionData",
    *,
    experiment_id: str | None = "XNAT_E00001",
    subject_id: str | None = "XNAT_S00001",
) -> dict[str, Any]:
    """Build an experiment metadata response.

    Real XNAT experiment documents always carry ``ID`` and ``subject_ID``. Both
    matter: the accession ID makes nested calls canonical, and the subject is
    what makes the nested URL route at all under a project (see
    ``TestScanNestedUrlRouting``). Pass ``None`` to model a sparse response.
    """
    row: dict[str, Any] = {"xsiType": xsi_type}
    if experiment_id:
        row["ID"] = experiment_id
    if subject_id:
        row["subject_ID"] = subject_id
    return {"ResultSet": {"Result": [row]}}


def _scan_results(scans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a scan listing response."""
    return {"ResultSet": {"Result": scans or []}}


class TestScanList:
    """Tests for `scan list` command."""

    def test_scan_list_happy_path(self, runner: CliRunner) -> None:
        """List scans for a session returns table output."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata("xnat:mrSessionData"),
            _scan_results(
                [
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
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "T1w" in result.output
        assert "T2w" in result.output

    def test_scan_list_with_project(self, runner: CliRunner) -> None:
        """List scans with -P scopes to project endpoint."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        # Second call is the scans listing. It stays inside the project (ACLs
        # apply) and goes through the subject, which is what makes XNAT route
        # the /scans suffix instead of echoing the experiment document.
        scan_call_url = mock_client.get_json.call_args_list[1][0][0]
        assert (
            "/data/projects/TESTPROJ/subjects/XNAT_S00001/experiments/XNAT_E00001/scans"
            in scan_call_url
        )

    def test_scan_list_without_project_uses_direct_endpoint(self, runner: CliRunner) -> None:
        """Without -P uses /data/experiments endpoint."""
        ctx, mock_client = make_authenticated_context(default_project=None)
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        scan_call_url = mock_client.get_json.call_args_list[1][0][0]
        assert "/data/experiments/XNAT_E00001/scans" in scan_call_url

    def test_scan_list_inspect_swallows_only_404(self, runner: CliRunner) -> None:
        """Non-404 errors from the experiment inspect probe propagate to the user.

        The legacy ``except Exception`` in ``_inspect_experiment`` masked every
        failure as "no experiment metadata", which produced a misleading "No
        results" downstream. Only ``ResourceNotFoundError`` should be swallowed.
        """
        from xnatctl.core.exceptions import NetworkError, ResourceNotFoundError

        # Case A: ResourceNotFoundError on the inspect probe is swallowed; the
        # scan list call still runs and returns the same output as before.
        ctx, mock_client = make_authenticated_context(default_project=None)
        mock_client.get_json.side_effect = [
            ResourceNotFoundError("session", "XNAT_E00001"),
            _scan_results(),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        # The scan list call still ran after the swallowed 404.
        assert mock_client.get_json.call_count == 2

        # Case B: a non-404 (network) error must propagate, not be swallowed.
        ctx2, mock_client2 = make_authenticated_context(default_project=None)
        mock_client2.get_json.side_effect = NetworkError("upstream timeout")

        with authenticated_seams(ctx2, mock_client2):
            result2 = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result2.exit_code != 0
        # Only the inspect probe ran; the scan list call was never issued.
        assert mock_client2.get_json.call_count == 1

    def test_scan_list_default_project_fallback(self, runner: CliRunner) -> None:
        """Falls back to profile default_project for label resolution."""
        ctx, mock_client = make_authenticated_context(default_project="FALLBACK")
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS_LABEL"])

        assert result.exit_code == 0
        scan_call_url = mock_client.get_json.call_args_list[1][0][0]
        assert "/data/projects/FALLBACK/" in scan_call_url

    def test_scan_list_json_output(self, runner: CliRunner) -> None:
        """JSON output returns scan data."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results(
                [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    }
                ]
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001", "-o", "json"])

        assert result.exit_code == 0
        assert "T1w" in result.output

    def test_scan_list_quiet(self, runner: CliRunner) -> None:
        """Quiet mode outputs IDs only."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results(
                [
                    {
                        "ID": "1",
                        "type": "T1w",
                        "series_description": "T1-weighted",
                        "quality": "usable",
                        "frames": "176",
                        "note": "",
                    }
                ]
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001", "-q"])

        assert result.exit_code == 0
        assert "1" in result.output

    def test_scan_list_empty(self, runner: CliRunner) -> None:
        """A session with genuinely no scans does not error (fallback also empty)."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results(),  # unfiltered listing empty
            _scan_results(),  # xsiType fallback also empty
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0

    def test_scan_list_opt_session_lists_other_dicom_scans(self, runner: CliRunner) -> None:
        """Regression for issue #16: xnat:otherDicomScanData scans are listed.

        An ``xnat:optSessionData`` session holds ``xnat:otherDicomScanData``
        scans, which cannot be derived from the session xsiType. ``scan list``
        must therefore query unfiltered rather than filtering on a guessed scan
        xsiType (which would drop every scan). The unfiltered listing returns
        all three scans and no fallback call is needed.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata("xnat:optSessionData"),
            _scan_results(
                [
                    {"ID": "1", "type": "Angiography", "series_description": "", "quality": ""},
                    {"ID": "2", "type": "Macular Cube", "series_description": "", "quality": ""},
                    {"ID": "3", "type": "Optic Disc Cube", "series_description": "", "quality": ""},
                ]
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "Angiography" in result.output
        assert "Macular Cube" in result.output
        assert "Optic Disc Cube" in result.output
        # Experiment metadata + exactly one unfiltered scans call (no fallback).
        assert mock_client.get_json.call_count == 2
        scan_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in scan_call_kwargs.get("params", {})

    def test_scan_list_mr_session_lists_unfiltered(self, runner: CliRunner) -> None:
        """Imaging (MR) sessions list scans without a guessed xsiType filter."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata("xnat:mrSessionData"),
            _scan_results(
                [{"ID": "1", "type": "T1", "series_description": "MPRAGE", "quality": "usable"}]
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "MPRAGE" in result.output
        # Exactly two calls: experiment metadata + one unfiltered scan listing.
        assert mock_client.get_json.call_count == 2
        scan_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in scan_call_kwargs.get("params", {})

    def test_scan_list_eeg_session_falls_back_to_xsi_type(self, runner: CliRunner) -> None:
        """Non-imaging (EEG) sessions that list empty unfiltered fall back to the scan xsiType.

        Some non-imaging session types return an empty ResultSet on an unfiltered
        ``/scans`` request; for those, ``scan list`` retries with the matching
        scan xsiType (``xnat:eegScanData``) rather than reporting no scans.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata("xnat:eegSessionData"),
            _scan_results(),  # unfiltered listing returns nothing
            _scan_results([{"ID": "1", "type": "EEG", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "EEG" in result.output
        # Primary scans call is unfiltered; the fallback carries the eeg scan xsiType.
        primary_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in primary_call_kwargs.get("params", {})
        fallback_call_kwargs = mock_client.get_json.call_args_list[2][1]
        assert fallback_call_kwargs["params"]["xsiType"] == "xnat:eegScanData"


# =============================================================================
# Scan Show
# =============================================================================


class TestScanShow:
    """Tests for `scan show` command."""

    def test_scan_show_happy_path(self, runner: CliRunner) -> None:
        """Show scan details by scan ID."""
        ctx, mock_client = make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/resources"):
                return {"ResultSet": {"Result": [{"label": "DICOM"}, {"label": "NIFTI"}]}}
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

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "1"])

        assert result.exit_code == 0

    def test_scan_show_items_response(self, runner: CliRunner) -> None:
        """Show scan details from an `items[]` response shape."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "project": "PROJ",
                            "subject_ID": "XNAT_S00001",
                        },
                        "meta": {"xsi:type": "xnat:petSessionData"},
                        "children": [],
                    }
                ]
            },
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "12",
                            "type": "PET",
                            "series_description": "Series",
                            "quality": "usable",
                            "frames": 712,
                            "note": "",
                        },
                        "meta": {"xsi:type": "xnat:petScanData"},
                        "children": [],
                    }
                ]
            },
            {"ResultSet": {"Result": [{"label": "DICOM"}]}},
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "12", "-o", "json"])

        assert result.exit_code == 0
        assert '"id": "12"' in result.output

    def test_scan_show_with_project(self, runner: CliRunner) -> None:
        """Show scan with -P resolves to the canonical experiment ID for nested calls."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "project": "TESTPROJ",
                            "subject_ID": "XNAT_S00001",
                            "xsiType": "xnat:mrSessionData",
                        }
                    ]
                }
            },
            {
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
            },
            {"ResultSet": {"Result": []}},
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "show", "-E", "SESS001", "-P", "TESTPROJ", "1"])

        assert result.exit_code == 0
        resolve_url = mock_client.get_json.call_args_list[0][0][0]
        assert "/data/projects/TESTPROJ/experiments/SESS001" in resolve_url
        # After inspection, nested calls preserve project scope so ACLs apply,
        # and address the scan through the subject so XNAT routes the suffix.
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert (
            "/data/projects/TESTPROJ/subjects/XNAT_S00001/experiments/XNAT_E00001/scans/1"
            in scan_url
        )

    def test_scan_show_not_found(self, runner: CliRunner) -> None:
        """Non-existent scan prints error and exits 1."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "999"])

        assert result.exit_code == 1


# =============================================================================
# Scan Delete
# =============================================================================


class TestScanDelete:
    """Tests for `scan delete` command."""

    def test_scan_delete_dry_run(self, runner: CliRunner) -> None:
        """Dry run lists scans to delete without deleting."""
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.delete.return_value = mock_response

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results([{"ID": "1"}, {"ID": "2"}, {"ID": "3"}]),
        ]

        with authenticated_seams(ctx, mock_client):
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
        """Delete with -P targets the scan under its subject, not the experiment.

        ``/data/projects/{P}/experiments/{E}/scans/1`` does not address a scan:
        XNAT ignores the suffix and the URL resolves to the experiment itself,
        so a DELETE there risks removing the whole session. The subject segment
        is what makes it address the scan.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = _exp_metadata()
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client.delete.return_value = mock_response

        with authenticated_seams(ctx, mock_client):
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
        assert (
            "/data/projects/TESTPROJ/subjects/XNAT_S00001/experiments/XNAT_E00001/scans/1"
            in delete_url
        )
        # The unrouted prefix must not be what gets DELETEd.
        assert "/data/projects/TESTPROJ/experiments/" not in delete_url

    def test_scan_delete_failure(self, runner: CliRunner) -> None:
        """Failed delete reports error and exits 1."""
        ctx, mock_client = make_authenticated_context()
        mock_client.delete.side_effect = Exception("Server error")

        with authenticated_seams(ctx, mock_client):
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
        ctx, mock_client = make_authenticated_context()
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_client.delete.side_effect = [mock_ok, Exception("fail")]

        with authenticated_seams(ctx, mock_client):
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
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "2 scans" in result.output

    def test_scan_download_dry_run_all(self, runner: CliRunner, tmp_path) -> None:
        """Dry run with '*' shows 'all scans'."""
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
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DICOM" in result.output

    def test_scan_download_name_with_path_separator(self, runner: CliRunner, tmp_path) -> None:
        """Name with path separator is rejected."""
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
                    "--out",
                    str(tmp_path),
                    "--name",
                    "bad/name",
                ],
            )

        assert result.exit_code != 0
        assert "path separator" in result.output

    def test_scan_download_name_empty_string_is_rejected(self, runner: CliRunner, tmp_path) -> None:
        """An explicit ``--name ""`` must fail validation, not silently fall
        back to the session_id -- `if name:` used to skip validation
        entirely for an empty string and let ``name or session_id`` pick
        the fallback without ever reporting the caller's bad input.
        """
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
                    "--out",
                    str(tmp_path),
                    "--name",
                    "",
                ],
            )

        assert result.exit_code != 0
        assert "empty" in result.output.lower()

    def test_scan_download_reserved_session_id_fallback_is_rejected(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Without --name, the raw session_id becomes the local output
        directory -- "CON" passes validate_session_id (a legal XNAT
        accession/label) but is a reserved Windows device name, so it must
        still be rejected before being used as a directory name. Mirrors
        session download's own fallback validation.
        """
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "CON",
                    "-s",
                    "1",
                    "--out",
                    str(tmp_path),
                ],
            )

        assert result.exit_code != 0
        assert "reserved" in result.output.lower()

    def test_scan_download_multiple_resources_rejected(self, runner: CliRunner, tmp_path) -> None:
        """Multiple --resource values are rejected."""
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

    def test_scan_download_happy_path(self, runner: CliRunner, tmp_path) -> None:
        """Successful download produces success message."""
        ctx, mock_client = make_authenticated_context()
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
                ],
            )

        assert result.exit_code == 0
        assert "Downloaded" in result.output

    def test_scan_download_verify_clean_exits_zero(self, runner: CliRunner, tmp_path) -> None:
        """--verify with a clean comparison exits 0 and reports what was verified."""
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=1,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        clean_report = VerificationReport(matched=1)

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = clean_report
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
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Verified 1 files" in result.stderr

    def test_scan_download_verify_mismatch_fails_the_command(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """A mismatch found by --verify must fail the command and name the file."""
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=1,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        bad_report = VerificationReport(matched=0, mismatched=["scans/1/resources/DICOM/0001.dcm"])

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = bad_report
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
                ],
            )

        assert result.exit_code == 1
        assert "MISMATCH" in result.stderr
        assert "scans/1/resources/DICOM/0001.dcm" in result.stderr
        # The download itself succeeded; only verification failed -- the
        # message must say so, not the misleading "Download failed".
        assert "Verification failed" in result.stderr
        assert "Download failed" not in result.stderr

    def test_scan_download_verify_all_unverifiable_fails(self, runner: CliRunner, tmp_path) -> None:
        """The server had no checksums for anything downloaded -- must not
        pass silently just because nothing mismatched.
        """
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=1,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        unverifiable_report = VerificationReport(
            matched=0, unverifiable=["scans/1/resources/DICOM/0001.dcm"]
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = unverifiable_report
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
                ],
            )

        assert result.exit_code == 1
        assert "no checksums" in result.stderr.lower()

    def test_scan_download_verify_empty_manifest_fails(self, runner: CliRunner, tmp_path) -> None:
        """An empty server manifest over files on disk must fail, not read as
        'Verified 0 files'.
        """
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=1,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        empty_manifest_report = VerificationReport(
            missing_remote=["scans/1/resources/DICOM/0001.dcm"]
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = empty_manifest_report
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
                ],
            )

        assert result.exit_code == 1
        assert "listed none" in result.stderr
        assert "nothing was verified" in result.stderr

    def test_scan_download_verify_reports_files_absent_from_manifest(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Manifest-uncovered files do not fail a run that matched something,
        but must be reported rather than staying silent.
        """
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=2,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        partial_report = VerificationReport(
            matched=1, missing_remote=["scans/1/resources/DICOM/0002.dcm"]
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = partial_report
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
                ],
            )

        assert result.exit_code == 0
        assert "Verified 1 files" in result.stderr
        assert "absent from the server manifest" in result.stderr

    def test_scan_download_verify_collision_fails(self, runner: CliRunner, tmp_path) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_summary = DownloadSummary(
            success=True,
            total=1,
            succeeded=1,
            failed=0,
            duration=2.5,
            total_files=1,
            total_size_mb=1.0,
            output_path=str(tmp_path / "XNAT_E00001"),
            session_id="XNAT_E00001",
        )
        colliding_report = VerificationReport(
            matched=0, collisions=["scans/1/resources/DICOM/0001.dcm"]
        )

        with (
            authenticated_seams(ctx, mock_client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.return_value = mock_summary
            mock_dl_cls.return_value.verify_scan_downloads.return_value = colliding_report
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
                ],
            )

        assert result.exit_code == 1
        assert "COLLISION" in result.stderr
        assert "scans/1/resources/DICOM/0001.dcm" in result.stderr

    def test_scan_download_failure(self, runner: CliRunner, tmp_path) -> None:
        """Failed download exits 1."""
        ctx, mock_client = make_authenticated_context()
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
                ],
            )

        assert result.exit_code == 1

    def test_scan_download_failure_json_exits_nonzero(self, runner: CliRunner, tmp_path) -> None:
        """A failed download must exit 1 under -o json, not just table."""
        ctx, mock_client = make_authenticated_context()
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

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["status"] == "failed"  # JSON summary still emitted

    def test_scan_download_json_output(self, runner: CliRunner, tmp_path) -> None:
        """JSON output includes structured download summary."""
        ctx, mock_client = make_authenticated_context()
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
        assert payload["status"] == "success"
        assert payload["session_id"] == "XNAT_E00001"
        assert payload["files"] == 5
        assert payload["bytes"] == 10 * 1024 * 1024


# =============================================================================
# Scan Help
# =============================================================================


class TestScanHelp:
    """Tests for scan subcommand help texts."""

    def test_scan_list_help(self, runner: CliRunner) -> None:
        """Scan list --help shows expected options."""
        result = runner.invoke(cli, ["scan", "list", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--project" in result.output

    def test_scan_show_help(self, runner: CliRunner) -> None:
        """Scan show --help shows expected options."""
        result = runner.invoke(cli, ["scan", "show", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "SCAN_ID" in result.output

    def test_scan_delete_help(self, runner: CliRunner) -> None:
        """Scan delete --help shows expected options."""
        result = runner.invoke(cli, ["scan", "delete", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--dry-run" in result.output
        assert "--yes" in result.output

    def test_scan_download_help(self, runner: CliRunner) -> None:
        """Scan download --help shows expected options."""
        result = runner.invoke(cli, ["scan", "download", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--out" in result.output
        assert "--dry-run" in result.output
        assert "--resource" in result.output
        assert "--extract" in result.output


# =============================================================================
# Nested URL routing
# =============================================================================


class TestScanNestedUrlRouting:
    """Regression tests for the URL prefix used by nested scan calls.

    XNAT does not route sub-resource suffixes on
    ``/data/projects/{P}/experiments/{E}``. It answers ``/scans`` -- or any
    other suffix, including nonsense ones -- with the parent experiment
    document. ``extract_rows`` finds no ``ResultSet`` there and returns an empty
    list, so ``scan list -P`` reported "no scans" for sessions that had them,
    ``scan show -P`` printed session fields under a scan heading, and
    ``scan delete -P`` aimed a DELETE at the experiment.

    Routing the call through the subject fixes all three at once.
    """

    def test_project_scoped_listing_goes_through_the_subject(self, runner: CliRunner) -> None:
        """With -P, the scans URL carries the subject from the experiment doc."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(subject_id="XNAT_S00042"),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert "/subjects/XNAT_S00042/" in scan_url

    def test_explicit_subject_is_not_overwritten(self, runner: CliRunner) -> None:
        """A user-supplied -S wins over the subject_ID in the experiment doc."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(subject_id="XNAT_S00042"),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli, ["scan", "list", "-P", "TESTPROJ", "-S", "SUB_LABEL", "-E", "SESS001"]
            )

        assert result.exit_code == 0
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert "/subjects/SUB_LABEL/" in scan_url
        assert "XNAT_S00042" not in scan_url

    def test_no_project_keeps_the_flat_experiment_url(self, runner: CliRunner) -> None:
        """Without a project there is no subject segment to add.

        ``/data/experiments/{E}/scans`` routes correctly on its own, and XNAT
        rejects a subject segment outside a project.
        """
        ctx, mock_client = make_authenticated_context(default_project=None)
        mock_client.get_json.side_effect = [
            _exp_metadata(subject_id="XNAT_S00042"),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert scan_url == "/data/experiments/XNAT_E00001/scans"
        assert "/subjects/" not in scan_url

    def test_missing_subject_id_falls_back_to_the_flat_url(self, runner: CliRunner) -> None:
        """With no subject to insert, the URL drops the project rather than
        keeping the prefix XNAT refuses to route.
        """
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(subject_id=None),
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert scan_url == "/data/experiments/XNAT_E00001/scans"

    def test_items_response_subject_is_carried_forward(self, runner: CliRunner) -> None:
        """The subject is read from an `items[]` experiment document too."""
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_E00001",
                            "label": "SESS001",
                            "project": "TESTPROJ",
                            "subject_ID": "XNAT_S00042",
                        },
                        "meta": {"xsi:type": "xnat:mrSessionData"},
                        "children": [],
                    }
                ]
            },
            _scan_results([{"ID": "1", "type": "T1", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "SESS001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        scan_url = mock_client.get_json.call_args_list[1][0][0]
        assert "/subjects/XNAT_S00042/" in scan_url

    def test_unresolvable_label_refuses_rather_than_guessing(self, runner: CliRunner) -> None:
        """A label that inspection cannot resolve fails closed on every verb.

        The URL would stay on the prefix XNAT answers with the experiment
        document, so a listing would report no scans and a DELETE would target
        the session. `-E` here is label-shaped, so the flat form is not an
        option.
        """
        from xnatctl.core.exceptions import ResourceNotFoundError

        for argv in (
            ["scan", "list", "-P", "TESTPROJ", "-E", "SESS_LABEL"],
            ["scan", "show", "-P", "TESTPROJ", "-E", "SESS_LABEL", "1"],
            ["scan", "delete", "-P", "TESTPROJ", "-E", "SESS_LABEL", "--scans", "1", "-y"],
        ):
            ctx, mock_client = make_authenticated_context()
            mock_client.get_json.side_effect = ResourceNotFoundError("session", "SESS_LABEL")

            with authenticated_seams(ctx, mock_client):
                result = runner.invoke(cli, argv)

            assert result.exit_code != 0, argv
            assert "SESS_LABEL" in result.output
            # Nothing was issued against the unrouted URL.
            mock_client.delete.assert_not_called()
