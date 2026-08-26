"""Tests for xnatctl CLI admin commands."""

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
                username="admin",
                password="adminpass",
                verify_ssl=False,
            )
        },
    )


def _mock_client() -> MagicMock:
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "admin"}
    return client


class TestAdminRefreshCatalogs:
    """Tests for admin refresh-catalogs command."""

    def test_refresh_catalogs_basic(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                    {"ID": "XNAT_E002", "subject_ID": "XNAT_S002", "label": "Sess2"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        ["admin", "refresh-catalogs", "TESTPROJ", "--no-parallel"],
                    )

        assert result.exit_code == 0
        assert "Refreshed" in result.output

    def test_refresh_catalogs_with_options(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--option",
                            "checksum",
                            "--option",
                            "delete",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0

    def test_refresh_catalogs_no_experiments(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"ResultSet": {"Result": []}}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "refresh-catalogs", "EMPTYPROJ"])

        assert result.exit_code == 0
        assert "No experiments" in result.output

    def test_refresh_catalogs_with_limit(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_E00{i}", "subject_ID": f"XNAT_S00{i}", "label": f"S{i}"}
                    for i in range(5)
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--limit",
                            "2",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0
        assert client.post.call_count == 2

    def test_refresh_catalogs_limit_zero_processes_nothing(self, runner: CliRunner) -> None:
        """``--limit 0`` must mean zero experiments, not "no limit"."""
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": f"XNAT_E00{i}", "subject_ID": f"XNAT_S00{i}", "label": f"S{i}"}
                    for i in range(5)
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--limit",
                            "0",
                            "--option",
                            "delete",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0
        assert client.post.call_count == 0
        assert "No experiments matched" in result.output

    def test_refresh_catalogs_json_output(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "refresh-catalogs",
                            "TESTPROJ",
                            "--output",
                            "json",
                            "--no-parallel",
                        ],
                    )

        assert result.exit_code == 0
        assert "refreshed" in result.output


class TestAdminUserAdd:
    """Tests for admin user add command."""

    def test_add_user_to_groups(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.put.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "PROJ1_member",
                            "PROJ2_owner",
                        ],
                    )

        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_user_to_groups_from_projects(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.put.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "EXTRA_group",
                            "--projects",
                            "PROJ1,PROJ2",
                            "--role",
                            "collaborator",
                        ],
                    )

        assert result.exit_code == 0
        call_args = client.put.call_args
        groups_sent = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "PROJ1_collaborator" in groups_sent
        assert "PROJ2_collaborator" in groups_sent
        assert "EXTRA_group" in groups_sent

    def test_add_user_to_groups_failure(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal server error"
        client.put.return_value = mock_resp

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "add",
                            "jsmith",
                            "PROJ1_member",
                        ],
                    )

        assert result.exit_code != 0


class TestAdminAudit:
    """Tests for admin audit command."""

    def test_audit_list(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [
            {
                "timestamp": "2024-01-15T10:00:00",
                "user": "admin",
                "action": "create",
                "resource": "/data/projects/PROJ1",
                "project": "PROJ1",
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit", "--limit", "10"])

        assert result.exit_code == 0

    def test_audit_no_entries(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit"])

        assert result.exit_code == 0
        assert "No audit entries" in result.output

    def test_audit_api_unavailable(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.side_effect = Exception("404 Not Found")

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit"])

        assert result.exit_code != 0

    def test_audit_bad_filter_is_not_swallowed_as_unavailable(self, runner: CliRunner) -> None:
        """A malformed --filter must raise its own usage error -- not get
        caught by the broad ``except Exception`` around the network call
        and misreported as "Audit log not available".
        """
        client = _mock_client()
        client.get_json.return_value = [
            {
                "timestamp": "2024-01-15T10:00:00",
                "user": "admin",
                "action": "create",
                "resource": "/data/projects/PROJ1",
                "project": "PROJ1",
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "audit", "--filter", "no-colon-here"])

        assert result.exit_code != 0
        assert "Audit log not available" not in result.output
        assert "field:glob" in result.output

    @staticmethod
    def _entry(timestamp: str, action: str) -> dict[str, str]:
        return {
            "timestamp": timestamp,
            "user": "admin" if action == "DELETE" else "u",
            "action": action,
            "resource": "",
            "project": "",
        }

    def test_audit_filter_sort_limit_sees_beyond_the_server_window(self, runner: CliRunner) -> None:
        """--filter must not be composed AFTER a small server-side --limit,
        and the full filter -> sort -> limit pipeline must run in that
        order end-to-end.

        Regression: fetching the server's small default/explicit --limit
        window FIRST and filtering it SECOND silently drops any match
        outside that window. Five matches are scattered beyond a small
        server window, in an order that requires --sort-by to reorder, and
        --limit (3) is smaller than the number of matches (5) -- so a
        composition bug (wrong fetch window, missing filter, or missing
        sort) each produce a different, wrong, ordered id list.
        """
        client = _mock_client()

        def fake_get_json(path: str, params: dict | None = None):
            params = params or {}
            if "limit" in params:
                # The server-truncated window never contains a match.
                return [self._entry(f"o{i}", "Running") for i in range(params["limit"])]
            return [
                self._entry("t03", "DELETE"),
                *(self._entry(f"o{i}", "Running") for i in range(30)),
                self._entry("t01", "DELETE"),
                *(self._entry(f"o{i}", "Running") for i in range(30, 60)),
                self._entry("t05", "DELETE"),
                *(self._entry(f"o{i}", "Running") for i in range(60, 90)),
                self._entry("t02", "DELETE"),
                *(self._entry(f"o{i}", "Running") for i in range(90, 120)),
                self._entry("t04", "DELETE"),
                *(self._entry(f"o{i}", "Running") for i in range(120, 150)),
            ]

        client.get_json.side_effect = fake_get_json

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "audit",
                            "--filter",
                            "action:DELETE",
                            "--sort-by",
                            "timestamp",
                            "--limit",
                            "3",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        # A disabled-TLS warning (verify_ssl=False in _mock_config) shares
        # stdout with the JSON in CliRunner's merged output; skip past it.
        rows = json.loads(result.output[result.output.index("[") :])
        assert [r["timestamp"] for r in rows] == ["t01", "t02", "t03"]

    def test_audit_sort_only_still_fetches_unbounded(self, runner: CliRunner) -> None:
        """--sort-by ALONE (no --filter) must also bypass the server window.

        The truncated (numeric-limit) fetch here returns an entirely
        different timestamp namespace ("o...") than the full set
        ("001".."150"), so sorting the wrong (truncated) page would never
        produce the true smallest ones -- this only passes if --sort-by
        alone (no --filter) still triggers an unbounded fetch.
        """
        client = _mock_client()

        def fake_get_json(path: str, params: dict | None = None):
            params = params or {}
            if "limit" in params:
                return [self._entry(f"o{i}", "Running") for i in range(params["limit"])]
            return [self._entry(f"{i:03d}", "Running") for i in range(150, 0, -1)]

        client.get_json.side_effect = fake_get_json

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "audit",
                            "--sort-by",
                            "timestamp",
                            "--limit",
                            "3",
                            "-o",
                            "json",
                        ],
                    )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert [r["timestamp"] for r in rows] == ["001", "002", "003"]


class TestAdminUserList:
    """Tests for admin user list."""

    def test_list_default_hits_profiles_endpoint(self, runner: CliRunner) -> None:
        """The bare /xapi/users route returns List[str] (usernames only) on real
        XNAT -- the default listing must hit /xapi/users/profiles instead, or
        every column here (email/enabled/verified) renders blank/crashes.
        """
        client = _mock_client()
        client.get_json.return_value = [
            {"username": "jsmith", "email": "j@example.org", "enabled": True, "verified": True},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "list", "--output", "json"])

        assert result.exit_code == 0
        assert "jsmith" in result.output
        client.get_json.assert_called_once_with("/xapi/users/profiles")

    def test_list_active_hits_active_endpoint_and_parses(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [
            {"username": "jsmith", "email": "j@example.org", "enabled": True, "verified": True},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "user", "list", "--active", "--output", "json"]
                    )

        assert result.exit_code == 0
        assert "jsmith" in result.output
        client.get_json.assert_called_once_with("/xapi/users/active")

    def test_list_quiet_usernames_only(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [{"username": "jsmith"}, {"username": "adoe"}]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "list", "--quiet"])

        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert lines[-2:] == ["jsmith", "adoe"]


class TestAdminUserShow:
    def test_show(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"username": "jsmith", "email": "j@example.org"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "show", "jsmith"])

        assert result.exit_code == 0
        assert "jsmith" in result.output


class TestAdminUserEnableDisable:
    def test_disable_puts_false(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "disable", "jsmith", "--yes"])

        assert result.exit_code == 0
        client.put.assert_called_once_with("/xapi/users/jsmith/enabled/false")

    def test_enable_puts_true(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "enable", "jsmith", "--yes"])

        assert result.exit_code == 0
        client.put.assert_called_once_with("/xapi/users/jsmith/enabled/true")

    def test_disable_dry_run_makes_no_http_call(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "disable", "jsmith", "--dry-run"])

        assert result.exit_code == 0
        client.put.assert_not_called()
        client.delete.assert_not_called()


class TestAdminUserRoles:
    def test_list_roles_plain(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = ["Administrator"]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "roles", "jsmith"])

        assert result.exit_code == 0
        assert "Administrator" in result.output
        client.put.assert_not_called()

    def test_blank_grant_rejected_not_treated_as_absent(self, runner: CliRunner) -> None:
        """`--grant ""` must fail cleanly rather than silently falling through to
        the plain-list branch.
        """
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "user", "roles", "jsmith", "--grant", "", "--yes"]
                    )

        assert result.exit_code != 0
        client.get_json.assert_not_called()
        client.put.assert_not_called()

    def test_blank_revoke_rejected_not_treated_as_absent(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "user", "roles", "jsmith", "--revoke", "", "--yes"]
                    )

        assert result.exit_code != 0
        client.get_json.assert_not_called()
        client.delete.assert_not_called()

    def test_grant_puts_role_path(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--grant",
                            "Administrator",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        client.put.assert_called_once_with("/xapi/users/jsmith/roles/Administrator")

    def test_revoke_deletes_role_path(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.delete.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--revoke",
                            "Administrator",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        client.delete.assert_called_once_with("/xapi/users/jsmith/roles/Administrator")

    def test_grant_dry_run_no_http_call(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--grant",
                            "Administrator",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        client.put.assert_not_called()

    def test_revoke_dry_run_no_http_call(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--revoke",
                            "Administrator",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        client.delete.assert_not_called()

    def test_grant_and_revoke_mutually_exclusive(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--grant",
                            "A",
                            "--revoke",
                            "B",
                            "--yes",
                        ],
                    )

        assert result.exit_code != 0
        assert "--grant and --revoke are mutually exclusive" in result.output
        client.put.assert_not_called()
        client.delete.assert_not_called()

    def test_grant_and_revoke_mutually_exclusive_writes_no_audit_record(
        self, runner: CliRunner, isolate_audit_log
    ) -> None:
        """The mutual-exclusion error fires from the confirm_destructive_when
        predicate, before the confirm/audit gate -- an invalid combination
        must not grow an audit entry even with --yes.
        """
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--grant",
                            "A",
                            "--revoke",
                            "B",
                            "--yes",
                        ],
                    )

        assert not isolate_audit_log.exists() or isolate_audit_log.read_text().strip() == ""

    def test_list_does_not_require_yes(self, runner: CliRunner) -> None:
        """A plain read must not demand confirmation just because the command carries it."""
        client = _mock_client()
        client.get_json.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "roles", "jsmith"])

        assert result.exit_code == 0

    def test_list_writes_no_audit_record(self, runner: CliRunner, isolate_audit_log) -> None:
        client = _mock_client()
        client.get_json.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    runner.invoke(cli, ["admin", "user", "roles", "jsmith"])

        assert not isolate_audit_log.exists() or isolate_audit_log.read_text().strip() == ""

    def test_grant_writes_exactly_one_audit_record(
        self, runner: CliRunner, isolate_audit_log
    ) -> None:
        client = _mock_client()
        client.put.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "roles",
                            "jsmith",
                            "--grant",
                            "Administrator",
                            "--yes",
                        ],
                    )

        lines = [line for line in isolate_audit_log.read_text().splitlines() if line.strip()]
        assert len(lines) == 1


class TestAdminUserGroups:
    def test_groups_lists_group_ids(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = ["PROJ01_owner", "PROJ02_member"]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "user", "groups", "jsmith"])

        assert result.exit_code == 0
        assert "PROJ01_owner" in result.output
        client.get_json.assert_called_once_with("/xapi/users/jsmith/groups")


class TestAdminUserKillSessions:
    def test_kill_sessions_deletes_active_path(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.delete.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "user", "kill-sessions", "jsmith", "--yes"]
                    )

        assert result.exit_code == 0
        client.delete.assert_called_once_with("/xapi/users/active/jsmith")

    def test_kill_sessions_dry_run_no_http_call(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "user", "kill-sessions", "jsmith", "--dry-run"]
                    )

        assert result.exit_code == 0
        client.delete.assert_not_called()


class TestAdminUserRemove:
    """Tests for admin user remove.

    Delegates to ProjectService.revoke -- the same group-resolution DELETE
    ``project revoke`` uses -- rather than AdminService.remove_user_from_groups,
    whose DELETE path (/data/projects/{P}/users/{username}) is not a real
    modifiable route in stock XNAT (that single-segment path is the read-only
    listing route; mutation needs the GROUP_ID segment).
    """

    def test_remove_from_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [{"login": "jsmith", "GROUP_ID": "TESTPROJ_member"}]
        client.delete.return_value = MagicMock(status_code=200)

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "remove",
                            "jsmith",
                            "--project",
                            "TESTPROJ",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        client.get_json.assert_called_once_with("/data/projects/TESTPROJ/users")
        client.delete.assert_called_once_with(
            "/data/projects/TESTPROJ/users/TESTPROJ_member/jsmith"
        )

    def test_remove_dry_run_no_http_call(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "user",
                            "remove",
                            "jsmith",
                            "--project",
                            "TESTPROJ",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        client.get_json.assert_not_called()
        client.delete.assert_not_called()


class TestAdminSiteConfig:
    def test_get_key_hits_property_path(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {"value": "MyXNAT"},
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "site-config", "get", "siteId"])

        assert result.exit_code == 0
        client.get.assert_called_once_with("/xapi/siteConfig/siteId")

    def test_set_with_yes_posts_raw_string_body(self, runner: CliRunner) -> None:
        """Confirmed against xnat-web's SiteConfigApi: setSiteConfigProperty is
        mapped POST, not PUT, and its @RequestBody is a plain String read by
        StringHttpMessageConverter (registered before Jackson in
        WebConfig.java) -- json="MyXNAT" would arrive literally quoted
        ('"MyXNAT"') since that converter never JSON-decodes the body. The
        body must be the raw value with a text/plain Content-Type instead.
        """
        client = _mock_client()
        client.post.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "site-config", "set", "siteId", "MyXNAT", "--yes"]
                    )

        assert result.exit_code == 0
        client.post.assert_called_once_with(
            "/xapi/siteConfig/siteId",
            content="MyXNAT",
            headers={"Content-Type": "text/plain"},
        )
        client.put.assert_not_called()

    def test_set_dry_run_no_write(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: "OldXNAT",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "site-config", "set", "siteId", "MyXNAT", "--dry-run"]
                    )

        assert result.exit_code == 0
        client.put.assert_not_called()
        client.post.assert_not_called()

    def test_set_dry_run_masks_secret_shaped_key(self, runner: CliRunner) -> None:
        """Neither the current nor the new value may appear in the preview when
        the key name looks secret-shaped (password/secret/token/key).
        """
        client = _mock_client()
        client.get.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: "hunter1",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "site-config",
                            "set",
                            "emailSmtpPassword",
                            "hunter2",
                            "--dry-run",
                        ],
                    )

        assert result.exit_code == 0
        assert "hunter1" not in result.output
        assert "hunter2" not in result.output
        assert "***" in result.output

    def test_set_writes_masked_value_to_audit_log(
        self, runner: CliRunner, isolate_audit_log
    ) -> None:
        """The audit record must never carry the plaintext value, regardless of
        whether the key name looks secret-shaped -- the key naming convention
        is unpredictable across deployments, so the value is always masked.
        """
        client = _mock_client()
        client.post.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    runner.invoke(
                        cli, ["admin", "site-config", "set", "siteId", "hunter2", "--yes"]
                    )

        audit_text = isolate_audit_log.read_text()
        assert "hunter2" not in audit_text
        assert '"value": "***"' in audit_text

    def test_set_success_line_masks_secret_shaped_key(self, runner: CliRunner) -> None:
        """The value must not leak into the SUCCESS line either -- terminals and
        CI logs persist it just as durably as the audit log does.
        """
        client = _mock_client()
        client.post.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli,
                        [
                            "admin",
                            "site-config",
                            "set",
                            "emailSmtpPassword",
                            "hunter2",
                            "--yes",
                        ],
                    )

        assert result.exit_code == 0
        assert "hunter2" not in result.output
        assert "***" in result.output
        client.post.assert_called_once_with(
            "/xapi/siteConfig/emailSmtpPassword",
            content="hunter2",
            headers={"Content-Type": "text/plain"},
        )

    def test_set_success_line_shows_non_secret_value(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.post.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "site-config", "set", "siteId", "MyXNAT", "--yes"]
                    )

        assert result.exit_code == 0
        assert "MyXNAT" in result.output

    @pytest.mark.parametrize("key", ["uiNewUserCaptchaPrivate", "someRecaptchaKey"])
    def test_dry_run_masks_expanded_secret_pattern(self, runner: CliRunner, key: str) -> None:
        """The secret-key pattern also covers 'private' and 'captcha', not just
        password/secret/token/key.
        """
        client = _mock_client()
        client.get.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: "old-value",
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(
                        cli, ["admin", "site-config", "set", key, "new-value", "--dry-run"]
                    )

        assert result.exit_code == 0
        assert "old-value" not in result.output
        assert "new-value" not in result.output
        assert "***" in result.output


class TestAdminPlugins:
    def test_plugins_json_output(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = [
            {"id": "container-service", "name": "Container Service", "version": "3.1.0"},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "plugins", "--output", "json"])

        assert result.exit_code == 0
        assert "container-service" in result.output

    def test_plugins_show(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {"id": "container-service", "version": "3.1.0"}

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "plugins", "show", "container-service"])

        assert result.exit_code == 0
        assert "container-service" in result.output


class TestAdminVersion:
    def test_version_quiet_bare_string(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get.return_value = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=lambda: {"version": "1.9.2"},
        )

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    result = runner.invoke(cli, ["admin", "version", "--quiet"])

        assert result.exit_code == 0
        assert result.output.strip().splitlines()[-1] == "1.9.2"
