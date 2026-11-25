from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from conftest import authenticated_seams, make_authenticated_context

from xnatctl.cli.main import cli
from xnatctl.core.exceptions import DownloadError, NetworkError
from xnatctl.models.progress import TransferSummary

runner = CliRunner()


def _resolved_experiment(client: Any) -> None:
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


def _session_download(tmp_path: Path, *extra: str) -> Any:
    return runner.invoke(
        cli,
        [
            "session",
            "download",
            "-E",
            "SESS001",
            "-P",
            "TESTPROJ",
            "--out",
            str(tmp_path),
            "-o",
            "json",
            *extra,
        ],
    )


class TestSessionDownloadFailureSummary:
    def test_a_raising_parallel_download_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, client = make_authenticated_context()
        _resolved_experiment(client)

        with (
            authenticated_seams(ctx, client),
            patch("xnatctl.cli.session.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_session_fast.side_effect = NetworkError(
                "https://xnat.example.org", "connection reset"
            )
            result = _session_download(tmp_path, "-w", "4")

        assert result.exit_code != 0
        summary = TransferSummary.model_validate(json.loads(result.stdout))
        assert summary.status == "failed"
        assert summary.scans == 0
        assert summary.files is None
        assert [(item.id, item.status) for item in summary.items] == [("session", "failed")]
        assert "connection reset" in (summary.items[0].error or "")

    def test_a_failed_extraction_after_an_archive_download_is_partial(self, tmp_path: Path) -> None:
        ctx, client = make_authenticated_context()
        _resolved_experiment(client)

        with (
            authenticated_seams(ctx, client),
            patch("xnatctl.cli.session.DownloadService"),
            patch(
                "xnatctl.cli.session.extract_session_zips",
                side_effect=DownloadError("corrupt archive", "scans.zip"),
            ),
        ):
            result = _session_download(tmp_path, "--extract")

        assert result.exit_code != 0
        summary = TransferSummary.model_validate(json.loads(result.stdout))
        assert summary.status == "partial"
        assert summary.scans is None
        assert [(item.id, item.status) for item in summary.items] == [
            ("archive", "success"),
            ("session", "failed"),
        ]


class TestScanDownloadFailureSummary:
    def test_a_rejected_download_emits_a_failed_summary(self, tmp_path: Path) -> None:
        ctx, client = make_authenticated_context()

        with (
            authenticated_seams(ctx, client),
            patch("xnatctl.services.downloads.DownloadService") as mock_dl_cls,
        ):
            mock_dl_cls.return_value.download_scans.side_effect = ValueError("no such scan")
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1,2",
                    "--out",
                    str(tmp_path),
                    "-o",
                    "json",
                ],
            )

        assert result.exit_code == 1
        summary = TransferSummary.model_validate(json.loads(result.stdout))
        assert summary.status == "failed"
        assert summary.scans == 2
        assert summary.files is None
        assert [(item.id, item.error) for item in summary.items] == [("1,2", "no such scan")]


class TestSubjectRenameFailurePaths:
    def test_a_rejected_rename_is_reported_as_skipped(self, tmp_path: Path) -> None:
        ctx, client = make_authenticated_context()
        svc_cls = MagicMock()
        svc_cls.return_value.list_rows.return_value = [{"ID": "S1", "label": "SUB001_v1"}]
        svc_cls.return_value.rename_raw.return_value = MagicMock(status_code=409)
        mapping = tmp_path / "mapping.json"
        mapping.write_text(json.dumps({"SUB001_v1": "SUB001"}))

        with authenticated_seams(ctx, client), patch("xnatctl.cli.subject.SubjectService", svc_cls):
            result = runner.invoke(
                cli, ["subject", "rename", "-P", "TESTPROJ", "--mapping", str(mapping)]
            )

        assert result.exit_code == 0
        assert "Skipped (1):\n  SUB001_v1: failed: 409" in result.stdout
        assert "Renamed: 0, Merged: 0, Skipped: 1" in result.output
        svc_cls.return_value.rename_raw.assert_called_once_with("TESTPROJ", "SUB001_v1", "SUB001")

    def test_a_failed_merge_reaches_only_the_diagnostics_file(self, tmp_path: Path) -> None:
        ctx, client = make_authenticated_context()
        svc_cls = MagicMock()
        svc_cls.return_value.list_rows.return_value = [
            {"ID": "S1", "label": "SUB001_v1"},
            {"ID": "S2", "label": "SUB001"},
        ]
        svc_cls.return_value.merge_subjects.side_effect = RuntimeError("reassignment not verified")
        log_file = tmp_path / "diag.jsonl"

        with authenticated_seams(ctx, client), patch("xnatctl.cli.subject.SubjectService", svc_cls):
            result = runner.invoke(
                cli,
                [
                    "subject",
                    "rename",
                    "-P",
                    "TESTPROJ",
                    "--pattern",
                    r"^(\w+)_v1$",
                    "--to",
                    "{1}",
                    "--log-file",
                    str(log_file),
                ],
            )

        assert result.exit_code == 0
        assert "SUB001_v1: merge failed: reassignment not verified" in result.stdout
        assert "Traceback" not in result.output
        records = [json.loads(line) for line in log_file.read_text().splitlines()]
        merge_records = [r for r in records if r.get("msg", "").startswith("Merging subject")]
        assert len(merge_records) == 1
        assert "reassignment not verified" in merge_records[0]["exc"]
