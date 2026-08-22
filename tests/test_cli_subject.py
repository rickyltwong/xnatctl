"""Tests for xnatctl CLI subject commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ResourceNotFoundError


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
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["subject", "show", "SUB001", "-P", "TESTPROJ"])

        assert result.exit_code == 0
        assert "SUB001" in result.output

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
