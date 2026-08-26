"""Tests for xnatctl CLI `wrapper` commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from conftest import AuthenticatedCLI

from xnatctl.core.exceptions import ResourceNotFoundError

SAMPLE_COMMAND = {
    "id": 12,
    "name": "dcm2niix",
    "xnat": [
        {
            "id": 34,
            "name": "dcm2niix-scan",
            "description": "Convert a scan",
            "contexts": ["xnat:imageScanData"],
        }
    ],
}

OTHER_COMMAND_SAME_WRAPPER_NAME = {
    "id": 13,
    "name": "other-tool",
    "xnat": [{"id": 99, "name": "dcm2niix-scan", "contexts": ["xnat:imageScanData"]}],
}


class TestWrapperList:
    """Tests for `wrapper list`."""

    def test_list_site_wide(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands")
        assert "dcm2niix-scan" in result.output
        assert "Enabled" not in result.output

    def test_list_scoped_to_command(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "list", "--command", "12"])

        assert result.exit_code == 0
        # No server-side scoped endpoint exists -- still GET /xapi/commands.
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands")
        assert "dcm2niix-scan" in result.output

    def test_list_json_output_keeps_contexts_as_array(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "list", "-o", "json"])

        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert rows[0]["contexts"] == ["xnat:imageScanData"]
        assert rows[0]["command-id"] == 12

    def test_list_table_output_joins_contexts(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "list"])

        assert result.exit_code == 0
        assert "xnat:imageScanData" in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "34"


class TestWrapperConfigGet:
    """Tests for `wrapper config get`."""

    def test_get_by_numeric_id_site_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # resolve_wrapper()'s list_wrappers() -> list_commands()
            {"inputs": {}, "outputs": {}},  # the config GET itself
        ]

        result = authenticated_cli.invoke(["wrapper", "config", "get", "34"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_any_call("/xapi/wrappers/34/config")

    def test_get_by_name(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],
            {"inputs": {}, "outputs": {}},
        ]

        result = authenticated_cli.invoke(["wrapper", "config", "get", "dcm2niix-scan"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_any_call("/xapi/wrappers/34/config")

    def test_get_project_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],
            {"inputs": {}},
        ]

        result = authenticated_cli.invoke(["wrapper", "config", "get", "34", "-P", "PROJ01"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_any_call(
            "/xapi/projects/PROJ01/wrappers/34/config"
        )

    def test_get_ambiguous_name_fails(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [
            SAMPLE_COMMAND,
            OTHER_COMMAND_SAME_WRAPPER_NAME,
        ]

        result = authenticated_cli.invoke(["wrapper", "config", "get", "dcm2niix-scan"])

        assert result.exit_code != 0
        assert "ambiguous" in result.output.lower()

    def test_get_json_output(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],
            {"inputs": {}, "outputs": {}},
        ]

        result = authenticated_cli.invoke(["wrapper", "config", "get", "34", "-o", "json"])

        assert result.exit_code == 0
        assert '"outputs": {}' in result.output


class TestWrapperEnable:
    """Tests for `wrapper enable` -- happy path, declined confirmation, dry-run."""

    def test_enable_site_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(["wrapper", "enable", "12", "34", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/commands/12/wrappers/34/enabled"
        )

    def test_enable_project_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
        )
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["wrapper", "enable", "12", "34", "-P", "PROJ01", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/enabled"
        )

    def test_enable_nonexistent_project_surfaces_as_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """The project-scoped route answers 200 for a bad project -- this must not reach it."""
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": []}}
        )

        result = authenticated_cli.invoke(
            ["wrapper", "enable", "12", "34", "-P", "TYPOPROJ", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_enable_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["wrapper", "enable", "12", "34"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_enable_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "enable", "12", "34", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_enable_unknown_wrapper_surfaces_as_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "enable", "12", "999", "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_enable_dry_run_nonexistent_project_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """`--dry-run` must refuse a nonexistent project the same way execution does.

        A dry-run that calls only ``get_wrapper()`` would skip the project
        preflight ``enable_wrapper`` runs before its PUT -- the
        project-scoped route silently succeeds (200) for a project that
        does not exist, so a dry run that skipped this check would report
        success too.
        """
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": []}}
        )

        result = authenticated_cli.invoke(
            ["wrapper", "enable", "12", "34", "-P", "TYPOPROJ", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "Would enable" not in result.output
        authenticated_cli.client.put.assert_not_called()


class TestWrapperDisable:
    """Tests for `wrapper disable` -- happy path, declined confirmation, dry-run."""

    def test_disable_site_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(["wrapper", "disable", "12", "34", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/commands/12/wrappers/34/disabled"
        )

    def test_disable_project_scoped(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
        )
        authenticated_cli.client.put.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["wrapper", "disable", "12", "34", "-P", "PROJ01", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/disabled"
        )

    def test_disable_nonexistent_project_surfaces_as_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """The project-scoped route answers 200 for a bad project -- this must not reach it."""
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": []}}
        )

        result = authenticated_cli.invoke(
            ["wrapper", "disable", "12", "34", "-P", "TYPOPROJ", "--yes"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_disable_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["wrapper", "disable", "12", "34"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.put.assert_not_called()

    def test_disable_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["wrapper", "disable", "12", "34", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.put.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_disable_dry_run_nonexistent_project_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """`--dry-run` must refuse a nonexistent project the same way execution does.

        A dry-run that calls only ``get_wrapper()`` would skip the project
        preflight ``disable_wrapper`` runs before its PUT -- the
        project-scoped route silently succeeds (200) for a project that
        does not exist, so a dry run that skipped this check would report
        success too.
        """
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]
        authenticated_cli.client.get.return_value = MagicMock(
            json=lambda: {"ResultSet": {"Result": []}}
        )

        result = authenticated_cli.invoke(
            ["wrapper", "disable", "12", "34", "-P", "TYPOPROJ", "--dry-run"]
        )

        assert result.exit_code != 0
        assert "Would disable" not in result.output
        authenticated_cli.client.put.assert_not_called()


class TestWrapperConfigSet:
    """Tests for `wrapper config set` -- happy path, declined confirmation, dry-run diff."""

    NEW_CONFIG = {"inputs": {"scan": {"user-settable": True}}, "outputs": {}}

    def test_set_from_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        """Configuring a wrapper must preserve its current enabled state.

        Verified live: ``POST .../config``'s ``enable`` param defaults to
        ``true`` when omitted, silently re-enabling a wrapper the caller
        may have deliberately disabled -- so this asserts the POST carries
        the wrapper's actual current state (fetched via a second GET)
        explicitly, not that the param is simply absent. See
        ``CommandService.set_wrapper_config``'s docstring for the exact live
        sequence this guards against.
        """
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # existence check (get_wrapper), inside check_wrapper_config_scope
            False,  # current site-scoped enabled state
        ]
        authenticated_cli.client.post.return_value = MagicMock(status_code=201)

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file), "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/wrappers/34/config", params={"enable": "false"}, json=self.NEW_CONFIG
        )

    def test_set_project_scoped(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # existence check (get_wrapper), inside check_wrapper_config_scope
            {"enabled-for-site": True, "enabled-for-project": True, "project": "PROJ01"},
        ]
        authenticated_cli.client.post.return_value = MagicMock(status_code=201)

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file), "-P", "PROJ01", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/config",
            params={"enable": "true"},
            json=self.NEW_CONFIG,
        )

    def test_set_from_stdin_requires_yes_or_dry_run(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", "-"], input=json.dumps(self.NEW_CONFIG)
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_set_prompt_abort_no_mutation(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file)], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_set_dry_run_prints_diff_not_generic_message(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # existence check (get_wrapper), inside check_wrapper_config_scope
            True,  # current site-scoped enabled state
            {"inputs": {}, "outputs": {}},  # current config for the diff
        ]

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file), "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "---" in result.output
        assert "+++" in result.output
        assert "scan" in result.output

    def test_set_dry_run_shows_enabled_state_that_will_be_preserved(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        """The preview must surface the same enabled state execution carries forward.

        A dry-run that diffed only the config body would never read (or
        show) the enabled state, so a preview would give no warning that a
        config write on a disabled wrapper is the exact scenario that
        silently re-enables it (verified live -- see
        ``CommandService.set_wrapper_config``'s docstring).
        """
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # existence check (get_wrapper), inside check_wrapper_config_scope
            False,  # current site-scoped enabled state -- disabled
            {"inputs": {}, "outputs": {}},  # current config for the diff
        ]

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file), "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "disabled" in result.output
        assert "preserved" in result.output

    def test_set_dry_run_succeeds_when_no_config_exists_yet(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        """`--dry-run` must succeed for a first-time config write, same as execution does.

        Regression test: ``get_wrapper_config`` raises ``ResourceNotFoundError``
        when no configuration exists yet for the wrapper (its documented
        contract -- see ``CommandService.get_wrapper_config``), but the
        execution path never reads current config at all; it just POSTs and
        the server 201-creates. Before the fix, ``--dry-run`` propagated that
        ResourceNotFoundError and exited non-zero for exactly the case
        execution handles fine -- dry-run must not fail where execution
        succeeds. A missing config must render as an all-additions diff
        (``current = {}``), the same convention ``json_diff`` already uses
        for ``command create``.
        """
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(self.NEW_CONFIG))
        authenticated_cli.client.get_json.side_effect = [
            [SAMPLE_COMMAND],  # existence check (get_wrapper), inside check_wrapper_config_scope
            True,  # current site-scoped enabled state
            ResourceNotFoundError("wrapper config", "34"),  # no config exists yet
        ]

        result = authenticated_cli.invoke(
            ["wrapper", "config", "set", "12", "34", str(config_file), "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "---" in result.output
        assert "+++" in result.output
        assert "scan" in result.output
