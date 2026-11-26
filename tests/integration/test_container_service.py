from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import (
    AuthenticationError,
    ClientRequestError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServerError,
    SessionExpiredError,
    XNATConnectionError,
    XNATCtlError,
)
from xnatctl.services.commands import CommandService
from xnatctl.services.containers import ContainerService

# 300, not this tier's usual 120: pytest-timeout charges a test's setup to
# the test itself, and the first test in this module pays for the
# ``container_service_ready`` readiness poll (bounded at 210s below), which
# would otherwise be killed at 120s with its diagnostic unreported.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

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


_CS_READY_TIMEOUT_S = 210

_CS_ITERATION_BUDGET_S = 105

_CS_PROBE_SENTINEL_ID = 999_999_999


@pytest.fixture(scope="module", autouse=True)
def container_service_ready(
    xnat_server: str, credentials: tuple[str, str], xnat_client: Any
) -> Iterator[None]:
    from xnatctl.core.client import XNATClient

    user, password = credentials
    probe = XNATClient(base_url=xnat_server, username=user, password=password, timeout=15)
    transplanted = False
    try:
        deadline = time.monotonic() + _CS_READY_TIMEOUT_S
        last = "no attempt made"
        absence_proven = False
        while time.monotonic() + _CS_ITERATION_BUDGET_S < deadline:
            try:
                if not probe.session_token:
                    probe.authenticate()
                if not absence_proven:
                    try:
                        probe.get(f"/xapi/commands/{_CS_PROBE_SENTINEL_ID}", max_retries=0)
                    except ResourceNotFoundError:
                        absence_proven = True
                    else:
                        pytest.fail(
                            f"command id {_CS_PROBE_SENTINEL_ID} unexpectedly exists on "
                            "this server; refusing to use it as the readiness probe's "
                            "DELETE target"
                        )
                probe.delete(f"/xapi/commands/{_CS_PROBE_SENTINEL_ID}", max_retries=0)
            except SessionExpiredError as exc:
                last = f"{type(exc).__name__}: {exc}"
                probe.invalidate_session()
            except PermissionDeniedError:
                raise
            except AuthenticationError as exc:
                if "Invalid credentials" in str(exc):
                    pytest.fail(f"login rejected -- a credential problem, not warmup: {exc}")
                last = f"login failed: {exc}"
            except (XNATConnectionError, ServerError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if not probe.session_token:
                    pytest.fail(
                        "login answered 200 with an empty session token; "
                        "refusing to transplant it onto the shared client"
                    )
                xnat_client.close()
                xnat_client.session_token = probe.session_token
                transplanted = True
                break
            time.sleep(5)
        if not transplanted:
            pytest.fail(
                f"the Container Service did not accept mutations within "
                f"{_CS_READY_TIMEOUT_S}s; last failure from DELETE "
                f"/xapi/commands/{_CS_PROBE_SENTINEL_ID}: {last}"
            )
        yield
    finally:
        if not transplanted:
            probe.invalidate_session()
        probe.close()


@pytest.fixture
def probe_command(xnat_client: Any) -> Iterator[int]:
    resp = xnat_client.post("/xapi/commands", json=PROBE_COMMAND)
    command_id = int(resp.json())
    try:
        yield command_id
    finally:
        try:
            xnat_client.delete(f"/xapi/commands/{command_id}")
        except XNATCtlError as exc:
            print(f"\nWARNING: could not delete probe command {command_id}: {exc}")


class TestCommandRegistration:
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
        assert "xnat" in command
        assert command["xnat"][0]["name"] == "xnatctl-it-probe-wrapper"

    def test_derived_wrapper_list_finds_the_embedded_wrapper(
        self, xnat_client: Any, probe_command: int
    ) -> None:
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
        resp = xnat_client.post("/xapi/commands", json=PROBE_COMMAND)
        command_id = int(resp.json())
        service = CommandService(xnat_client)
        assert service.get_command(command_id)["name"] == "xnatctl-it-probe"

        xnat_client.delete(f"/xapi/commands/{command_id}")

        remaining_ids = [c["id"] for c in service.list_commands()]
        assert command_id not in remaining_ids


class TestMutatingVerbs:
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
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_command(999_999_999)

    def test_update_replaces_the_definition_and_can_wipe_wrappers(self, xnat_client: Any) -> None:
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
        service = CommandService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.set_wrapper_config(probe_command, 999_999_999, {"inputs": {}, "outputs": {}})

    def test_set_wrapper_config_does_not_re_enable_a_disabled_project_wrapper(
        self, xnat_client: Any, probe_command: int, integration_project: str
    ) -> None:
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
        service = ContainerService(xnat_client)
        _wrapper_id, wrapper = service.resolve_wrapper("xnatctl-it-probe-wrapper")

        with pytest.raises(ResourceNotFoundError):
            service.preflight_launch("XCTL-NO-SUCH-PROJECT", wrapper, {})

    def test_launch_queues_and_echoes_params_back(
        self, xnat_client: Any, probe_command: int, integration_project: str
    ) -> None:
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
        service = ContainerService(xnat_client)

        result = service.launch(integration_project, 999_999_999, {})

        assert result["status"] == "success"

    def test_kill_unknown_container_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.kill_container("999999")

    def test_kill_route_is_post_only(self, xnat_client: Any) -> None:
        with pytest.raises(ClientRequestError):
            xnat_client.get("/xapi/containers/999999/kill")
        with pytest.raises(ClientRequestError):
            xnat_client.put("/xapi/containers/999999/kill")

    def test_stream_logs_unknown_container_raises_not_found(self, xnat_client: Any) -> None:
        service = ContainerService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            with service.stream_logs("999999"):
                pass
