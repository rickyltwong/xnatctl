from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import authenticated_seams, make_authenticated_context

from xnatctl.cli.main import cli
from xnatctl.core.exceptions import ServerError
from xnatctl.models.progress import DownloadSummary, VerificationReport


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _exp_metadata(
    xsi_type: str = "xnat:mrSessionData",
    *,
    experiment_id: str | None = "XNAT_E00001",
    subject_id: str | None = "XNAT_S00001",
) -> dict[str, Any]:
    row: dict[str, Any] = {"xsiType": xsi_type}
    if experiment_id:
        row["ID"] = experiment_id
    if subject_id:
        row["subject_ID"] = subject_id
    return {"ResultSet": {"Result": [row]}}


def _scan_results(scans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ResultSet": {"Result": scans or []}}


class TestScanList:
    def test_scan_list_happy_path(self, runner: CliRunner) -> None:
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

    def test_scan_list_filter_and_sort(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results(
                [
                    {"ID": "1", "type": "T1w", "series_description": "", "quality": "unusable"},
                    {"ID": "3", "type": "T3w", "series_description": "", "quality": "usable"},
                    {"ID": "4", "type": "T4w", "series_description": "", "quality": "usable"},
                    {"ID": "2", "type": "T2w", "series_description": "", "quality": "usable"},
                ]
            ),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "list",
                    "-E",
                    "XNAT_E00001",
                    "--filter",
                    "quality:usable",
                    "--sort-by",
                    "id",
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert [r["id"] for r in rows] == ["2", "3", "4"]

    def test_scan_list_with_project(self, runner: CliRunner) -> None:
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
        from xnatctl.core.exceptions import NetworkError, ResourceNotFoundError

        ctx, mock_client = make_authenticated_context(default_project=None)
        mock_client.get_json.side_effect = [
            ResourceNotFoundError("session", "XNAT_E00001"),
            _scan_results(),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert mock_client.get_json.call_count == 2

        ctx2, mock_client2 = make_authenticated_context(default_project=None)
        mock_client2.get_json.side_effect = NetworkError("upstream timeout")

        with authenticated_seams(ctx2, mock_client2):
            result2 = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result2.exit_code != 0
        assert mock_client2.get_json.call_count == 1

    def test_scan_list_default_project_fallback(self, runner: CliRunner) -> None:
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
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata(),
            _scan_results(),
            _scan_results(),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0

    def test_scan_list_opt_session_lists_other_dicom_scans(self, runner: CliRunner) -> None:
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
        assert mock_client.get_json.call_count == 2
        scan_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in scan_call_kwargs.get("params", {})

    def test_scan_list_mr_session_lists_unfiltered(self, runner: CliRunner) -> None:
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
        assert mock_client.get_json.call_count == 2
        scan_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in scan_call_kwargs.get("params", {})

    def test_scan_list_eeg_session_falls_back_to_xsi_type(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.side_effect = [
            _exp_metadata("xnat:eegSessionData"),
            _scan_results(),
            _scan_results([{"ID": "1", "type": "EEG", "series_description": "", "quality": ""}]),
        ]

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "list", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        assert "EEG" in result.output
        primary_call_kwargs = mock_client.get_json.call_args_list[1][1]
        assert "xsiType" not in primary_call_kwargs.get("params", {})
        fallback_call_kwargs = mock_client.get_json.call_args_list[2][1]
        assert fallback_call_kwargs["params"]["xsiType"] == "xnat:eegScanData"


class TestScanShow:
    def test_scan_show_happy_path(self, runner: CliRunner) -> None:
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

    def test_scan_show_resources_listing_failure_is_not_silent(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()

        def _get_json_side(url: str, **kwargs: Any) -> dict[str, Any]:
            if url.endswith("/resources"):
                raise ServerError(500, "GET", url)
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
        assert "Warning: could not list resources" in result.output

    def test_scan_show_not_found(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "show", "-E", "XNAT_E00001", "999"])

        assert result.exit_code == 1


class TestScanDelete:
    def test_scan_delete_dry_run(self, runner: CliRunner) -> None:
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
        assert "/data/projects/TESTPROJ/experiments/" not in delete_url

    def test_scan_delete_failure(self, runner: CliRunner) -> None:
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
                    "--workers",
                    "1",
                ],
            )

        assert result.exit_code == 1
        assert "Deleted 1 scan" in result.output


class TestScanDeleteBatch:
    def test_batch_stdin_deletes_each_scan(self, runner: CliRunner) -> None:
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
                    "--batch",
                    "-",
                    "-y",
                ],
                input="1\n2\n",
            )

        assert result.exit_code == 0
        assert mock_client.delete.call_count == 2

    def test_batch_json_array_file(self, runner: CliRunner, tmp_path: Any) -> None:
        batch_file = tmp_path / "scans.json"
        batch_file.write_text('["1", "2"]')
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
                    "--batch",
                    str(batch_file),
                    "-y",
                ],
            )

        assert result.exit_code == 0
        assert mock_client.delete.call_count == 2

    def test_batch_dry_run_lists_scans_without_deleting(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        batch_file = tmp_path / "scans.txt"
        batch_file.write_text("1\n2\n")
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "delete",
                    "-E",
                    "XNAT_E00001",
                    "--batch",
                    str(batch_file),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        assert "2 scans" in result.output
        mock_client.delete.assert_not_called()

    def test_neither_scans_nor_batch_is_usage_error(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(cli, ["scan", "delete", "-E", "XNAT_E00001", "-y"])

        assert result.exit_code != 0
        assert "provide --scans or --batch" in result.output

    def test_both_scans_and_batch_is_usage_error(self, runner: CliRunner, tmp_path: Any) -> None:
        batch_file = tmp_path / "scans.txt"
        batch_file.write_text("2\n")
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
                    "1",
                    "--batch",
                    str(batch_file),
                    "-y",
                ],
            )

        assert result.exit_code != 0
        assert "not both" in result.output

    def test_batch_dash_without_yes_or_dry_run_is_usage_error(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                ["scan", "delete", "-E", "XNAT_E00001", "--batch", "-"],
                input="1\n",
            )

        assert result.exit_code != 0
        assert "requires --yes or --dry-run" in result.output
        mock_client.delete.assert_not_called()

    def test_batch_empty_file_does_not_fall_through_to_wildcard(
        self, runner: CliRunner, tmp_path: Any
    ) -> None:
        batch_file = tmp_path / "empty.txt"
        batch_file.write_text("")
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
                    "*",
                    "--batch",
                    str(batch_file),
                    "-y",
                ],
            )

        assert result.exit_code != 0
        mock_client.delete.assert_not_called()
        mock_client.get_json.assert_not_called()


class TestScanDownload:
    def test_scan_download_dry_run(self, runner: CliRunner, tmp_path) -> None:
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
        assert "Verification failed" in result.stderr
        assert "Download failed" not in result.stderr

    def test_scan_download_verify_all_unverifiable_fails(self, runner: CliRunner, tmp_path) -> None:
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
        assert payload["status"] == "failed"

    def test_scan_download_json_output(self, runner: CliRunner, tmp_path) -> None:
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


class TestScanHelp:
    def test_scan_list_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["scan", "list", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--project" in result.output

    def test_scan_show_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["scan", "show", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "SCAN_ID" in result.output

    def test_scan_delete_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["scan", "delete", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--dry-run" in result.output
        assert "--yes" in result.output

    def test_scan_download_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["scan", "download", "--help"])
        assert result.exit_code == 0
        assert "--experiment" in result.output
        assert "--scans" in result.output
        assert "--out" in result.output
        assert "--dry-run" in result.output
        assert "--resource" in result.output
        assert "--extract" in result.output


class TestScanNestedUrlRouting:
    def test_project_scoped_listing_goes_through_the_subject(self, runner: CliRunner) -> None:
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
            mock_client.delete.assert_not_called()
