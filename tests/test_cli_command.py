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
        assert '"wrapper_count": 1' in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_COMMAND]

        result = authenticated_cli.invoke(["command", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "12"


class TestCommandShow:
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
        assert "---" in result.output
        assert "+++" in result.output
        assert "new-tool" in result.output
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
        assert "renamed-scan" in result.output

        removed_lines = [
            line
            for line in result.output.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        wrapper_id = SAMPLE_COMMAND["xnat"][0]["id"]
        assert any(f'"id": {wrapper_id}' in line for line in removed_lines), result.output


class TestCommandDelete:
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
        authenticated_cli.client.get_json.return_value = SAMPLE_COMMAND

        result = authenticated_cli.invoke(["command", "delete", "12", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/commands/12")
        authenticated_cli.client.delete.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_delete_dry_run_unknown_command_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "command", "/xapi/commands/999"
        )

        result = authenticated_cli.invoke(["command", "delete", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()
        assert "Would delete" not in result.output
