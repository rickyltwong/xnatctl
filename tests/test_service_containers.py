"""Unit tests for ContainerService.

The container object could not be captured live (no Docker daemon in the
integration stack), but its field names ARE verified -- read out of the
shipped plugin jar's compiled DTOs (see services/containers.py's module
docstring). The sample row here uses a subset of that real field set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.services.commands import CommandService
from xnatctl.services.containers import ContainerService, ScanLaunch

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


AVAILABLE_PATH = "/xapi/projects/PROJ01/commands/available"
SCAN_LABEL = "DICOM to NIfTI (KCNI default)"


def _available_row(
    wrapper_id: int,
    label: str,
    *,
    name: str | None = None,
    enabled: bool = True,
    root: str = "scan",
) -> dict[str, Any]:
    """One ``GET .../commands/available`` row, using the key set verified live on CS 3.7.3."""
    return {
        "command-id": 108,
        "command-name": "nipop-convert",
        "wrapper-id": wrapper_id,
        "wrapper-name": name or f"wrapper-{wrapper_id}",
        "wrapper-label": label,
        "wrapper-description": "Convert this scan",
        "wrapper-contexts": ["xnat:mrScanData"],
        "enabled": enabled,
        "root-element-name": root,
    }


def _dispatch(mapping: dict[str, Any]):
    """A ``get``/``get_json`` side_effect that routes by exact path, failing on anything else."""

    def _route(path: str, **_kwargs: Any) -> Any:
        if path not in mapping:
            raise AssertionError(f"unexpected call: {path}")
        value = mapping[path]
        if isinstance(value, Exception):
            raise value
        return value

    return _route


class TestAvailableWrappers:
    """ContainerService.available_wrappers -- the Run menu's own source, Read access only."""

    def test_gets_project_scoped_route_for_the_type(self, fake_client) -> None:
        rows = [_available_row(117, SCAN_LABEL)]
        fake_client.get_json.return_value = rows
        service = ContainerService(fake_client)

        result = service.available_wrappers("PROJ01", "xnat:mrScanData")

        assert result == rows
        fake_client.get_json.assert_called_once_with(
            AVAILABLE_PATH, params={"xsiType": "xnat:mrScanData"}
        )

    @pytest.mark.parametrize(
        "body",
        [
            {"not": "a list"},
            [None],
            [{k: v for k, v in _available_row(117, SCAN_LABEL).items() if k != "enabled"}],
            [dict(_available_row(117, SCAN_LABEL), enabled="true")],
            [dict(_available_row(117, SCAN_LABEL), **{"wrapper-id": "117"})],
            [dict(_available_row(117, SCAN_LABEL), **{"wrapper-id": True})],
        ],
        ids=[
            "not-a-list",
            "null-row",
            "no-enabled",
            "string-enabled",
            "string-wrapper-id",
            "boolean-wrapper-id",
        ],
    )
    def test_unexpected_shape_raises(self, fake_client, body: Any) -> None:
        fake_client.get_json.return_value = body
        service = ContainerService(fake_client)

        with pytest.raises(XNATCtlError, match="commands/available"):
            service.available_wrappers("PROJ01", "xnat:mrScanData")


class TestResolveAvailableWrapper:
    """ContainerService.resolve_available_wrapper -- match among wrappers enabled in the project."""

    @pytest.mark.parametrize(
        "ref",
        [SCAN_LABEL, "nipop-scan", "117"],
        ids=["run-menu-label", "wrapper-name", "wrapper-id"],
    )
    def test_matches_label_name_or_id(self, fake_client, ref: str) -> None:
        row = _available_row(117, SCAN_LABEL, name="nipop-scan")
        fake_client.get_json.return_value = [_available_row(21, "Other"), row]
        service = ContainerService(fake_client)

        assert service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", ref) == row

    def test_digits_are_a_wrapper_id_never_a_label(self, fake_client) -> None:
        """Otherwise the ID the ambiguity error suggests could itself be ambiguous."""
        by_id = _available_row(117, "Convert")
        fake_client.get_json.return_value = [_available_row(42, "117", name="117"), by_id]
        service = ContainerService(fake_client)

        assert service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", "117") == by_id

    def test_non_ascii_digit_label_is_a_label(self, fake_client) -> None:
        """'²' passes str.isdigit() but is not a wrapper ID int() can read."""
        row = _available_row(42, "²")
        fake_client.get_json.return_value = [row]
        service = ContainerService(fake_client)

        assert service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", "²") == row

    def test_disabled_wrapper_with_same_label_is_not_a_conflict(self, fake_client) -> None:
        enabled = _available_row(117, SCAN_LABEL)
        fake_client.get_json.return_value = [_available_row(99, SCAN_LABEL, enabled=False), enabled]
        service = ContainerService(fake_client)

        assert service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", SCAN_LABEL) == enabled

    def test_only_disabled_match_is_not_found_and_lists_enabled(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _available_row(99, "Old converter", enabled=False),
            _available_row(117, SCAN_LABEL),
        ]
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError, match="Old converter") as excinfo:
            service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", "Old converter")

        assert excinfo.value.hint is not None
        assert f"{SCAN_LABEL!r} (ID 117)" in excinfo.value.hint

    def test_nothing_enabled_says_so(self, fake_client) -> None:
        fake_client.get_json.return_value = [_available_row(99, "Old", enabled=False)]
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError) as excinfo:
            service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", "Old")

        assert excinfo.value.hint is not None
        assert "No pipeline is enabled" in excinfo.value.hint

    def test_two_enabled_matches_are_ambiguous(self, fake_client) -> None:
        fake_client.get_json.return_value = [
            _available_row(75, "dcm2bids - session"),
            _available_row(96, "dcm2bids - session"),
        ]
        service = ContainerService(fake_client)

        with pytest.raises(InputValidationError, match=r"ambiguous.*75.*96"):
            service.resolve_available_wrapper("PROJ01", "xnat:mrScanData", "dcm2bids - session")


SESSION_PATH = "/data/projects/PROJ01/experiments/PROJ01_S1_CT"
SCAN_PATH = "/data/experiments/XNAT_E00001/scans/7"
PROJECT_FOUND_BODY = {"ResultSet": {"Result": [{"ID": "PROJ01"}]}}
SESSION_BODY = {
    "items": [
        {
            "meta": {"xsi:type": "xnat:ctSessionData"},
            "data_fields": {"ID": "XNAT_E00001", "label": "PROJ01_S1_CT", "project": "PROJ01"},
        }
    ]
}
# Deliberately not the mrScanData / "scan" pair of the workshop wrapper, so
# neither the scan type nor the wrapper's input name can be hard-coded.
SCAN_BODY = {
    "items": [{"meta": {"xsi:type": "xnat:ctScanData"}, "data_fields": {"ID": "7", "type": "CT"}}]
}
SCAN_URI = "/archive/experiments/XNAT_E00001/scans/7"


class TestPlanScanLaunch:
    """ContainerService.plan_scan_launch -- session label + scan ID to a launchable request."""

    def _service(self, fake_client, response_factory, *, scan_body: Any = SCAN_BODY):
        fake_client.get.side_effect = _dispatch(
            {
                "/data/projects/PROJ01": response_factory(PROJECT_FOUND_BODY),
                SESSION_PATH: response_factory(SESSION_BODY),
            }
        )
        fake_client.get_json.side_effect = _dispatch(
            {
                SCAN_PATH: scan_body,
                AVAILABLE_PATH: [_available_row(117, SCAN_LABEL, root="image")],
            }
        )
        return ContainerService(fake_client)

    def test_builds_scan_uri_under_the_wrappers_own_input_name(
        self, fake_client, response_factory
    ) -> None:
        service = self._service(fake_client, response_factory)

        plan = service.plan_scan_launch(
            "PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan="7"
        )

        assert plan == ScanLaunch(
            wrapper_id=117, wrapper_label=SCAN_LABEL, params={"image": SCAN_URI}
        )

    def test_looks_up_wrappers_for_the_scans_own_type(self, fake_client, response_factory) -> None:
        service = self._service(fake_client, response_factory)

        service.plan_scan_launch("PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan="7")

        fake_client.get_json.assert_any_call(AVAILABLE_PATH, params={"xsiType": "xnat:ctScanData"})

    def test_keeps_other_params(self, fake_client, response_factory) -> None:
        service = self._service(fake_client, response_factory)

        plan = service.plan_scan_launch(
            "PROJ01", SCAN_LABEL, {"deface": "false"}, experiment="PROJ01_S1_CT", scan="7"
        )

        assert plan.params == {"deface": "false", "image": SCAN_URI}

    def test_param_for_the_scan_input_conflicts(self, fake_client, response_factory) -> None:
        service = self._service(fake_client, response_factory)

        with pytest.raises(InputValidationError, match="image"):
            service.plan_scan_launch(
                "PROJ01", SCAN_LABEL, {"image": "/x"}, experiment="PROJ01_S1_CT", scan="7"
            )

    def test_missing_scan_raises_not_found(self, fake_client, response_factory) -> None:
        service = self._service(
            fake_client, response_factory, scan_body=ResourceNotFoundError("scan", "7")
        )

        with pytest.raises(ResourceNotFoundError, match="PROJ01_S1_CT/7"):
            service.plan_scan_launch("PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan="7")

    @pytest.mark.parametrize(
        "scan_body",
        [{"items": None}, {"items": []}, {"items": [{"meta": {}}]}, ["not", "an", "object"]],
        ids=["null-items", "no-items", "no-xsi-type", "not-an-object"],
    )
    def test_unexpected_scan_document_raises(
        self, fake_client, response_factory, scan_body: Any
    ) -> None:
        service = self._service(fake_client, response_factory, scan_body=scan_body)

        with pytest.raises(XNATCtlError, match="scans/7"):
            service.plan_scan_launch("PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan="7")

    def test_padded_scan_id_is_used_stripped(self, fake_client, response_factory) -> None:
        service = self._service(fake_client, response_factory)

        plan = service.plan_scan_launch(
            "PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan=" 7 "
        )

        assert plan.params == {"image": SCAN_URI}
        fake_client.get_json.assert_any_call(SCAN_PATH)

    def test_wrapper_without_input_name_names_the_endpoint(
        self, fake_client, response_factory
    ) -> None:
        service = self._service(fake_client, response_factory)
        row = _available_row(117, SCAN_LABEL)
        del row["root-element-name"]
        fake_client.get_json.side_effect = _dispatch({SCAN_PATH: SCAN_BODY, AVAILABLE_PATH: [row]})

        with pytest.raises(XNATCtlError, match="commands/available.*117"):
            service.plan_scan_launch("PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan="7")

    @pytest.mark.parametrize("scan", ["7 1", "7/1", ""], ids=["space", "slash", "empty"])
    def test_invalid_scan_id_rejected_before_any_request(self, fake_client, scan: str) -> None:
        service = ContainerService(fake_client)

        with pytest.raises(InputValidationError):
            service.plan_scan_launch("PROJ01", SCAN_LABEL, {}, experiment="PROJ01_S1_CT", scan=scan)

        fake_client.get.assert_not_called()
        fake_client.get_json.assert_not_called()

    def test_nonexistent_project_raises_before_session_lookup(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get.side_effect = _dispatch(
            {"/data/projects/BADPROJ": response_factory({"ResultSet": {"Result": []}})}
        )
        service = ContainerService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.plan_scan_launch("BADPROJ", SCAN_LABEL, {}, experiment="S", scan="7")

        fake_client.get_json.assert_not_called()


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
