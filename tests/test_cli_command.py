"""Tests for xnatctl CLI `command` commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from conftest import AuthenticatedCLI

SAMPLE_COMMAND = {
    "id": 12,
    "name": "dcm2niix",
    "image": "xnat/dcm2niix:v1.2",
    "version": "1.2",
    "xnat": [{"id": 34, "name": "dcm2niix-scan"}],
}

NEW_COMMAND_PAYLOAD = {
    "name": "new-tool",
    "label": "New Tool",
    "image": "busybox:latest",
    "type": "docker",
    "command-line": "echo hi",
}


class TestCommandList:
    """Tests for `command list`."""

    def test_list_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["command", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands")
        assert "dcm2niix" in result.output

    def test_list_json_output(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["command", "list", "-o", "json"])

        assert result.exit_code == 0
        assert '"id": 12' in result.output
        # Computed client-side from "xnat", not part of the raw server row.
        assert '"wrapper_count": 1' in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["command", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "12"


class TestCommandShow:
    """Tests for `command show`."""

    def test_show_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(["command", "show", "12"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands/12")
        assert "dcm2niix" in result.output

    def test_show_json_output(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(["command", "show", "12", "-o", "json"])

        assert result.exit_code == 0
        assert '"image": "xnat/dcm2niix:v1.2"' in result.output


class TestCommandCreate:
    """Tests for `command create` -- happy path, declined confirmation, dry-run."""

    def test_create_from_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))
        authenticated_cli.client.post.return_value = MagicMock(json=MagicMock(return_value=45))

        result = authenticated_cli.invoke(["command", "create", str(payload_file), "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/commands", json=NEW_COMMAND_PAYLOAD
        )
        assert "45" in result.output

    def test_create_from_stdin(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = MagicMock(json=MagicMock(return_value=45))

        result = authenticated_cli.invoke(
            ["command", "create", "-", "--yes"], input=json.dumps(NEW_COMMAND_PAYLOAD)
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/commands", json=NEW_COMMAND_PAYLOAD
        )

    def test_create_stdin_without_yes_or_dry_run_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """FILE - can't be read AND leave stdin for a confirmation prompt."""
        result = authenticated_cli.invoke(
            ["command", "create", "-"], input=json.dumps(NEW_COMMAND_PAYLOAD)
        )

        assert result.exit_code != 0
        assert "--yes or --dry-run" in result.output
        authenticated_cli.client.post.assert_not_called()

    def test_create_prompt_abort_no_mutation(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))

        result = authenticated_cli.invoke(["command", "create", str(payload_file)], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_create_dry_run_no_http_call(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))

        result = authenticated_cli.invoke(["command", "create", str(payload_file), "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_create_invalid_json_rejected(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text("not json")

        result = authenticated_cli.invoke(["command", "create", str(payload_file), "--yes"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()


class TestCommandUpdate:
    """Tests for `command update` -- happy path, declined confirmation, dry-run diff."""

    def test_update_from_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND
        authenticated_cli.client.post.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(["command", "update", "12", str(payload_file), "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands/12")
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/commands/12", json=NEW_COMMAND_PAYLOAD
        )

    def test_update_prompt_abort_no_mutation(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))

        result = authenticated_cli.invoke(
            ["command", "update", "12", str(payload_file)], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_update_dry_run_prints_diff_not_generic_message(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(NEW_COMMAND_PAYLOAD))
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(
            ["command", "update", "12", str(payload_file), "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        # A unified diff, not "[DRY-RUN] Would ..." alone.
        assert "---" in result.output
        assert "+++" in result.output
        assert "new-tool" in result.output
        # ``update_command`` is a full replace: NEW_COMMAND_PAYLOAD omits
        # "xnat" entirely, so this update would silently wipe every wrapper
        # registered on the command. The whole point of the dry-run diff is
        # to make that removal visible -- assert the wrapper deletion is
        # actually there as removed ("-") lines, not just that the diff has
        # *some* markers and mentions the new name. A regression that
        # filtered "xnat" out of the comparison (e.g. diffing the payload
        # against itself, or dropping "xnat" before diffing) would still
        # satisfy the assertions above while hiding this exact danger.
        removed_lines = [
            line
            for line in result.output.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        assert any('"xnat"' in line for line in removed_lines), result.output
        assert any("dcm2niix-scan" in line for line in removed_lines), result.output

    def test_update_dry_run_previews_the_stale_id_stripping_execution_performs(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        """The preview must show the body execution actually sends, ids included.

        Execution runs the payload through the stale-wrapper-id stripping
        (a MODIFIED wrapper that reuses its old id makes the server answer
        a Hibernate identity conflict, so its id is dropped and the server
        mints a new one). A dry-run that diffed the RAW payload instead
        would show a renamed wrapper keeping its id while execution
        silently replaces it -- invalidating anything scoped to that id,
        such as
        `wrapper enable` or `wrapper config set`.

        This exercises the CLI path specifically. The service-level test of
        ``prepare_update_body`` cannot catch a regression here: if the CLI
        went back to diffing the raw payload, that test would still pass.
        """
        renamed_wrapper = {**SAMPLE_COMMAND["xnat"][0], "name": "renamed-scan"}
        payload = {**SAMPLE_COMMAND, "xnat": [renamed_wrapper]}
        payload_file = tmp_path / "command.json"
        payload_file.write_text(json.dumps(payload))
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(
            ["command", "update", "12", str(payload_file), "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        # The rename is visible...
        assert "renamed-scan" in result.output

        # ...and so is the id REMOVAL that comes with it. This has to assert
        # on a "-" line, not on the absence of a "+" line: if the preview
        # regressed to diffing the raw payload, the wrapper's id would be
        # identical on both sides and difflib would emit it as unchanged
        # CONTEXT -- so "no added line carries the id" is true in both the
        # fixed and the broken case, and would pin nothing. The id showing
        # up as removed is the thing that is only true once the preview runs
        # the same stale-id stripping the request does.
        removed_lines = [
            line
            for line in result.output.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        wrapper_id = SAMPLE_COMMAND["xnat"][0]["id"]
        assert any(f'"id": {wrapper_id}' in line for line in removed_lines), result.output


class TestCommandDelete:
    """Tests for `command delete` -- happy path, declined confirmation, dry-run."""

    def test_delete(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND
        authenticated_cli.client.delete.return_value = MagicMock(status_code=204)

        result = authenticated_cli.invoke(["command", "delete", "12", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with("/xapi/commands/12")

    def test_delete_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["command", "delete", "12"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()

    def test_delete_dry_run_checks_existence_no_delete(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run must run the same existence check execution does, but never DELETE.

        A dry-run that returns before any GET would report "would delete"
        for an unknown command_id -- execution's own preflight
        (delete_command()'s get_command() call) is what stands between a
        typo and DELETE's silent 204-even-if-missing no-op, and a dry-run
        diverging from it would hide that from a caller running dry-run
        first.
        """
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(["command", "delete", "12", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands/12")
        authenticated_cli.client.delete.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_delete_dry_run_unknown_command_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run must refuse an unknown command_id the same way execution does."""
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "command", "/xapi/commands/999"
        )

        result = authenticated_cli.invoke(["command", "delete", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()
        # The generic "[DRY-RUN] Preview mode" banner still prints (it's part
        # of the confirmation flow, ahead of the command body); what must NOT
        # appear is the per-command success line reporting "would delete".
        assert "Would delete" not in result.output
