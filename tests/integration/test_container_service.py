"""Container Service, checked against a real server.

Every endpoint the unit suite for xnatctl.services.commands/containers/
docker_admin mocks was verified once, by hand, against this same stack (see
the ground-truth notes that rewrite was built from). This file is what keeps
that verification from going stale: it registers a real command, and checks
that CommandService parses what Container Service 3.7.2 actually sends back
-- particularly the two things that were wrong before that rewrite, wrappers
embedded under "xnat" rather than a server-side listing endpoint, and a
wrapper config route that takes a bare wrapper ID.

A *successful* container launch/kill needs a reachable Docker daemon, which
this stack does not provide (see services/containers.py's module
docstring), so ``TestContainerLaunchAndKill`` below covers everything
reachable WITHOUT one: wrapper resolution, the launch route's real
request/response shape and its lack of server-side validation, the
project-existence preflight, and kill/logs against a nonexistent container.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import ClientRequestError, ResourceNotFoundError
from xnatctl.services.commands import CommandService
from xnatctl.services.containers import ContainerService

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

#: A minimal but complete command.json, confirmed live against this stack to
#: register (201, body is the bare new numeric ID) and to produce a wrapper
#: reachable through every documented config-read form.
PROBE_COMMAND = {
    "name": "xnatctl-it-probe",
    "label": "xnatctl integration probe",
    "description": "Registered by the xnatctl integration tier; deleted afterward.",
    "version": "0.1.0",
    "image": "busybox:latest",
    "type": "docker",
    "command-line": "echo hello",
    "xnat": [
        {
            "name": "xnatctl-it-probe-wrapper",
            "description": "Probe wrapper",
            "contexts": ["xnat:imageSessionData"],
            "external-inputs": [{"name": "session", "type": "Session", "required": True}],
        }
    ],
}


@pytest.fixture
def probe_command(xnat_client: Any) -> Iterator[int]:
    """Register PROBE_COMMAND, yield its new ID, delete it afterward.

    ``POST /xapi/commands`` answers 201 with the new ID as a bare integer
    body (not JSON), so this reads ``resp.json()`` rather than going through
    ``get_json``, which always sends ``format=json`` -- a POST response
    doesn't take that query param.
    """
    resp = xnat_client.post("/xapi/commands", json=PROBE_COMMAND)
    command_id = int(resp.json())
    try:
        yield command_id
    finally:
        try:
            xnat_client.delete(f"/xapi/commands/{command_id}")
        except Exception as exc:  # noqa: BLE001  # teardown must not mask a test result
            print(f"\nWARNING: could not delete probe command {command_id}: {exc}")


class TestCommandRegistration:
    """Registration, and that CommandService parses the real response shapes."""

    def test_list_commands_sees_the_registered_command(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        service = CommandService(xnat_client)

        commands = service.list_commands()

        ids = [c["id"] for c in commands]
        assert probe_command in ids

    def test_get_command_returns_the_full_definition(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        service = CommandService(xnat_client)

        command = service.get_command(probe_command)

        assert command["name"] == "xnatctl-it-probe"
        assert command["image"] == "busybox:latest"
        # The real key holding embedded wrappers -- not "xnat-wrappers".
        assert "xnat" in command
        assert command["xnat"][0]["name"] == "xnatctl-it-probe-wrapper"

    def test_derived_wrapper_list_finds_the_embedded_wrapper(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        """There is no server-side wrapper-listing endpoint (GET /xapi/wrappers is
        404; GET /xapi/commands/{id}/wrappers is 405) -- list_wrappers() must derive
        the wrapper from the command's own "xnat" array.
        """
        service = CommandService(xnat_client)

        wrappers = service.list_wrappers(command_id=probe_command)

        assert len(wrappers) == 1
        assert wrappers[0]["name"] == "xnatctl-it-probe-wrapper"
        assert wrappers[0]["command-id"] == probe_command
        assert "enabled" not in wrappers[0]

    def test_resolve_wrapper_by_name_finds_it(self, xnat_client: Any, probe_command: int) -> None:
        service = CommandService(xnat_client)

        wrapper_id, wrapper = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        assert wrapper["command-id"] == probe_command
        assert wrapper["id"] == wrapper_id

    def test_wrapper_config_get_returns_the_observed_shape(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        """GET /xapi/wrappers/{wrapperId}/config -- the form CommandService uses. Its
        real shape has explicit nulls for unset fields, not absent keys.
        """
        service = CommandService(xnat_client)
        wrapper_id, _ = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        config = service.get_wrapper_config(wrapper_id)

        assert "inputs" in config
        assert "outputs" in config
        session_input = config["inputs"]["session"]
        assert session_input["type"] == "Session"
        assert "description" in session_input
        assert session_input["description"] is None

    def test_deleting_a_command_removes_it(self, xnat_client: Any) -> None:
        """Registers and deletes its own command rather than reusing the
        probe_command fixture, so its teardown does not double-delete.
        """
        resp = xnat_client.post("/xapi/commands", json=PROBE_COMMAND)
        command_id = int(resp.json())
        service = CommandService(xnat_client)
        assert service.get_command(command_id)["name"] == "xnatctl-it-probe"

        xnat_client.delete(f"/xapi/commands/{command_id}")

        remaining_ids = [c["id"] for c in service.list_commands()]
        assert command_id not in remaining_ids


class TestMutatingVerbs:
    """The mutating half: create, update, enable/disable, config set, delete.

    Each test registers and cleans up its own command rather than sharing
    ``probe_command`` -- several of these mutate the command itself (update
    replaces it, delete removes it), which would break that fixture's own
    teardown if shared.
    """

    def test_create_then_delete_command(self, xnat_client: Any) -> None:
        service = CommandService(xnat_client)

        command_id = service.create_command(PROBE_COMMAND)
        try:
            assert service.get_command(command_id)["name"] == "xnatctl-it-probe"
        finally:
            service.delete_command(command_id)

        with pytest.raises(ResourceNotFoundError):
            service.get_command(command_id)

    def test_delete_unknown_command_raises_not_found(self, xnat_client: Any) -> None:
        """Verifies the existence-check guard: DELETE alone answers 204 for
        any ID, verified live -- delete_command() must not trust that.
        """
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_command(999_999_999)

    def test_update_replaces_the_definition_and_can_wipe_wrappers(self, xnat_client: Any) -> None:
        """Verifies the full-replace behavior: updating without ``xnat`` in
        the payload wipes the wrapper the command was created with.
        """
        service = CommandService(xnat_client)
        command_id = service.create_command(PROBE_COMMAND)
        try:
            before = service.get_command(command_id)
            assert len(before["xnat"]) == 1

            updated = dict(PROBE_COMMAND, label="updated by xnatctl integration test")
            del updated["xnat"]
            service.update_command(command_id, updated)

            after = service.get_command(command_id)
            assert after["label"] == "updated by xnatctl integration test"
            assert after["xnat"] == []
        finally:
            service.delete_command(command_id)

    def test_update_unknown_command_raises_not_found(self, xnat_client: Any) -> None:
        """The real server answers 500 (Hibernate exception) for this, not
        404 -- verifies the existence-check guard converts it cleanly.
        """
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.update_command(999_999_999, PROBE_COMMAND)

    def test_enable_disable_wrapper_round_trips(self, xnat_client: Any, probe_command: int) -> None:
        service = CommandService(xnat_client)
        wrapper_id, _ = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        service.disable_wrapper(probe_command, wrapper_id)
        disabled = xnat_client.get_json(
            f"/xapi/commands/{probe_command}/wrappers/{wrapper_id}/enabled"
        )
        assert disabled is False

        service.enable_wrapper(probe_command, wrapper_id)
        enabled = xnat_client.get_json(
            f"/xapi/commands/{probe_command}/wrappers/{wrapper_id}/enabled"
        )
        assert enabled is True

    def test_enable_wrapper_unknown_pair_raises_not_found(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.enable_wrapper(probe_command, 999_999_999)

    def test_set_wrapper_config_then_read_it_back(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        """Verified live: the server does not persist every field of an
        input configuration (e.g. ``description``) -- this only asserts on
        the field that does persist (``user-settable``), rather than
        asserting a full round-trip the server itself does not provide.
        """
        service = CommandService(xnat_client)
        wrapper_id, _ = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        service.set_wrapper_config(
            probe_command,
            wrapper_id,
            {"inputs": {"session": {"user-settable": True}}, "outputs": {}},
        )

        config = service.get_wrapper_config(wrapper_id)
        assert config["inputs"]["session"]["user-settable"] is True

    def test_set_wrapper_config_unknown_pair_raises_not_found(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        """The route itself does NOT validate the wrapper ID (a nonexistent
        one still answers 201, verified live) -- this is entirely the
        service's own existence-check guard.
        """
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.set_wrapper_config(probe_command, 999_999_999, {"inputs": {}, "outputs": {}})

    def test_set_wrapper_config_does_not_re_enable_a_disabled_project_wrapper(
        self, xnat_client: Any, probe_command: int, integration_project: str
    ) -> None:
        """Regression test for a real re-enable bug, reproduced live 2026-08-25.

        The project-scoped enable route is inert until the wrapper is also
        enabled at site scope -- two earlier probes missed this bug entirely
        because they skipped that step and saw no visible change. The
        sequence below is the one that actually reproduces it: enable at
        site, enable at project, disable at project, then write
        configuration with no ``enable`` param. Before this fix, the config
        write silently flipped ``enabled-for-project`` back to ``true``.
        """
        service = CommandService(xnat_client)
        wrapper_id, _ = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        service.enable_wrapper(probe_command, wrapper_id)
        service.enable_wrapper(probe_command, wrapper_id, project=integration_project)
        service.disable_wrapper(probe_command, wrapper_id, project=integration_project)

        state = xnat_client.get_json(
            f"/xapi/projects/{integration_project}/wrappers/{wrapper_id}/enabled"
        )
        assert state["enabled-for-project"] is False

        service.set_wrapper_config(
            probe_command,
            wrapper_id,
            {"inputs": {"session": {"user-settable": True}}, "outputs": {}},
            project=integration_project,
        )

        state = xnat_client.get_json(
            f"/xapi/projects/{integration_project}/wrappers/{wrapper_id}/enabled"
        )
        assert state["enabled-for-project"] is False


class TestContainerLaunchAndKill:
    """`container launch`/`kill`: everything reachable without a Docker daemon.

    A container never runs to completion on this stack (see the module
    docstring), so nothing here asserts on a populated Container object or a
    successful kill. What IS asserted is real, reproducible server behavior:
    the launch route's shape, its total lack of server-side validation
    (verified live -- a real regression risk this project's client-side
    checks exist specifically to cover), and that both ``kill`` and
    ``logs``/``get`` answer a clean 404 for an unknown container id rather
    than something uglier.
    """

    def test_resolve_wrapper_by_id_and_name_agree(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        service = ContainerService(xnat_client)

        by_name = service.resolve_wrapper("xnatctl-it-probe-wrapper")
        by_id = service.resolve_wrapper(str(by_name[0]))

        assert by_id == by_name

    def test_resolve_unknown_wrapper_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.resolve_wrapper("no-such-wrapper")

    def test_list_containers_site_and_project_scoped(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        """No container has ever run here -- both scopes stay real, verified empties."""
        service = ContainerService(xnat_client)

        assert service.list() == []
        assert service.list(project=integration_project) == []

    def test_get_unknown_container_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get("999999")

    def test_preflight_launch_maps_experiment_to_the_wrapper_session_input(
        self, xnat_client: Any, probe_command: int, integration_project: str
    ) -> None:
        service = ContainerService(xnat_client)
        _wrapper_id, wrapper = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        params = service.preflight_launch(
            integration_project, wrapper, {}, experiment="XNAT_E00001"
        )

        assert params == {"session": "XNAT_E00001"}

    def test_preflight_launch_unknown_project_raises_not_found(
        self, xnat_client: Any, probe_command: int
    ) -> None:
        """The raw launch route answers an unhelpful 500 for a bad project
        (verified live: "There was an error in the request : null") --
        preflight_launch exists specifically to turn that into a clean,
        typed not-found before anything is sent.
        """
        service = ContainerService(xnat_client)
        _wrapper_id, wrapper = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        with pytest.raises(ResourceNotFoundError):
            service.preflight_launch("XCTL-NO-SUCH-PROJECT", wrapper, {})

    def test_launch_queues_and_echoes_params_back(
        self, xnat_client: Any, probe_command: int, integration_project: str
    ) -> None:
        """Verified live: the launch route answers 200 with a LaunchReport --
        "status", "params" (echoed back unchanged), "command-id",
        "wrapper-id", "workflow-id". No "container-id" key exists on this
        response (see services/containers.py's module docstring) -- nothing
        here asserts on one.
        """
        service = ContainerService(xnat_client)
        wrapper_id, wrapper = service.resolve_wrapper("xnatctl-it-probe-wrapper")
        params = service.build_launch_params(wrapper, {}, experiment="XNAT_E00001")

        result = service.launch(integration_project, wrapper_id, params)

        assert result["status"] == "success"
        assert result["params"] == {"session": "XNAT_E00001"}
        assert result["wrapper-id"] == wrapper_id
        assert "workflow-id" in result
        assert "container-id" not in result

    def test_launch_does_not_validate_wrapper_id_server_side(
        self, xnat_client: Any, integration_project: str
    ) -> None:
        """Regression guard for the exact gap ContainerService.resolve_wrapper
        exists to cover client-side: the server queues (200 "success") a
        launch against a wrapper ID that does not exist at all, rather than
        rejecting it. If this ever starts raising instead, the client-side
        resolve-before-launch guard in cli/container.py can be relaxed.
        """
        service = ContainerService(xnat_client)

        result = service.launch(integration_project, 999_999_999, {})

        assert result["status"] == "success"

    def test_kill_unknown_container_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.kill_container("999999")

    def test_kill_route_is_post_only(self, xnat_client: Any) -> None:
        """GET/PUT on the kill path answer 405 -- confirms POST is a real,
        registered route rather than a catch-all matched by any method.
        """
        with pytest.raises(ClientRequestError):
            xnat_client.get("/xapi/containers/999999/kill")
        with pytest.raises(ClientRequestError):
            xnat_client.put("/xapi/containers/999999/kill")

    def test_stream_logs_unknown_container_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            with service.stream_logs("999999"):
                pass
