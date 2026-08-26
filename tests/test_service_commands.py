"""Unit tests for CommandService.

Fixture shapes are the real ones observed live against XNAT 1.9.2.1 +
Container Service 3.7.2 (see the ground-truth notes this rewrite was built
from) -- wrappers embedded under "xnat", no "enabled" key anywhere, no
server-side wrapper-listing or single-wrapper endpoint.
"""

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
    """Tests for CommandService.list_commands."""

    def test_list_commands(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.list_commands()

        assert result == [SAMPLE_COMMAND]
        fake_client.get_json.assert_called_once_with("/xapi/commands")

    def test_list_commands_non_list_response_raises(self, fake_client) -> None:
        """An unexpected 2xx shape must raise, not silently become []."""
        fake_client.get_json.return_value = {"error": "nope"}
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_commands()


class TestGetCommand:
    """Tests for CommandService.get_command."""

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
    """Tests for CommandService.list_wrappers -- derived client-side from `xnat`."""

    def test_list_wrappers_scoped_to_command(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_COMMAND]
        service = CommandService(fake_client)

        result = service.list_wrappers(command_id=12)

        assert len(result) == 1
        assert result[0]["id"] == 34
        assert result[0]["command-id"] == 12
        # No server-side wrapper endpoint exists -- the scoped call is
        # still just GET /xapi/commands, filtered client-side.
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
        """Explicit null is one of the three legitimate "no wrappers" shapes."""
        command = dict(SAMPLE_COMMAND, xnat=None)
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        assert service.list_wrappers() == []

    def test_list_wrappers_missing_xnat_key_means_no_wrappers(self, fake_client) -> None:
        """Absent key is also legitimately "no wrappers", not an error."""
        command = {k: v for k, v in SAMPLE_COMMAND.items() if k != "xnat"}
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        assert service.list_wrappers() == []

    def test_list_wrappers_invalid_xnat_shape_raises_naming_command_and_type(
        self, fake_client
    ) -> None:
        """`"xnat": {}` (or any non-list, non-null shape) is a schema regression,
        not "no wrappers" -- it must raise, not silently produce an empty,
        successful listing that hides the real problem.
        """
        command = dict(SAMPLE_COMMAND, xnat={})
        fake_client.get_json.return_value = [command]
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.list_wrappers()

        message = str(exc_info.value)
        assert "12" in message  # names the command
        assert "dict" in message  # names what was received

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
    """Tests for CommandService.get_wrapper -- also derived client-side."""

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
    """Tests for CommandService.get_wrapper_config."""

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
    """Tests for CommandService.resolve_wrapper -- all four resolution outcomes."""

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
        """Same wrapper name on two different commands must error, not silently pick one."""
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
    """Tests for CommandService.create_command.

    ``POST /xapi/commands`` answers 201 with the new ID as a BARE INTEGER
    body (not a JSON object) -- verified live.
    """

    def test_create_command(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(45)
        service = CommandService(fake_client)

        result = service.create_command(NEW_COMMAND_PAYLOAD)

        assert result == 45
        fake_client.post.assert_called_once_with("/xapi/commands", json=NEW_COMMAND_PAYLOAD)

    def test_create_command_non_int_response_raises(self, fake_client, response_factory) -> None:
        """An unexpected 2xx shape (a JSON object, say) must raise, not be coerced."""
        fake_client.post.return_value = response_factory({"id": 45})
        service = CommandService(fake_client)

        with pytest.raises(XNATCtlError):
            service.create_command(NEW_COMMAND_PAYLOAD)


class TestUpdateCommand:
    """Tests for CommandService.update_command.

    ``POST /xapi/commands/{id}`` is a full replace -- there is no ``PUT``
    (405 Method Not Allowed) -- and a nonexistent ID answers 500 (a raw
    Hibernate exception), not 404, so this checks existence first.
    """

    def test_update_command(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.post.return_value = response_factory("", content_type="text/plain")
        service = CommandService(fake_client)

        service.update_command(12, NEW_COMMAND_PAYLOAD)

        fake_client.get_json.assert_called_once_with("/xapi/commands/12")
        fake_client.post.assert_called_once_with("/xapi/commands/12", json=NEW_COMMAND_PAYLOAD)

    def test_update_command_not_found_checks_existence_first(self, fake_client) -> None:
        """The real 500-on-unknown-ID is never reached: the existence check
        (get_command) raises ResourceNotFoundError before any POST happens.
        """
        fake_client.get_json.side_effect = ResourceNotFoundError("command", "/xapi/commands/999")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.update_command(999, NEW_COMMAND_PAYLOAD)

        fake_client.post.assert_not_called()

    def test_update_command_strips_id_from_a_modified_wrapper(
        self, fake_client, response_factory
    ) -> None:
        """A wrapper whose content changed must not keep its old id on the wire.

        Verified live against XNAT 1.9.2.1 + Container Service 3.7.2:
        reusing a wrapper's current numeric id on a MODIFIED entry makes the
        server's POST answer 500 -- a Hibernate identity conflict ("A
        different object with the same identifier value was already
        associated with the session"). Stripping the id from a changed
        entry avoids it and lets the server mint a fresh one.
        """
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
        """A wrapper submitted byte-for-byte unchanged keeps its existing id.

        Verified live: re-POSTing the exact same ``xnat`` array, id
        included, succeeds and the id stays stable -- only a modified entry
        sharing its old id triggers the conflict.
        """
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
        """A "new" wrapper entry that happens to reuse a different wrapper's id is also stripped.

        Content differs from what that id currently maps to, so it hits the
        same identity conflict a modified entry does.
        """
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
        """``command update --dry-run`` and execution must build the request body identically.

        A dry-run that diffed the raw payload would show a modified
        wrapper keeping an id that execution's stale-id stripping actually
        removes. Both paths go through
        ``prepare_update_body``, so this asserts its returned body matches
        what ``update_command`` itself sends -- exercised at the service
        layer rather than the CLI, since that is the one place the
        transformation lives.
        """
        fake_client.get_json.return_value = SAMPLE_COMMAND
        service = CommandService(fake_client)
        renamed = {**SAMPLE_COMMAND, "xnat": [{**SAMPLE_COMMAND["xnat"][0], "name": "renamed"}]}

        current, previewed_body = service.prepare_update_body(12, renamed)

        assert current == SAMPLE_COMMAND
        assert "id" not in previewed_body["xnat"][0]
        assert previewed_body["xnat"][0]["name"] == "renamed"
        # And it's a pure preview: no POST happened.
        fake_client.post.assert_not_called()


class TestDropStaleWrapperIds:
    """Edge cases for :func:`_drop_stale_wrapper_ids`, called directly.

    :class:`TestUpdateCommand` above covers the common cases (modified,
    unchanged, new-reusing-another-id) through the service method; these
    cover shapes the service tests don't exercise.
    """

    def test_duplicate_ids_in_payload_only_first_occurrence_keeps_its_id(self) -> None:
        """Two payload entries sharing one id must not both reach the server with it.

        If both occurrences compared equal to the one current wrapper and
        both kept the id, the server would see the same id twice in one
        ``xnat`` array -- reintroducing the Hibernate identity conflict this
        function exists to prevent.
        """
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
        """``"xnat": null`` and ``"xnat": []`` both mean "no wrappers"."""
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
        """An id valid on another command isn't in `current`, so it can't match -- stripped."""
        foreign_wrapper = dict(OTHER_COMMAND_SAME_WRAPPER_NAME["xnat"][0])
        payload = {**SAMPLE_COMMAND, "xnat": [foreign_wrapper]}

        result = _drop_stale_wrapper_ids(SAMPLE_COMMAND, payload)

        assert "id" not in result["xnat"][0]
        assert result["xnat"][0]["name"] == "dcm2niix-scan"


class TestDeleteCommand:
    """Tests for CommandService.delete_command.

    ``DELETE /xapi/commands/{id}`` answers 204 even for a nonexistent ID --
    idempotent-succeeds, not idempotent-404s -- so this checks existence
    first via get_command rather than trusting the DELETE response.
    """

    def test_delete_command(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = SAMPLE_COMMAND
        fake_client.delete.return_value = response_factory(None, status_code=204)
        service = CommandService(fake_client)

        service.delete_command(12)

        fake_client.get_json.assert_called_once_with("/xapi/commands/12")
        fake_client.delete.assert_called_once_with("/xapi/commands/12")

    def test_delete_command_not_found_checks_existence_first(self, fake_client) -> None:
        """Without this guard, DELETE on an unknown ID would silently 204."""
        fake_client.get_json.side_effect = ResourceNotFoundError("command", "/xapi/commands/999")
        service = CommandService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_command(999)

        fake_client.delete.assert_not_called()


class TestEnableDisableWrapper:
    """Tests for CommandService.enable_wrapper / disable_wrapper.

    ``PUT .../enabled`` and ``PUT .../disabled`` are two SEPARATE routes,
    not one route with a boolean query parameter -- verified live,
    ``PUT .../enabled?enable=false`` does not disable anything.
    """

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
        """The project-scoped route answers 200 for a bad project -- this must not reach it."""
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
        """The project-scoped route answers 200 for a bad project -- this must not reach it."""
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
    """Tests for CommandService.set_wrapper_config.

    ``POST .../config`` does not validate the wrapper ID server-side at all
    (a nonexistent wrapper ID still answers 201, verified live) -- this
    method's own existence check (via ``check_wrapper_config_scope`` ->
    ``get_wrapper``) is the only thing that turns an unknown ID into an
    error instead of an orphaned config row.

    ``POST .../config`` also defaults its ``enable`` query parameter to
    ``true`` when omitted. Reproduced live on 2026-08-25 against XNAT
    1.9.2.1 + Container Service 3.7.2, at both scopes: enable, then disable,
    then POST a configuration with no ``enable`` param -- ``GET
    .../enabled`` (site) and ``enabled-for-project`` (project) both flip
    back to ``true`` from the config write alone. See
    ``CommandService.set_wrapper_config``'s docstring for the exact request/
    response sequence. Every test here that reaches the POST supplies the
    current enabled state as a second ``get_json`` return (site: a bare
    bool; project: the ``enabled-for-project`` object) and asserts the POST
    carries the matching ``enable`` param, so a regression that drops that
    param again would fail these rather than only showing up live.
    """

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
        """A deliberately disabled wrapper must not come back enabled from a config write."""
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
