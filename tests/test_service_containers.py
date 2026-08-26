"""Unit tests for ContainerService.

The container object could not be captured live (no Docker daemon in the
integration stack), but its field names ARE verified -- read out of the
shipped plugin jar's compiled DTOs (see services/containers.py's module
docstring). The sample row here uses a subset of that real field set.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.services.commands import CommandService
from xnatctl.services.containers import ContainerService

SAMPLE_CONTAINER = {
    "id": "501",
    "command-id": 12,
    "wrapper-id": 34,
    "status": "Complete",
    "project": "PROJ01",
    "user-id": "jsmith",
}

SAMPLE_WRAPPER = {
    "id": 34,
    "name": "dcm2niix-scan",
    "command-id": 12,
    "external-inputs": [{"name": "session", "type": "Session", "required": True}],
}


class TestInit:
    """Tests for ContainerService's CommandService composition."""

    def test_defaults_to_fresh_command_service(self, fake_client) -> None:
        service = ContainerService(fake_client)

        assert isinstance(service._commands, CommandService)  # noqa: SLF001

    def test_accepts_injected_command_service(self, fake_client) -> None:
        injected = CommandService(fake_client)

        service = ContainerService(fake_client, command_service=injected)

        assert service._commands is injected  # noqa: SLF001


class TestList:
    """Tests for ContainerService.list."""

    def test_list_site_wide(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_CONTAINER]
        service = ContainerService(fake_client)

        result = service.list()

        assert result == [SAMPLE_CONTAINER]
        fake_client.get_json.assert_called_once_with("/xapi/containers")

    def test_list_scoped_to_project(self, fake_client) -> None:
        fake_client.get_json.return_value = []
        service = ContainerService(fake_client)

        service.list(project="PROJ01")

        fake_client.get_json.assert_called_once_with("/xapi/projects/PROJ01/containers")

    def test_list_filters_by_status_client_side(self, fake_client) -> None:
        running = dict(SAMPLE_CONTAINER, id="502", status="Running")
        fake_client.get_json.return_value = [SAMPLE_CONTAINER, running]
        service = ContainerService(fake_client)

        result = service.list(status="Running")

        assert result == [running]

    def test_list_filters_by_status_legitimate_empty_match(self, fake_client) -> None:
        """Rows carry `status`, none match -- a real empty result, not an error."""
        fake_client.get_json.return_value = [SAMPLE_CONTAINER]
        service = ContainerService(fake_client)

        result = service.list(status="NoSuchStatus")

        assert result == []

    def test_list_filters_by_status_raises_when_field_absent_from_every_row(
        self, fake_client
    ) -> None:
        """No row carries a `status` key at all -- distinguishable from a legitimate empty match."""
        fake_client.get_json.return_value = [{"id": "501"}, {"id": "502"}]
        service = ContainerService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list(status="Running")

    def test_list_filters_by_status_no_containers_is_not_an_error(self, fake_client) -> None:
        """An empty site has no rows to inspect for a `status` key -- not the same failure."""
        fake_client.get_json.return_value = []
        service = ContainerService(fake_client)

        assert service.list(status="Running") == []

    def test_list_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {}
        service = ContainerService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list()


class TestGet:
    """Tests for ContainerService.get -- container_id is a str (not always numeric)."""

    def test_get_numeric_id(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_CONTAINER
        service = ContainerService(fake_client)

        result = service.get("501")

        assert result == SAMPLE_CONTAINER
        fake_client.get_json.assert_called_once_with("/xapi/containers/501")

    def test_get_docker_id_string(self, fake_client) -> None:
        """Container routes also accept the Docker container ID string."""
        fake_client.get_json.return_value = SAMPLE_CONTAINER
        service = ContainerService(fake_client)

        service.get("weu6k9c3f1a2")

        fake_client.get_json.assert_called_once_with("/xapi/containers/weu6k9c3f1a2")

    def test_get_not_found(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("container", "999")
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get("999")

    def test_get_non_dict_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = []
        service = ContainerService(fake_client)

        with pytest.raises(XNATCtlError):
            service.get("501")


class TestStreamLogs:
    """Tests for ContainerService.stream_logs -- returns the client's streaming context manager."""

    def _stream_ctx(self, chunks: list[bytes]) -> MagicMock:
        resp = MagicMock()
        resp.iter_bytes.return_value = iter(chunks)
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    def test_logs_stdout_path(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_CONTAINER
        cm = self._stream_ctx([b"hello from stdout"])
        fake_client.stream.return_value = cm
        service = ContainerService(fake_client)

        with service.stream_logs("501") as response:
            chunks = list(response.iter_bytes())

        assert chunks == [b"hello from stdout"]
        fake_client.stream.assert_called_once_with("GET", "/xapi/containers/501/logs/stdout")

    def test_logs_stderr_path(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_CONTAINER
        cm = self._stream_ctx([b"an error occurred"])
        fake_client.stream.return_value = cm
        service = ContainerService(fake_client)

        with service.stream_logs("501", stream="stderr"):
            pass

        fake_client.stream.assert_called_once_with("GET", "/xapi/containers/501/logs/stderr")

    def test_logs_empty_body_yields_no_chunks(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_CONTAINER
        cm = self._stream_ctx([])
        fake_client.stream.return_value = cm
        service = ContainerService(fake_client)

        with service.stream_logs("501") as response:
            chunks = list(response.iter_bytes())

        assert chunks == []

    def test_logs_nonexistent_container_raises_not_found_without_calling_stream(
        self, fake_client
    ) -> None:
        """A bad ID is caught by the existence check (GET /xapi/containers/{id},
        which genuinely answers 404) before ever hitting the logs route --
        verified live, logs/stdout answers 500 for a nonexistent container,
        which would otherwise walk the retry ladder.
        """
        fake_client.get_json.side_effect = ResourceNotFoundError("container", "999")
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.stream_logs("999")

        fake_client.stream.assert_not_called()


class TestResolveWrapper:
    """ContainerService.resolve_wrapper is a thin passthrough to CommandService."""

    def test_delegates_to_command_service(self, fake_client) -> None:
        injected = MagicMock()
        injected.resolve_wrapper.return_value = (34, SAMPLE_WRAPPER)
        service = ContainerService(fake_client, command_service=injected)

        result = service.resolve_wrapper("dcm2niix-scan")

        assert result == (34, SAMPLE_WRAPPER)
        injected.resolve_wrapper.assert_called_once_with("dcm2niix-scan")


class TestBuildLaunchParams:
    """ContainerService.build_launch_params -- pure, no HTTP calls."""

    def test_no_experiment_returns_params_unchanged(self, fake_client) -> None:
        service = ContainerService(fake_client)

        result = service.build_launch_params(SAMPLE_WRAPPER, {"greeting": "hi"})

        assert result == {"greeting": "hi"}

    def test_experiment_maps_to_session_input_name(self, fake_client) -> None:
        service = ContainerService(fake_client)

        result = service.build_launch_params(SAMPLE_WRAPPER, {}, experiment="XNAT_E00001")

        assert result == {"session": "XNAT_E00001"}

    def test_experiment_preserves_other_params(self, fake_client) -> None:
        service = ContainerService(fake_client)

        result = service.build_launch_params(
            SAMPLE_WRAPPER, {"greeting": "hi"}, experiment="XNAT_E00001"
        )

        assert result == {"greeting": "hi", "session": "XNAT_E00001"}

    def test_experiment_conflicting_param_key_raises(self, fake_client) -> None:
        service = ContainerService(fake_client)

        with pytest.raises(InputValidationError):
            service.build_launch_params(
                SAMPLE_WRAPPER, {"session": "XNAT_E99999"}, experiment="XNAT_E00001"
            )

    def test_no_session_input_raises(self, fake_client) -> None:
        wrapper = {"id": 1, "name": "no-session", "external-inputs": []}
        service = ContainerService(fake_client)

        with pytest.raises(InputValidationError):
            service.build_launch_params(wrapper, {}, experiment="XNAT_E00001")

    def test_ambiguous_session_inputs_raises(self, fake_client) -> None:
        wrapper = {
            "id": 1,
            "name": "two-sessions",
            "external-inputs": [
                {"name": "session1", "type": "Session"},
                {"name": "session2", "type": "Session"},
            ],
        }
        service = ContainerService(fake_client)

        with pytest.raises(InputValidationError):
            service.build_launch_params(wrapper, {}, experiment="XNAT_E00001")


class TestPreflightLaunch:
    """ContainerService.preflight_launch -- the shared dry-run/execution preflight."""

    def test_checks_project_exists_then_builds_params(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory(
            {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
        )
        service = ContainerService(fake_client)

        result = service.preflight_launch("PROJ01", SAMPLE_WRAPPER, {}, experiment="XNAT_E00001")

        assert result == {"session": "XNAT_E00001"}
        fake_client.get.assert_called_once()

    def test_nonexistent_project_raises(self, fake_client, response_factory) -> None:
        fake_client.get.return_value = response_factory({"ResultSet": {"Result": []}})
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.preflight_launch("BADPROJ", SAMPLE_WRAPPER, {})


class TestLaunch:
    """ContainerService.launch -- verified live: POST .../wrappers/{id}/launch, JSON body."""

    def test_launch_posts_params_as_json_body(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(
            {
                "status": "success",
                "params": {"session": "XNAT_E00001"},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "99",
            }
        )
        service = ContainerService(fake_client)

        result = service.launch("PROJ01", 34, {"session": "XNAT_E00001"})

        assert result["workflow-id"] == "99"
        fake_client.post.assert_called_once_with(
            "/xapi/projects/PROJ01/wrappers/34/launch", json={"session": "XNAT_E00001"}
        )

    def test_launch_non_dict_response_raises(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(["not", "a", "dict"])
        service = ContainerService(fake_client)

        with pytest.raises(XNATCtlError):
            service.launch("PROJ01", 34, {})


class TestKillContainer:
    """ContainerService.kill_container -- verified live: POST .../containers/{id}/kill."""

    def test_kill_posts_expected_path(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(status_code=200)
        service = ContainerService(fake_client)

        service.kill_container("501")

        fake_client.post.assert_called_once_with("/xapi/containers/501/kill")

    def test_kill_nonexistent_container_raises_not_found(self, fake_client) -> None:
        fake_client.post.side_effect = ResourceNotFoundError("container", "999")
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.kill_container("999")
