"""Tests for xnatctl CLI subject commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import AuthenticatedCLI, config_seam, core_config_seam, make_response

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ResourceNotFoundError, ServerError


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _mock_config() -> Config:
    """Build a mock Config with a default profile including default_project."""
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="testuser",
                password="testpass",
                verify_ssl=False,
                default_project="TESTPROJ",
            )
        },
    )


def _mock_client() -> MagicMock:
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}
    return client


class TestSubjectList:
    """Tests for subject list command."""

    def test_subject_list_with_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                        {"ID": "XNAT_S002", "label": "SUB002", "src": ""},
                    ]
                }
            },
            {"ResultSet": {"Result": [{"ID": "EXP1"}]}},
            {"ResultSet": {"Result": [{"ID": "EXP2"}, {"ID": "EXP3"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "list", "--project", "TESTPROJ"])

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_list_uses_default_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001", "src": ""}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "list"])

        assert result.exit_code == 0

    def test_subject_list_no_project_no_default(self, runner: CliRunner) -> None:
        client = _mock_client()
        cfg = Config(
            default_profile="default",
            profiles={
                "default": Profile(
                    url="https://xnat.example.org",
                    username="testuser",
                    password="testpass",
                    verify_ssl=False,
                )
            },
        )

        with core_config_seam(cfg):
            with config_seam(cfg):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "list"])

        assert result.exit_code != 0
        assert "Project required" in result.output or "Project required" in result.stderr

    def test_subject_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                    {"ID": "XNAT_S002", "label": "SUB002", "src": ""},
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "list", "-P", "TESTPROJ", "--quiet"])

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_list_with_filter(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                    {"ID": "XNAT_S002", "label": "CTL001", "src": ""},
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "list",
                            "-P",
                            "TESTPROJ",
                            "--filter",
                            "label:SUB*",
                            "--quiet",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output
        assert "CTL001" not in result.output

    def test_subject_list_sort_by_sessions_sees_the_enriched_field(self, runner: CliRunner) -> None:
        """--sort-by sessions must see the count AFTER enrichment.

        Running list controls BEFORE the per-subject session count is
        added to each row would make sorting/filtering by "sessions" a
        silent no-op (the field wouldn't exist yet). SUB001 has fewer
        sessions than SUB002 but sorts first in the raw API order, so a
        correct descending sort must reverse that order.
        """
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "XNAT_S001", "label": "SUB001", "src": ""},
                        {"ID": "XNAT_S002", "label": "SUB002", "src": ""},
                    ]
                }
            },
            {"ResultSet": {"Result": [{"ID": "E1"}, {"ID": "E2"}]}},
            {
                "ResultSet": {
                    "Result": [{"ID": f"E{i}"} for i in range(5)],
                }
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "list",
                            "-P",
                            "TESTPROJ",
                            "--sort-by",
                            "sessions:desc",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert [r["label"] for r in rows] == ["SUB002", "SUB001"]
        assert [r["sessions"] for r in rows] == [5, 2]

    def test_subject_list_narrow_filter_on_large_project_still_gets_session_counts(
        self, runner: CliRunner
    ) -> None:
        """A --filter on label narrows BEFORE the 50-subject enrichment cap
        is checked, so a big project with a narrow filter still gets the
        sessions column for its (small) surviving set.

        An enrichment gate keyed on the project's raw, pre-filter subject
        count -- 60 subjects here -- would skip enrichment outright even
        though only 10 subjects actually survive --filter.
        """
        client = _mock_client()
        base_rows = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_S{i:03d}", "label": f"SUB{i:03d}", "src": ""} for i in range(60)
                ]
            }
        }

        def get_json_side_effect(path: str, **kwargs: object) -> dict:
            if "/experiments" in path:
                return {"ResultSet": {"Result": [{"ID": "E1"}]}}
            return base_rows

        client.get_json.side_effect = get_json_side_effect

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "list",
                            "-P",
                            "TESTPROJ",
                            "--filter",
                            "label:SUB00*",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert len(rows) == 10  # SUB000..SUB009
        assert all(r["sessions"] == 1 for r in rows)

    def test_subject_list_filter_by_sessions_on_large_project_raises_clear_error(
        self, runner: CliRunner
    ) -> None:
        """--filter/--sort-by 'sessions' has nothing to narrow the working
        set by before enrichment, so on a project too large to enrich
        outright it must raise a clear error rather than silently produce a
        blank "sessions" column or an opaque "Unknown filter field" error.
        """
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_S{i:03d}", "label": f"SUB{i:03d}", "src": ""} for i in range(60)
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "list", "-P", "TESTPROJ", "--filter", "sessions:5"],
                    )

        assert result.exit_code != 0
        assert "50 or fewer" in result.output

    def test_subject_list_large_project_drops_sessions_column_with_note(
        self, runner: CliRunner
    ) -> None:
        """When enrichment is skipped for size, the "sessions" column must
        be DROPPED (not rendered as silent blanks), and a one-line stderr
        note must explain why.
        """
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_S{i:03d}", "label": f"SUB{i:03d}", "src": ""} for i in range(60)
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "list", "-P", "TESTPROJ", "-o", "json"])

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert len(rows) == 60
        assert all("sessions" not in r for r in rows)
        assert "session counts omitted" in result.output
        assert "60" in result.output

    def test_subject_list_mixed_label_filter_and_sessions_sort_narrows_first(
        self, runner: CliRunner
    ) -> None:
        """--filter on label must narrow the working set BEFORE the
        enrichment cap is checked even when --sort-by (a different option)
        targets "sessions" -- the presence of a sessions-targeting control
        anywhere must not suppress narrowing by a control that doesn't need
        it.

        A sessions-targeting --sort-by that skips the --filter narrowing
        pass would make this exact combination (60 subjects, filter
        narrows to 10) raise the same "too many subjects" error as
        filtering by "sessions" itself would.
        """
        client = _mock_client()
        base_rows = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_S{i:03d}", "label": f"SUB{i:03d}", "src": ""} for i in range(60)
                ]
            }
        }

        def get_json_side_effect(path: str, **kwargs: object) -> dict:
            if "/experiments" in path:
                return {"ResultSet": {"Result": [{"ID": "E1"}]}}
            return base_rows

        client.get_json.side_effect = get_json_side_effect

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "list",
                            "-P",
                            "TESTPROJ",
                            "--filter",
                            "label:SUB00*",
                            "--sort-by",
                            "sessions",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert len(rows) == 10
        assert all(r["sessions"] == 1 for r in rows)


class TestSubjectShow:
    """Tests for subject show command."""

    def test_subject_show_items_response(self, runner: CliRunner) -> None:
        """Subject show handles `items[]` detail responses."""
        client = _mock_client()
        client.get_json.side_effect = [
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_S001",
                            "label": "SUB001",
                            "project": "TESTPROJ",
                        }
                    }
                ]
            },
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "EXP1", "label": "SESSION1"},
                    ]
                }
            },
            # `subject show` now also lists the projects the subject is
            # shared into: GET /data/subjects/{id}/projects. Only the primary
            # project comes back here, so nothing renders as a share.
            {"ResultSet": {"Result": [{"ID": "TESTPROJ", "label": "SUB001"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "show", "SUB001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_show(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = [
            {"ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}},
            {
                "ResultSet": {
                    "Result": [
                        {"ID": "EXP1", "label": "SESSION1"},
                    ]
                }
            },
            # `subject show` now also lists the projects the subject is
            # shared into: GET /data/subjects/{id}/projects. Only the primary
            # project comes back here, so nothing renders as a share.
            {"ResultSet": {"Result": [{"ID": "TESTPROJ", "label": "SUB001"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "show", "SUB001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "SUB001" in result.output

    def test_subject_show_sessions_listing_failure_is_not_silent(self, runner: CliRunner) -> None:
        """A session-listing failure must be visible, not indistinguishable from "no sessions".

        A bare ``except Exception: session_labels = []`` around the
        session-count fetch would render a transient 500 identically --
        silently -- to a subject that genuinely has no sessions.
        """
        client = _mock_client()
        client.get_json.side_effect = [
            {"ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}},
            ServerError(500, "GET", "/data/projects/TESTPROJ/subjects/XNAT_S001/experiments"),
            # The shared-projects listing that follows must still be attempted:
            # one failed sub-fetch must not cascade into skipping the next.
            {"ResultSet": {"Result": [{"ID": "TESTPROJ", "label": "SUB001"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "show", "SUB001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "Warning: could not list sessions" in result.output

    def test_subject_show_not_found(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "show", "NOSUB", "-P", "TESTPROJ"])

        assert result.exit_code != 0


class TestSubjectDelete:
    """Tests for subject delete command."""

    def test_subject_delete_dry_run(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "Would delete" in result.output
        client.delete.assert_not_called()

    def test_subject_delete_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.delete.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        assert "Deleted" in result.output
        client.delete.assert_called_once()

    def test_subject_delete_failure(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server error"
        client.delete.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--yes",
                        ],
                    )

        assert result.exit_code != 0

    def test_subject_delete_prompt_abort_no_mutation(self, runner: CliRunner) -> None:
        """Declining the confirmation prompt must not call the client."""
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "SUB001", "-P", "TESTPROJ"],
                        input="n\n",
                    )

        assert result.exit_code != 0
        client.delete.assert_not_called()

    def test_subject_delete_raises_service_error(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.subject.SubjectService.delete_raw",
                        side_effect=ResourceNotFoundError("Subject", "SUB001"),
                    ):
                        result = runner.invoke(
                            cli,
                            ["subject", "delete", "SUB001", "-P", "TESTPROJ", "--yes"],
                        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestSubjectDeleteBatch:
    """Tests for `subject delete --batch`."""

    def test_batch_stdin_deletes_each_id(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.delete.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "-P", "TESTPROJ", "--batch", "-", "--yes"],
                        input="SUB001\nSUB002\n",
                    )

        assert result.exit_code == 0
        assert client.delete.call_count == 2
        urls = [c.args[0] for c in client.delete.call_args_list]
        assert any("SUB001" in u for u in urls)
        assert any("SUB002" in u for u in urls)

    def test_batch_json_array_file(self, runner: CliRunner, tmp_path: Path) -> None:
        batch_file = tmp_path / "ids.json"
        batch_file.write_text('["SUB001", "SUB002"]')
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.delete.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "-P",
                            "TESTPROJ",
                            "--batch",
                            str(batch_file),
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        assert client.delete.call_count == 2

    def test_batch_dry_run_lists_each_id_without_deleting(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        batch_file = tmp_path / "ids.txt"
        batch_file.write_text("SUB001\nSUB002\n")
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "-P",
                            "TESTPROJ",
                            "--batch",
                            str(batch_file),
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001" in result.output
        assert "SUB002" in result.output
        client.delete.assert_not_called()

    def test_batch_partial_failure_exits_nonzero(self, runner: CliRunner) -> None:
        client = _mock_client()
        ok = MagicMock(status_code=200)
        bad = MagicMock(status_code=500, text="server error")
        client.delete.side_effect = [ok, bad]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "-P", "TESTPROJ", "--batch", "-", "--yes"],
                        input="SUB001\nSUB002\n",
                    )

        assert result.exit_code != 0
        assert client.delete.call_count == 2

    def test_batch_continues_past_a_raising_id(self, runner: CliRunner) -> None:
        """A failing ID mid-batch must not abandon the IDs after it.

        The client layer returns 2xx only and raises a typed exception for
        every error status, so a 404 on the second of three subjects arrives
        as an exception, not as a response to inspect. If that is not caught
        per subject, the third ID is silently never attempted -- the caller
        sees one success, one error, and no word at all about the rest of
        the list they handed in.

        A single-subject delete deliberately does NOT swallow the exception:
        with no rest of the list to protect, the real error class is worth
        more than a failure count, both on stderr and in the audit record.
        """
        client = _mock_client()
        ok = MagicMock(status_code=200)
        client.delete.side_effect = [
            ok,
            ResourceNotFoundError("subject", "SUB002"),
            ok,
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "-P", "TESTPROJ", "--batch", "-", "--yes"],
                        input="SUB001\nSUB002\nSUB003\n",
                    )

        assert client.delete.call_count == 3
        assert result.exit_code != 0
        assert "SUB002" in result.output
        urls = [c.args[0] for c in client.delete.call_args_list]
        assert any("SUB003" in u for u in urls)

    def test_neither_subject_id_nor_batch_is_usage_error(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=_mock_client()):
                    result = runner.invoke(cli, ["subject", "delete", "-P", "TESTPROJ", "--yes"])

        assert result.exit_code != 0
        assert "provide SUBJECT_ID or --batch" in result.output

    def test_both_subject_id_and_batch_is_usage_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        batch_file = tmp_path / "ids.txt"
        batch_file.write_text("SUB002\n")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=_mock_client()):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--batch",
                            str(batch_file),
                            "--yes",
                        ],
                    )

        assert result.exit_code != 0
        assert "not both" in result.output

    def test_batch_dash_empty_stdin_does_not_delete_positional_subject(
        self, runner: CliRunner
    ) -> None:
        """--batch - with empty stdin must not read as "no --batch given"
        and fall through to deleting the positional SUBJECT_ID -- --batch
        was given, so this is mutual-exclusion territory, and an empty batch
        is its own clear error either way. Either reading must leave SUB001
        undeleted.
        """
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "delete",
                            "SUB001",
                            "-P",
                            "TESTPROJ",
                            "--batch",
                            "-",
                            "--yes",
                        ],
                        input="",
                    )

        assert result.exit_code != 0
        client.delete.assert_not_called()

    def test_batch_validates_all_ids_before_deleting_any(self, runner: CliRunner) -> None:
        """A malformed ID anywhere in the batch must abort the whole command
        BEFORE any subject is deleted -- not after the earlier, valid IDs in
        the same batch have already been removed.
        """
        client = _mock_client()
        mock_resp = MagicMock(status_code=200)
        client.delete.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "-P", "TESTPROJ", "--batch", "-", "--yes"],
                        input="SUB001\nbad/id\n",
                    )

        assert result.exit_code != 0
        client.delete.assert_not_called()

    def test_batch_dash_without_yes_or_dry_run_is_usage_error(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "delete", "-P", "TESTPROJ", "--batch", "-"],
                        input="SUB001\n",
                    )

        assert result.exit_code != 0
        assert "requires --yes or --dry-run" in result.output
        client.delete.assert_not_called()


class TestSubjectRename:
    """Tests for subject rename command."""

    def test_rename_requires_a_method(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "rename", "-P", "TESTPROJ"])

        assert result.exit_code != 0
        assert "Must provide" in result.output

    def test_rename_rejects_multiple_methods(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps({"OLD": "NEW"}))
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(json.dumps({"patterns": []}))

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--patterns-file",
                            str(patterns_file),
                            "--mapping",
                            str(mapping_file),
                        ],
                    )

        assert result.exit_code != 0
        assert "only one of" in result.output.lower()
        client.put.assert_not_called()

    def test_rename_mapping_dry_run_no_mutation(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps({"SUB001": "SUB001_new"}))
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--mapping",
                            str(mapping_file),
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001 -> SUB001_new" in result.output
        client.put.assert_not_called()

    def test_rename_mapping_applies_rename(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps({"SUB001": "SUB001_new"}))
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--mapping",
                            str(mapping_file),
                        ],
                    )

        assert result.exit_code == 0
        assert "Renamed: 1" in result.output
        client.put.assert_called_once()

    def test_rename_mapping_merges_into_existing_target(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps({"SUB001": "SUB002"}))
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001"},
                    {"ID": "XNAT_S002", "label": "SUB002"},
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.subject.SubjectService.merge_subjects",
                        return_value={"experiments_moved": 3, "source_deleted": True},
                    ) as mock_merge:
                        result = runner.invoke(
                            cli,
                            [
                                "subject",
                                "rename",
                                "-P",
                                "TESTPROJ",
                                "--mapping",
                                str(mapping_file),
                            ],
                        )

        assert result.exit_code == 0
        assert "Merged: 1" in result.output
        mock_merge.assert_called_once_with(
            project="TESTPROJ", source_label="SUB001", target_label="SUB002", dry_run=False
        )

    def test_rename_mapping_skips_missing_and_same_label(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps({"NOSUCH": "TARGET", "SUB001": "SUB001"}))
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--mapping",
                            str(mapping_file),
                        ],
                    )

        assert result.exit_code == 0
        assert "Skipped (2)" in result.output
        assert "not found" in result.output
        assert "same label" in result.output

    def test_rename_pattern_to_template(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001_visit1"}]}
        }
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--pattern",
                            r"^(\w+)_visit\d+$",
                            "--to",
                            "{1}",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001_visit1 -> SUB001" in result.output

    def test_rename_pattern_rejects_invalid_target_label(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--pattern",
                            r"^(.+)$",
                            # A target with a path separator is rejected as an
                            # illegal XNAT label.
                            "--to",
                            "bad/{1}",
                        ],
                    )

        assert result.exit_code == 0
        assert "invalid target label" in result.output
        client.put.assert_not_called()

    def test_rename_patterns_file_dry_run(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps(
                {
                    "patterns": [
                        {
                            "project": "TESTPROJ",
                            "match": r"^(\w+)_visit\d+$",
                            "to": "{1}",
                            "description": "collapse visits",
                        }
                    ]
                }
            )
        )
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001_visit1"}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--patterns-file",
                            str(patterns_file),
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001_visit1 -> SUB001" in result.output
        assert "collapse visits" in result.output
        client.put.assert_not_called()

    def test_rename_patterns_file_infers_single_project(self, runner: CliRunner, tmp_path) -> None:
        """With no -P and no profile default, a single-project patterns file is used as-is."""
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps(
                {"patterns": [{"project": "OTHERPROJ", "match": r"^(\w+)$", "to": "{1}_renamed"}]}
            )
        )
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }
        cfg = Config(
            default_profile="default",
            profiles={
                "default": Profile(
                    url="https://xnat.example.org",
                    username="testuser",
                    password="testpass",
                    verify_ssl=False,
                )
            },
        )

        with core_config_seam(cfg):
            with config_seam(cfg):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "--patterns-file",
                            str(patterns_file),
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "SUB001 -> SUB001_renamed" in result.output

    def test_rename_patterns_file_ambiguous_projects_errors(
        self, runner: CliRunner, tmp_path
    ) -> None:
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps(
                {
                    "patterns": [
                        {"project": "PROJA", "match": r"^(\w+)$", "to": "{1}_x"},
                        {"project": "PROJB", "match": r"^(\w+)$", "to": "{1}_y"},
                    ]
                }
            )
        )
        cfg = Config(
            default_profile="default",
            profiles={
                "default": Profile(
                    url="https://xnat.example.org",
                    username="testuser",
                    password="testpass",
                    verify_ssl=False,
                )
            },
        )

        with core_config_seam(cfg):
            with config_seam(cfg):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["subject", "rename", "--patterns-file", str(patterns_file)],
                    )

        assert result.exit_code != 0
        assert "multiple projects" in result.output.lower()

    def test_rename_patterns_file_bad_json_errors(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text("not json")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--patterns-file",
                            str(patterns_file),
                        ],
                    )

        assert result.exit_code != 0
        assert "Failed to load patterns file" in result.output

    def test_rename_patterns_file_no_match_for_project_errors(
        self, runner: CliRunner, tmp_path
    ) -> None:
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps({"patterns": [{"project": "OTHER", "match": "^x$", "to": "y"}]})
        )
        client.get_json.return_value = {
            "ResultSet": {"Result": [{"ID": "XNAT_S001", "label": "SUB001"}]}
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "subject",
                            "rename",
                            "-P",
                            "TESTPROJ",
                            "--patterns-file",
                            str(patterns_file),
                        ],
                    )

        assert result.exit_code != 0
        assert "no patterns found" in result.output.lower()

    def test_rename_patterns_file_applies_merge_and_rename(
        self, runner: CliRunner, tmp_path
    ) -> None:
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps(
                {
                    "patterns": [
                        {"project": "TESTPROJ", "match": r"^(\w+)_visit\d+$", "to": "{1}"},
                    ]
                }
            )
        )
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001_visit1"},
                    {"ID": "XNAT_S002", "label": "SUB002"},
                    {"ID": "XNAT_S003", "label": "SUB003_visit1"},
                ]
            }
        }
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.subject.SubjectService.merge_subjects",
                        return_value={"experiments_moved": 2, "source_deleted": True},
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "subject",
                                "rename",
                                "-P",
                                "TESTPROJ",
                                "--patterns-file",
                                str(patterns_file),
                            ],
                        )

        # SUB001_visit1 merges into existing SUB001? No SUB001 exists here, so
        # it renames; SUB002 does not match the pattern; SUB003_visit1 renames.
        assert result.exit_code == 0
        assert "Renamed: 2" in result.output

    def test_rename_patterns_file_merge_failure_is_skipped(
        self, runner: CliRunner, tmp_path
    ) -> None:
        client = _mock_client()
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(
            json.dumps(
                {
                    "patterns": [
                        {"project": "TESTPROJ", "match": r"^(\w+)_visit\d+$", "to": "{1}"},
                    ]
                }
            )
        )
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_S001", "label": "SUB001_visit1"},
                    {"ID": "XNAT_S002", "label": "SUB001"},
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.subject.SubjectService.merge_subjects",
                        side_effect=RuntimeError("boom"),
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "subject",
                                "rename",
                                "-P",
                                "TESTPROJ",
                                "--patterns-file",
                                str(patterns_file),
                            ],
                        )

        assert result.exit_code == 0
        assert "merge failed: boom" in result.output


def _resolved_subject_document(
    subject_id: str = "XNAT_S00001", label: str = "SUB001", project: str = "TESTPROJ"
) -> dict:
    """A minimal format=json document HierarchyService.resolve_subject can parse."""
    return {
        "items": [
            {
                "data_fields": {"ID": subject_id, "label": label, "project": project},
                "meta": {"xsi:type": "xnat:subjectData"},
                "children": [],
            }
        ]
    }


class TestSubjectShare:
    """Tests for `subject share`."""

    def test_share_puts_flat_accession_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_subject_document()
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["subject", "share", "SUB001", "--into", "OTHERPROJ", "--label", "SUB_SHARED", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/subjects/XNAT_S00001/projects/OTHERPROJ", params={"label": "SUB_SHARED"}
        )

    def test_share_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["subject", "share", "SUB001", "--into", "OTHERPROJ"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_share_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_subject_document()

        result = authenticated_cli.invoke(
            ["subject", "share", "SUB001", "--into", "OTHERPROJ", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_share_not_found(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("subject", "GONE")

        result = authenticated_cli.invoke(
            ["subject", "share", "GONE", "--into", "OTHERPROJ", "--yes"]
        )

        assert result.exit_code != 0
        assert "Subject not found: GONE" in result.output

    def test_share_rejects_invalid_label(self, authenticated_cli: AuthenticatedCLI) -> None:
        """A --label containing a path/URL-reserved character fails locally, not on the wire."""
        result = authenticated_cli.invoke(
            ["subject", "share", "SUB001", "--into", "OTHERPROJ", "--label", "bad%label", "--yes"]
        )

        assert result.exit_code != 0
        assert "Invalid subject label" in result.output
        authenticated_cli.client.put.assert_not_called()


class TestSubjectUnshare:
    """Tests for `subject unshare`."""

    def test_unshare_deletes_flat_accession_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_subject_document()
        authenticated_cli.client.delete.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["subject", "unshare", "SUB001", "--from", "OTHERPROJ", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with(
            "/data/subjects/XNAT_S00001/projects/OTHERPROJ"
        )

    def test_unshare_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = _resolved_subject_document()

        result = authenticated_cli.invoke(
            ["subject", "unshare", "SUB001", "--from", "OTHERPROJ", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_not_called()

    def test_unshare_from_primary_project_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Real execution refuses unsharing FROM the subject's own primary project."""
        authenticated_cli.client.get_json.return_value = _resolved_subject_document(
            project="TESTPROJ"
        )

        result = authenticated_cli.invoke(
            ["subject", "unshare", "SUB001", "--from", "TESTPROJ", "--yes"]
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
        (because XNAT would delete the subject outright).
        """
        authenticated_cli.client.get_json.return_value = _resolved_subject_document(
            project="TESTPROJ"
        )

        result = authenticated_cli.invoke(
            ["subject", "unshare", "SUB001", "--from", "TESTPROJ", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "primary project" in result.output
        assert "Would remove" not in result.output
        authenticated_cli.client.delete.assert_not_called()

    def test_unshare_from_primary_project_refused_with_padded_target(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Padded ``--from`` input must not slip past the primary-project guard."""
        authenticated_cli.client.get_json.return_value = _resolved_subject_document(
            project="TESTPROJ"
        )

        result = authenticated_cli.invoke(
            ["subject", "unshare", "SUB001", "--from", " TESTPROJ ", "--yes"]
        )

        assert result.exit_code != 0
        assert "primary project" in result.output
        authenticated_cli.client.delete.assert_not_called()


class TestSubjectVars:
    """Tests for `subject vars` (read)."""

    def _fields_document(self, fields: list[tuple[str, str]]) -> dict:
        return {
            "items": [
                {
                    "data_fields": {"ID": "XNAT_S00001", "label": "SUB001"},
                    "meta": {"xsi:type": "xnat:subjectData"},
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
        authenticated_cli.client.get.return_value = make_response(
            self._fields_document([("studytag", "phase1")])
        )

        result = authenticated_cli.invoke(["subject", "vars", "SUB001"])

        assert result.exit_code == 0
        assert "studytag" in result.output
        assert "phase1" in result.output
        authenticated_cli.client.get.assert_called_once_with(
            "/data/projects/TESTPROJ/subjects/SUB001", params={"format": "json"}
        )

    def test_vars_json(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            self._fields_document([("studytag", "phase1")])
        )

        result = authenticated_cli.invoke(["subject", "vars", "SUB001", "-o", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == [{"name": "studytag", "value": "phase1"}]

    def test_vars_quiet_emits_names(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(
            self._fields_document([("studytag", "phase1"), ("cohort", "A")])
        )

        result = authenticated_cli.invoke(["subject", "vars", "SUB001", "-q"])

        assert result.exit_code == 0
        assert result.output.strip().splitlines() == ["studytag", "cohort"]

    def test_vars_not_found(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("subject", "GONE")

        result = authenticated_cli.invoke(["subject", "vars", "GONE"])

        assert result.exit_code != 0
        assert "Subject not found: GONE" in result.output


class TestSubjectVarsSet:
    """Tests for `subject vars set`."""

    def _subject_document(self) -> dict:
        """Minimal ``format=json`` document satisfying the existence preflight."""
        return {"items": [{"data_fields": {"ID": "XNAT_S00001", "label": "SUB001"}, "meta": {}}]}

    def test_set_single_pair(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(self._subject_document())
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "studytag=phase1", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/data/projects/TESTPROJ/subjects/SUB001",
            params={"xnat:subjectData/fields/field[name=studytag]/field": "phase1"},
        )

    def test_set_multiple_pairs(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get.return_value = make_response(self._subject_document())
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            [
                "subject",
                "vars",
                "set",
                "SUB001",
                "studytag=phase1",
                "cohort=A",
                "--yes",
            ]
        )

        assert result.exit_code == 0
        put_params = authenticated_cli.client.put.call_args[1]["params"]
        assert put_params == {
            "xnat:subjectData/fields/field[name=studytag]/field": "phase1",
            "xnat:subjectData/fields/field[name=cohort]/field": "A",
        }

    def test_set_no_pairs_is_usage_error(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["subject", "vars", "set", "SUB001", "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_invalid_pair_is_usage_error(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "not-a-pair", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "studytag=phase1"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_dry_run_confirms_subject_exists_no_put(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run runs the same existence preflight execution does, and skips only the PUT.

        This replaces a prior assertion that dry-run makes NO HTTP call at
        all -- that was the bug: dry-run "approved" a typo'd subject ID
        because it never checked whether the subject existed.
        """
        authenticated_cli.client.get.return_value = make_response(self._subject_document())

        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "studytag=phase1", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.get.assert_called_once_with(
            "/data/projects/TESTPROJ/subjects/SUB001", params={"format": "json"}
        )
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_set_dry_run_nonexistent_subject_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """`--dry-run` must refuse a subject it can't find, matching execution.

        A dry-run that makes no HTTP call at all would report "Would set"
        for a subject that doesn't exist -- see
        test_set_execute_nonexistent_subject_refused below for what
        execution does with the same typo.
        """
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("subject", "GONE")

        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "GONE", "studytag=phase1", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "Would set" not in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_set_execute_nonexistent_subject_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Execution must refuse a nonexistent subject, not silently create it.

        Regression test: ``SubjectService.set_vars`` PUTs to the same
        create-or-update route ``SubjectService.create`` uses, and XNAT
        answers a nonexistent subject with 201, not 404 (verified live
        against 1.9.2.1) -- an unguarded typo'd subject ID would succeed
        and create an empty phantom subject in the project.
        """
        authenticated_cli.client.get.side_effect = ResourceNotFoundError("subject", "GONE")

        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "GONE", "studytag=phase1", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_duplicate_key_is_usage_error(self, authenticated_cli: AuthenticatedCLI) -> None:
        """Silently keeping only the last value would send less than the user asked for."""
        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "studytag=one", "studytag=two", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_dry_run_rejects_invalid_field_name(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run must reject what execution rejects, not report a false success."""
        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "a/b=value", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "custom variable name" in result.output
        authenticated_cli.client.put.assert_not_called()

    def test_set_subject_id_and_batch_mutually_exclusive(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "SUB001", "--batch", "-", "studytag=phase1", "--yes"],
            input="SUB002\n",
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_set_batch_applies_to_every_id(
        self, authenticated_cli: AuthenticatedCLI, tmp_path: Path
    ) -> None:
        batch_file = tmp_path / "ids.txt"
        batch_file.write_text("SUB001\nSUB002\n")
        authenticated_cli.client.get.return_value = make_response(self._subject_document())
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["subject", "vars", "set", "--batch", str(batch_file), "studytag=phase1", "--yes"]
        )

        assert result.exit_code == 0
        assert authenticated_cli.client.put.call_count == 2
        called_paths = {call.args[0] for call in authenticated_cli.client.put.call_args_list}
        assert called_paths == {
            "/data/projects/TESTPROJ/subjects/SUB001",
            "/data/projects/TESTPROJ/subjects/SUB002",
        }
