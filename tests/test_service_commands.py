from __future__ import annotations

import pytest

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.services.commands import CommandService, _drop_stale_wrapper_ids

SAMPLE_COMMAND = {
    "id": 12,
    "name": "dcm2niix",
    "label": "dcm2niix",
    "description": "Convert DICOM to NIfTI",
    "version": "1.2",
    "image": "xnat/dcm2niix:v1.2",
    "type": "docker",
    "command-line": "dcm2niix [ARGS]",
    "mounts": [],
    "environment-variables": {},
    "ports": {},
    "inputs": [],
    "outputs": [],
    "xnat": [
        {
            "id": 34,
            "name": "dcm2niix-scan",
            "label": "dcm2niix (scan)",
            "description": "Convert a scan",
            "contexts": ["xnat:imageScanData"],
            "external-inputs": [{"name": "scan", "type": "Scan", "required": True}],
            "derived-inputs": [],
            "output-handlers": [],
        }
    ],
    "container-labels": {},
    "generic-resources": {},
    "ulimits": {},
    "secrets": [],
    "visibility": "public",
}

OTHER_COMMAND_SAME_WRAPPER_NAME = {
    "id": 13,
    "name": "other-tool",
    "xnat": [
        {
            "id": 99,
            "name": "dcm2niix-scan",
            "contexts": ["xnat:imageScanData"],
        }
    ],
}


class TestListCommands:
    def test_list_commands(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.list_commands()

        assert result == [SAMPLE_COMMAND]
        fake_client.get_json.assert_called_once_with("/xapi/commands")

    def test_list_commands_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"error": "nope"}
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_commands()


class TestGetCommand:
    def test_get_command(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        service = CommandService(fake_client)

        result = service.get_command(12)

        assert result == SAMPLE_COMMAND
        fake_client.get_json.assert_called_once_with("/xapi/commands/12")

    def test_get_command_not_found(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("command", "/xapi/commands/999")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_command(999)


class TestListWrappers:
    def test_list_wrappers_scoped_to_command(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.list_wrappers(command_id=12)

        assert len(result) == 1
        assert result[0]["id"] == 34
        assert result[0]["command-id"] == 12
        fake_client.get_json.assert_called_once_with("/xapi/commands")

    def test_list_wrappers_scoped_to_unknown_command_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.list_wrappers(command_id=999)

    def test_list_wrappers_site_wide_extracted_from_commands(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.list_wrappers()

        assert len(result) == 1
        assert result[0]["id"] == 34
        assert result[0]["command-id"] == 12
        assert "enabled" not in result[0]
        fake_client.get_json.assert_called_once_with("/xapi/commands")

    def test_list_wrappers_site_wide_no_commands(self, fake_client) -> None:
        fake_client.get_json.return_value = []
        service = CommandService(fake_client)

        assert service.list_wrappers() == []

    def test_list_wrappers_null_xnat_means_no_wrappers(self, fake_client) -> None:
        command = dict(SAMPLE_COMMAND, xnat=None)
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        assert service.list_wrappers() == []

    def test_list_wrappers_missing_xnat_key_means_no_wrappers(self, fake_client) -> None:
        command = {k: v for k, v in SAMPLE_COMMAND.items() if k != "xnat"}
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        assert service.list_wrappers() == []

    def test_list_wrappers_invalid_xnat_shape_raises_naming_command_and_type(
        self, fake_client
    ) -> None:
        command = dict(SAMPLE_COMMAND, xnat={})
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.list_wrappers()

        message = str(exc_info.value)
        assert "12" in message
        assert "dict" in message

    @pytest.mark.parametrize("bad_xnat", ["", 0], ids=["empty-string", "zero"])
    def test_list_wrappers_other_invalid_xnat_shapes_also_raise(
        self, fake_client, bad_xnat
    ) -> None:
        command = dict(SAMPLE_COMMAND, xnat=bad_xnat)
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_wrappers()


class TestGetWrapper:
    def test_get_wrapper(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.get_wrapper(12, 34)

        assert result["name"] == "dcm2niix-scan"
        fake_client.get_json.assert_called_once_with("/xapi/commands")

    def test_get_wrapper_not_found(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_wrapper(12, 999)


class TestGetWrapperConfig:
    def test_site_scoped(self, fake_client) -> None:
        config = {
            "inputs": {
                "session": {
                    "description": None,
                    "type": "Session",
                    "default-value": None,
                    "matcher": None,
                    "user-settable": None,
                    "advanced": False,
                    "required": None,
                }
            },
            "outputs": {},
        }
        fake_client.get_json.return_value = config
        service = CommandService(fake_client)

        result = service.get_wrapper_config(34)

        assert result == config
        fake_client.get_json.assert_called_once_with("/xapi/wrappers/34/config")

    def test_project_scoped(self, fake_client) -> None:
        fake_client.get_json.return_value = {"inputs": {}, "outputs": {}}
        service = CommandService(fake_client)

        service.get_wrapper_config(34, project="PROJ01")

        fake_client.get_json.assert_called_once_with("/xapi/projects/PROJ01/wrappers/34/config")

    def test_not_found(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("wrapper config", "34")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_wrapper_config(34)


class TestResolveWrapper:
    def test_resolve_by_numeric_id(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        wrapper_id, wrapper = service.resolve_wrapper("34")

        assert wrapper_id == 34
        assert wrapper["name"] == "dcm2niix-scan"

    def test_resolve_by_name(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        wrapper_id, wrapper = service.resolve_wrapper("dcm2niix-scan")

        assert wrapper_id == 34
        assert wrapper["command-id"] == 12

    def test_resolve_ambiguous_name_raises_naming_every_candidate(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND, OTHER_COMMAND_SAME_WRAPPER_NAME]
        service = CommandService(fake_client)

        with pytest.raises(InputValidationError) as exc_info:
            service.resolve_wrapper("dcm2niix-scan")

        message = str(exc_info.value)
        assert "command 12 wrapper 34" in message
        assert "command 13 wrapper 99" in message

    def test_resolve_unknown_id_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.resolve_wrapper("999")

    def test_resolve_unknown_name_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.resolve_wrapper("no-such-wrapper")


NEW_COMMAND_PAYLOAD = {
    "name": "new-tool",
    "label": "New Tool",
    "image": "busybox:latest",
    "type": "docker",
    "command-line": "echo hi",
}


class TestCreateCommand:
    def test_create_command(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(45)
        service = CommandService(fake_client)

        result = service.create_command(NEW_COMMAND_PAYLOAD)

        assert result == 45
        fake_client.post.assert_called_once_with("/xapi/commands", json=NEW_COMMAND_PAYLOAD)

    def test_create_command_non_int_response_raises(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory({"id": 45})
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError):
            service.create_command(NEW_COMMAND_PAYLOAD)


class TestUpdateCommand:
    def test_update_command(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.post.return_value = response_factory("", content_type="text/plain")
        service = CommandService(fake_client)

        service.update_command(12, NEW_COMMAND_PAYLOAD)

        fake_client.get_json.assert_called_once_with("/xapi/commands/12")
        fake_client.post.assert_called_once_with("/xapi/commands/12", json=NEW_COMMAND_PAYLOAD)

    def test_update_command_not_found_checks_existence_first(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("command", "/xapi/commands/999")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.update_command(999, NEW_COMMAND_PAYLOAD)

        fake_client.post.assert_not_called()

    def test_update_command_strips_id_from_a_modified_wrapper(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.post.return_value = response_factory("", content_type="text/plain")
        service = CommandService(fake_client)

        renamed = {**SAMPLE_COMMAND, "xnat": [{**SAMPLE_COMMAND["xnat"][0], "name": "renamed"}]}

        service.update_command(12, renamed)

        sent_body = fake_client.post.call_args.kwargs["json"]
        assert "id" not in sent_body["xnat"][0]
        assert sent_body["xnat"][0]["name"] == "renamed"

    def test_update_command_keeps_id_for_an_unchanged_wrapper(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.post.return_value = response_factory("", content_type="text/plain")
        service = CommandService(fake_client)

        unchanged_wrapper_payload = {**SAMPLE_COMMAND, "description": "new command-level text"}

        service.update_command(12, unchanged_wrapper_payload)

        sent_body = fake_client.post.call_args.kwargs["json"]
        assert sent_body["xnat"][0]["id"] == 34
        assert sent_body["description"] == "new command-level text"

    def test_update_command_strips_id_from_a_new_wrapper_reusing_another_ones_id(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.post.return_value = response_factory("", content_type="text/plain")
        service = CommandService(fake_client)

        payload = {
            **SAMPLE_COMMAND,
            "xnat": [{"id": 34, "name": "totally-different-wrapper", "contexts": []}],
        }

        service.update_command(12, payload)

        sent_body = fake_client.post.call_args.kwargs["json"]
        assert "id" not in sent_body["xnat"][0]

    def test_update_command_dry_run_previews_the_same_body_execution_sends(
        self, fake_client
    ) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        service = CommandService(fake_client)
        renamed = {**SAMPLE_COMMAND, "xnat": [{**SAMPLE_COMMAND["xnat"][0], "name": "renamed"}]}

        current, previewed_body = service.prepare_update_body(12, renamed)

        assert current == SAMPLE_COMMAND
        assert "id" not in previewed_body["xnat"][0]
        assert previewed_body["xnat"][0]["name"] == "renamed"
        fake_client.post.assert_not_called()


class TestDropStaleWrapperIds:
    def test_duplicate_ids_in_payload_only_first_occurrence_keeps_its_id(self) -> None:
        current = SAMPLE_COMMAND
        wrapper = SAMPLE_COMMAND["xnat"][0]
        payload = {**SAMPLE_COMMAND, "xnat": [wrapper, dict(wrapper)]}

        result = _drop_stale_wrapper_ids(current, payload)

        ids = [w.get("id") for w in result["xnat"]]
        assert ids == [34, None]

    def test_xnat_absent_from_payload_is_returned_unchanged(self) -> None:
        payload = {"id": 12, "name": "dcm2niix"}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert result is payload

    def test_xnat_null_in_payload_normalizes_to_an_empty_list(self) -> None:
        payload = {**SAMPLE_COMMAND, "xnat": None}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert result["xnat"] == []

    def test_non_dict_entry_passed_through_for_the_server_to_reject(self) -> None:
        payload = {**SAMPLE_COMMAND, "xnat": ["not-a-wrapper-object"]}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert result["xnat"] == ["not-a-wrapper-object"]

    def test_wrapper_with_no_id_passed_through_unchanged(self) -> None:
        new_wrapper = {"name": "brand-new-wrapper", "contexts": []}
        payload = {**SAMPLE_COMMAND, "xnat": [new_wrapper]}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert result["xnat"] == [new_wrapper]

    def test_id_belonging_to_a_different_command_is_stripped(self) -> None:
        foreign_wrapper = dict(OTHER_COMMAND_SAME_WRAPPER_NAME["xnat"][0])
        payload = {**SAMPLE_COMMAND, "xnat": [foreign_wrapper]}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert "id" not in result["xnat"][0]
        assert result["xnat"][0]["name"] == "dcm2niix-scan"


class TestDeleteCommand:
    def test_delete_command(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.delete.return_value = response_factory(None, status_code=204)
        service = CommandService(fake_client)

        service.delete_command(12)

        fake_client.get_json.assert_called_once_with("/xapi/commands/12")
        fake_client.delete.assert_called_once_with("/xapi/commands/12")

    def test_delete_command_not_found_checks_existence_first(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("command", "/xapi/commands/999")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_command(999)

        fake_client.delete.assert_not_called()


class TestEnableDisableWrapper:
    def test_enable_wrapper_site_scoped(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.put.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.enable_wrapper(12, 34)

        fake_client.put.assert_called_once_with("/xapi/commands/12/wrappers/34/enabled")

    def test_enable_wrapper_project_scoped(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.get.return_value = response_factory(
            {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
        )
        fake_client.put.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.enable_wrapper(12, 34, project="PROJ01")

        fake_client.put.assert_called_once_with("/xapi/projects/PROJ01/wrappers/34/enabled")

    def test_enable_wrapper_nonexistent_project_raises_without_calling_put(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.get.return_value = response_factory({"ResultSet": {"Result": []}})
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.enable_wrapper(12, 34, project="NOPE")

        fake_client.put.assert_not_called()

    def test_disable_wrapper_site_scoped(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.put.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.disable_wrapper(12, 34)

        fake_client.put.assert_called_once_with("/xapi/commands/12/wrappers/34/disabled")

    def test_disable_wrapper_project_scoped(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.get.return_value = response_factory(
            {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
        )
        fake_client.put.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.disable_wrapper(12, 34, project="PROJ01")

        fake_client.put.assert_called_once_with("/xapi/projects/PROJ01/wrappers/34/disabled")

    def test_disable_wrapper_nonexistent_project_raises_without_calling_put(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        fake_client.get.return_value = response_factory({"ResultSet": {"Result": []}})
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.disable_wrapper(12, 34, project="NOPE")

        fake_client.put.assert_not_called()

    def test_enable_wrapper_unknown_pair_raises_without_calling_put(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.enable_wrapper(12, 999)

        fake_client.put.assert_not_called()

    def test_disable_wrapper_unknown_command_raises_without_calling_put(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.disable_wrapper(999, 34)

        fake_client.put.assert_not_called()


class TestSetWrapperConfig:
    NEW_CONFIG = {"inputs": {"scan": {"user-settable": True}}, "outputs": {}}

    def test_set_wrapper_config_site_scoped_preserves_enabled_true(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.side_effect = [[SAMPLE_COMMAND], True]
        fake_client.post.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.set_wrapper_config(12, 34, self.NEW_CONFIG)

        fake_client.post.assert_called_once_with(
            "/xapi/wrappers/34/config", params={"enable": "true"}, json=self.NEW_CONFIG
        )

    def test_set_wrapper_config_site_scoped_preserves_disabled(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.side_effect = [[SAMPLE_COMMAND], False]
        fake_client.post.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.set_wrapper_config(12, 34, self.NEW_CONFIG)

        fake_client.post.assert_called_once_with(
            "/xapi/wrappers/34/config", params={"enable": "false"}, json=self.NEW_CONFIG
        )

    def test_set_wrapper_config_project_scoped_preserves_disabled(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.side_effect = [
            [SAMPLE_COMMAND],
            {"enabled-for-site": True, "enabled-for-project": False, "project": "PROJ01"},
        ]
        fake_client.post.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.set_wrapper_config(12, 34, self.NEW_CONFIG, project="PROJ01")

        fake_client.post.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/config",
            params={"enable": "false"},
            json=self.NEW_CONFIG,
        )

    def test_set_wrapper_config_project_scoped_preserves_enabled_true(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.side_effect = [
            [SAMPLE_COMMAND],
            {"enabled-for-site": True, "enabled-for-project": True, "project": "PROJ01"},
        ]
        fake_client.post.return_value = response_factory(None, content_type="text/plain")
        service = CommandService(fake_client)

        service.set_wrapper_config(12, 34, self.NEW_CONFIG, project="PROJ01")

        fake_client.post.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/config",
            params={"enable": "true"},
            json=self.NEW_CONFIG,
        )

    def test_set_wrapper_config_unknown_pair_raises_without_posting(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.set_wrapper_config(12, 999, self.NEW_CONFIG)

        fake_client.post.assert_not_called()
