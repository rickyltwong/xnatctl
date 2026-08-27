"""Tests for xnatctl CLI project commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _mock_config() -> Config:
    """Build a mock Config with a default profile."""
    return Config(
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


class TestProjectList:
    """Tests for project list command."""

    def test_project_list_top_level_list(self, runner: CliRunner) -> None:
        """Project list tolerates bare top-level JSON arrays."""
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [
            {
                "ID": "PROJ1",
                "name": "Project One",
                "pi_lastname": "Smith",
                "description": "Test project",
            }
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_table(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "Smith",
                        "description": "Test project",
                    },
                    {
                        "ID": "PROJ2",
                        "name": "Project Two",
                        "pi_lastname": "Jones",
                        "description": "",
                    },
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_filter_and_limit(self, runner: CliRunner) -> None:
        """--filter and --limit narrow the JSON output, client-side.

        The non-matching rows are placed FIRST in the server response: if
        --filter were silently ignored, --limit 2 alone would return exactly
        those two non-matching rows and this test would still pass by
        accident. Ordering it this way means the assertions only pass when
        filtering actually ran before the limit was applied.
        """
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "PROJZ", "name": "Other", "pi_lastname": "", "description": ""},
                    {"ID": "PROJY", "name": "Another", "pi_lastname": "", "description": ""},
                ]
                + [
                    {"ID": f"PROJ{i}", "name": f"ABC{i}", "pi_lastname": "", "description": ""}
                    for i in range(3)
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "list",
                            "--filter",
                            "name:ABC*",
                            "--limit",
                            "2",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        # A disabled-TLS warning (verify_ssl=False in _mock_config) shares
        # stdout with the JSON in CliRunner's merged output; skip past it.
        rows = json.loads(result.output[result.output.index("[") :])
        assert [row["id"] for row in rows] == ["PROJ0", "PROJ1"]

    def test_project_list_json(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "Smith",
                        "description": "Test project",
                    },
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "list", "--output", "json"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_quiet(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {
                        "ID": "PROJ1",
                        "name": "Project One",
                        "pi_lastname": "",
                        "description": "",
                    },
                ]
            }
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "list", "--quiet"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_list_empty(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0


class TestProjectShow:
    """Tests for project show command."""

    def test_project_show_items_response(self, runner: CliRunner) -> None:
        """Project show handles `items[]` detail responses."""
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.side_effect = [
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "PROJ1",
                            "name": "Project One",
                            "secondary_ID": "SEC01",
                        }
                    }
                ]
            },
            {"ResultSet": {"Result": [{"ID": "SUB1"}, {"ID": "SUB2"}]}},
            {"ResultSet": {"Result": [{"ID": "EXP1"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "show", "PROJ1"])

        assert result.exit_code == 0
        assert "SEC01" in result.output

    def test_project_show(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.side_effect = [
            {
                "ResultSet": {
                    "Result": [
                        {
                            "ID": "PROJ1",
                            "name": "Project One",
                            "secondary_ID": "",
                            "pi_lastname": "Smith",
                            "description": "A test project",
                            "accessibility": "private",
                        }
                    ]
                }
            },
            {"ResultSet": {"Result": [{"ID": "SUB1"}, {"ID": "SUB2"}]}},
            {"ResultSet": {"Result": [{"ID": "EXP1"}]}},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "show", "PROJ1"])

        assert result.exit_code == 0
        assert "PROJ1" in result.output

    def test_project_show_not_found(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "show", "NONEXIST"])

        assert result.exit_code != 0


class TestProjectCreate:
    """Tests for project create command."""

    def test_project_create_success(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "create",
                            "NEWPROJ",
                            "--name",
                            "New Project",
                            "--pi",
                            "Smith",
                        ],
                    )

        assert result.exit_code == 0
        assert "NEWPROJ" in result.output

    def test_project_create_failure(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Project already exists"
        mock_client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "create", "EXISTING"])

        assert result.exit_code != 0

    def test_project_create_with_description(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "create",
                            "NEWPROJ",
                            "--description",
                            "A new project",
                            "--accessibility",
                            "protected",
                        ],
                    )

        assert result.exit_code == 0
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "accessibility" in str(call_kwargs)


class TestProjectUsers:
    """Tests for project users command."""

    def test_users_json(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [
            {"login": "jsmith", "GROUP_ID": "PROJ1_owner", "email": "j@example.org"},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "users", "PROJ1", "--output", "json"])

        assert result.exit_code == 0
        assert "jsmith" in result.output
        assert '"role": "owner"' in result.output
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ1/users")

    def test_users_quiet_usernames(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [
            {"login": "jsmith"},
            {"login": "adoe"},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "users", "PROJ1", "--quiet"])

        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert lines[-2:] == ["jsmith", "adoe"]


class TestProjectGrant:
    """Tests for project grant command."""

    def test_grant_puts_singular_group_id(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "grant",
                            "PROJ",
                            "jsmith",
                            "--role",
                            "member",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        mock_client.put.assert_called_once_with("/data/projects/PROJ/users/PROJ_member/jsmith")

    def test_grant_invalid_role_lists_valid_roles(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        ["project", "grant", "PROJ", "jsmith", "--role", "superadmin", "--yes"],
                    )

        assert result.exit_code != 0
        assert (
            "Invalid value for '--role': 'superadmin' is not one of "
            "'owner', 'member', 'collaborator'." in result.output
        )
        mock_client.put.assert_not_called()

    def test_grant_dry_run_no_http_call(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        [
                            "project",
                            "grant",
                            "PROJ",
                            "jsmith",
                            "--role",
                            "owner",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        mock_client.put.assert_not_called()


class TestProjectRevoke:
    """Tests for project revoke command."""

    def test_revoke_deletes_group_id_verbatim(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [{"login": "jsmith", "GROUP_ID": "PROJ_member"}]
        mock_client.delete.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "revoke", "PROJ", "jsmith", "--yes"])

        assert result.exit_code == 0
        mock_client.delete.assert_called_once_with("/data/projects/PROJ/users/PROJ_member/jsmith")

    def test_revoke_removes_from_every_group_a_multi_group_user_holds(
        self, runner: CliRunner
    ) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [
            {"login": "jsmith", "GROUP_ID": "PROJ_member"},
            {"login": "jsmith", "GROUP_ID": "PROJ_owner"},
        ]
        mock_client.delete.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "revoke", "PROJ", "jsmith", "--yes"])

        assert result.exit_code == 0
        assert mock_client.delete.call_count == 2

    def test_revoke_dry_run_no_http_call(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli, ["project", "revoke", "PROJ", "jsmith", "--dry-run"]
                    )

        assert result.exit_code == 0
        mock_client.get_json.assert_not_called()
        mock_client.delete.assert_not_called()


class TestProjectAccess:
    """Tests for project access command."""

    def test_access_get(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get.return_value = MagicMock(status_code=200, text="private")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "access", "PROJ"])

        assert result.exit_code == 0
        assert "private" in result.output
        mock_client.get.assert_called_once_with("/data/projects/PROJ/accessibility")

    def test_access_get_does_not_require_yes(self, runner: CliRunner) -> None:
        """A plain read must not demand confirmation."""
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get.return_value = MagicMock(status_code=200, text="private")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "access", "PROJ"], input="")

        assert result.exit_code == 0

    def test_access_get_writes_no_audit_record(self, runner: CliRunner, isolate_audit_log) -> None:
        """A read through a `confirm_destructive_when` command must not be audited."""
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get.return_value = MagicMock(status_code=200, text="private")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "access", "PROJ"])

        assert result.exit_code == 0
        assert not isolate_audit_log.exists() or isolate_audit_log.read_text().strip() == ""

    def test_access_set_puts_level(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli, ["project", "access", "PROJ", "--set", "private", "--yes"]
                    )

        assert result.exit_code == 0
        mock_client.put.assert_called_once_with("/data/projects/PROJ/accessibility/private")

    def test_access_set_writes_exactly_one_audit_record(
        self, runner: CliRunner, isolate_audit_log
    ) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli, ["project", "access", "PROJ", "--set", "private", "--yes"]
                    )

        assert result.exit_code == 0
        lines = [line for line in isolate_audit_log.read_text().splitlines() if line.strip()]
        assert len(lines) == 1

    def test_access_set_dry_run_no_write(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli, ["project", "access", "PROJ", "--set", "private", "--dry-run"]
                    )

        assert result.exit_code == 0
        mock_client.put.assert_not_called()
        mock_client.get.assert_not_called()


class TestProjectRequests:
    """Tests for project requests command."""

    def test_requests_list_hits_project_scoped_route(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = [
            {"par_id": "42", "login": "jsmith", "email": "j@example.org"},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "requests", "PROJ"])

        assert result.exit_code == 0
        assert "jsmith" in result.output
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ/pars")
        mock_client.put.assert_not_called()

    def test_requests_list_writes_no_audit_record(
        self, runner: CliRunner, isolate_audit_log
    ) -> None:
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.get_json.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "requests", "PROJ"])

        assert result.exit_code == 0
        assert not isolate_audit_log.exists() or isolate_audit_log.read_text().strip() == ""

    def test_requests_has_no_approve_or_deny_options(self, runner: CliRunner) -> None:
        """PAR resolution always acts on the CURRENT SESSION USER in stock XNAT
        (ProjectAccessRequest.process()), not the request's actual invitee --
        an admin "approving" someone else's PAR would add themselves to the
        project instead. There is no safe admin-side resolution to offer, so
        the command exposes no --approve/--deny at all.
        """
        mock_client = MagicMock()
        mock_client.is_authenticated = True
        mock_client.base_url = "https://xnat.example.org"
        mock_client.whoami.return_value = {"username": "testuser"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["project", "requests", "PROJ", "--approve", "42"])

        assert result.exit_code != 0
        assert "no such option" in result.output.lower()
        mock_client.put.assert_not_called()

    def test_requests_help_documents_no_resolution(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["project", "requests", "--help"])

        assert result.exit_code == 0
        assert "invitee" in result.output.lower() or "invitation" in result.output.lower()
