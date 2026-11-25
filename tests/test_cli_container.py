from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
from conftest import AuthenticatedCLI, make_response

from xnatctl.cli import container as container_cli
from xnatctl.core.exceptions import PermissionDeniedError, ResourceNotFoundError, XNATCtlError

SAMPLE_CONTAINER = {
    "id": "501",
    "command-id": 12,
    "wrapper-id": 34,
    "status": "Complete",
    "project": "PROJ01",
    "user-id": "jsmith",
}

SAMPLE_COMMAND = {
    "id": 12,
    "name": "dcm2niix",
    "xnat": [
        {
            "id": 34,
            "name": "dcm2niix-scan",
            "description": "Convert a scan",
            "contexts": ["xnat:imageSessionData"],
            "external-inputs": [{"name": "session", "type": "Session", "required": True}],
        }
    ],
}

PROJECT_FOUND = {"ResultSet": {"Result": [{"ID": "TESTPROJ"}]}}
PROJECT_NOT_FOUND = {"ResultSet": {"Result": []}}


def _stream_ctx(chunks: list[bytes]) -> MagicMock:
    resp = MagicMock()
    resp.iter_bytes.return_value = iter(chunks)
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def _stream_body_ctx(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class _Seq:
    def __init__(self, values: list[Any]) -> None:
        self._values = list(values)

    def __call__(self) -> Any:
        return self._values.pop(0)


def _dispatch_get_json(mapping: dict[str, Any]):
    def _dispatch(path: str, **_kwargs: Any) -> Any:
        if path not in mapping:
            raise AssertionError(f"unexpected get_json call: {path}")
        value = mapping[path]
        return value() if isinstance(value, _Seq) else value

    return _dispatch


class TestStartedDisplay:
    def test_earliest_history_entry_wins_across_a_dst_change(self) -> None:
        from xnatctl.cli.container import _started_display

        earlier = "2026-11-01T01:50:00.000-0400"
        later = "2026-11-01T01:10:00.000-0500"
        assert later < earlier

        row = {
            "history": [
                {"status": "Complete", "time-recorded": later},
                {"status": "Created", "time-recorded": earlier},
            ]
        }
        assert _started_display(row) == earlier

    def test_unparseable_stamps_never_outrank_a_real_one(self) -> None:
        from xnatctl.cli.container import _started_display

        row = {
            "history": [
                {"time-recorded": "not-a-timestamp"},
                {"time-recorded": "2026-08-20T14:03:00.000-0400"},
            ]
        }
        assert _started_display(row) == "2026-08-20T14:03:00.000-0400"

    def test_falls_back_to_status_time_then_placeholder(self) -> None:
        from xnatctl.cli.container import _UNKNOWN, _started_display

        assert _started_display({"status-time": "2026-08-20T14:03:00.000-0400"}) == (
            "2026-08-20T14:03:00.000-0400"
        )
        assert _started_display({"history": []}) == _UNKNOWN
        assert _started_display({"history": None}) == _UNKNOWN
        assert _started_display({}) == _UNKNOWN


class TestContainerList:
    def test_list_uses_profile_default_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_CONTAINER]

        result = authenticated_cli.invoke(["container", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with(
            "/xapi/projects/TESTPROJ/containers"
        )
        assert "Complete" in result.output

    def test_list_explicit_project_overrides_default(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = []

        result = authenticated_cli.invoke(["container", "list", "-P", "OTHERPROJ"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with(
            "/xapi/projects/OTHERPROJ/containers"
        )

    def test_list_falls_back_to_site_wide_with_no_project(self, authenticated_cli_factory) -> None:
        cli = authenticated_cli_factory(default_project=None)
        cli.client.get_json.return_value = [SAMPLE_CONTAINER]

        result = cli.invoke(["container", "list"])

        assert result.exit_code == 0
        cli.client.get_json.assert_called_once_with("/xapi/containers")

    def test_list_filters_by_status(self, authenticated_cli: AuthenticatedCLI) -> None:
        running = dict(SAMPLE_CONTAINER, id="502", status="Running")
        authenticated_cli.client.get_json.return_value = [SAMPLE_CONTAINER, running]

        result = authenticated_cli.invoke(["container", "list", "--status", "Running", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "502"

    def test_list_shows_placeholder_when_no_started_field(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_CONTAINER]

        result = authenticated_cli.invoke(["container", "list"])

        assert result.exit_code == 0
        assert "-" in result.output

    def test_list_derives_started_from_earliest_history_entry(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        row = dict(
            SAMPLE_CONTAINER,
            history=[
                {"status": "Complete", "time-recorded": "2026-08-20T14:03:00.000-0400"},
                {"status": "Created", "time-recorded": "2026-08-20T14:00:00.000-0400"},
            ],
        )
        authenticated_cli.client.get_json.return_value = [row]

        result = authenticated_cli.invoke(["container", "list", "-o", "json"])

        assert result.exit_code == 0
        rows = json.loads(result.output)
        assert rows[0]["started-display"] == "2026-08-20T14:00:00.000-0400"

    def test_list_json_output_does_not_lose_raw_fields(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_CONTAINER]

        result = authenticated_cli.invoke(["container", "list", "-o", "json"])

        assert result.exit_code == 0
        assert '"user-id": "jsmith"' in result.output


class TestContainerShow:
    def test_show_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER

        result = authenticated_cli.invoke(["container", "show", "501"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/containers/501")

    def test_show_accepts_docker_id_string(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER

        result = authenticated_cli.invoke(["container", "show", "weu6k9c3f1a2"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/containers/weu6k9c3f1a2")


class TestBinaryStdout:
    def test_a_binary_stdout_is_written_to_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", out)

        assert container_cli._binary_stdout() is out

    def test_a_text_stdout_is_written_through_its_buffer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = io.TextIOWrapper(io.BytesIO())
        monkeypatch.setattr(sys, "stdout", out)

        assert container_cli._binary_stdout() is out.buffer

    def test_a_text_only_stdout_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        with pytest.raises(click.ClickException, match="does not accept bytes"):
            container_cli._binary_stdout()


class TestContainerLogs:
    def test_logs_stdout(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_ctx([b"hello from stdout"])

        result = authenticated_cli.invoke(["container", "logs", "501"])

        assert result.exit_code == 0
        authenticated_cli.client.stream.assert_called_once_with(
            "GET", "/xapi/containers/501/logs/stdout"
        )
        assert result.output == "hello from stdout"

    def test_logs_stderr(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_ctx([b"an error occurred"])

        result = authenticated_cli.invoke(["container", "logs", "501", "--stderr"])

        assert result.exit_code == 0
        authenticated_cli.client.stream.assert_called_once_with(
            "GET", "/xapi/containers/501/logs/stderr"
        )
        assert result.output == "an error occurred"

    def test_logs_empty_body_prints_nothing(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_ctx([])

        result = authenticated_cli.invoke(["container", "logs", "501"])

        assert result.exit_code == 0
        assert result.output == ""

    def test_logs_body_already_ending_in_newline_gains_no_second_one(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_ctx([b"line one\n"])

        result = authenticated_cli.invoke(["container", "logs", "501"])

        assert result.exit_code == 0
        assert result.output == "line one\n"

    def test_logs_nonexistent_container_reports_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("container", "999")

        result = authenticated_cli.invoke(["container", "logs", "999"])

        assert result.exit_code != 0
        authenticated_cli.client.stream.assert_not_called()


class TestContainerLogsFollow:
    def test_follow_stops_at_terminal_status_after_one_poll(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "0.01"]
        )

        assert result.exit_code == 0
        assert result.output == "hello"

    def test_follow_prints_only_new_bytes_across_polls(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        running = dict(SAMPLE_CONTAINER, status="Running")
        complete = dict(SAMPLE_CONTAINER, status="Complete")
        authenticated_cli.client.get_json.side_effect = [
            running,
            running,
            complete,
            complete,
        ]
        authenticated_cli.client.stream.side_effect = [
            _stream_body_ctx(b"hello"),
            _stream_body_ctx(b"hello world"),
        ]

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "0.01"]
        )

        assert result.exit_code == 0
        assert result.output == "hello world"

    def test_follow_nonexistent_container_reports_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("container", "999")

        result = authenticated_cli.invoke(["container", "logs", "999", "--follow"])

        assert result.exit_code != 0
        authenticated_cli.client.stream.assert_not_called()

    def test_follow_negative_interval_rejected_before_any_call(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = dict(SAMPLE_CONTAINER, status="Running")
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "-1"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.stream.assert_not_called()


class TestContainerKill:
    def test_kill_happy_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(["container", "kill", "501", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with("/xapi/containers/501/kill")

    def test_kill_declined_prompt_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["container", "kill", "501"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_kill_dry_run_checks_existence_without_mutating(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER

        result = authenticated_cli.invoke(["container", "kill", "501", "--dry-run"])

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/containers/501")
        authenticated_cli.client.post.assert_not_called()

    def test_kill_dry_run_nonexistent_container_still_raises(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("container", "999")

        result = authenticated_cli.invoke(["container", "kill", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_kill_nonexistent_container_reports_not_found(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.post.side_effect = ResourceNotFoundError("container", "999")

        result = authenticated_cli.invoke(["container", "kill", "999", "--yes"])

        assert result.exit_code != 0


class TestContainerLaunch:
    def test_launch_by_wrapper_id_maps_experiment_to_session_input(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {"session": "XNAT_E00001"},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "To be assigned",
            }
        )

        result = authenticated_cli.invoke(["container", "launch", "34", "-E", "XNAT_E00001"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/TESTPROJ/wrappers/34/launch", json={"session": "XNAT_E00001"}
        )

    def test_launch_by_wrapper_name_with_param(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {"greeting": "hi"},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "1",
            }
        )

        result = authenticated_cli.invoke(
            ["container", "launch", "dcm2niix-scan", "--param", "greeting=hi"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/TESTPROJ/wrappers/34/launch", json={"greeting": "hi"}
        )

    def test_launch_dry_run_no_post(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)

        result = authenticated_cli.invoke(
            ["container", "launch", "34", "-E", "XNAT_E00001", "--dry-run"]
        )

        assert result.exit_code == 0
        assert "DRY-RUN" in result.output
        authenticated_cli.client.post.assert_not_called()

    def test_launch_nonexistent_wrapper_id_rejected_client_side(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )

        result = authenticated_cli.invoke(["container", "launch", "999"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_launch_nonexistent_project_rejected_before_post(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_NOT_FOUND)

        result = authenticated_cli.invoke(["container", "launch", "34", "-P", "BADPROJ"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_launch_duplicate_param_key_rejected(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["container", "launch", "34", "--param", "greeting=hi", "--param", "greeting=bye"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_launch_param_without_equals_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(["container", "launch", "34", "--param", "greeting"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_launch_experiment_conflicting_with_param_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)

        result = authenticated_cli.invoke(
            [
                "container",
                "launch",
                "34",
                "-E",
                "XNAT_E00001",
                "--param",
                "session=XNAT_E99999",
            ]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_launch_wait_polls_until_terminal_status(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        launched = dict(SAMPLE_CONTAINER, id="777", status="Complete")
        launched["workflow-id"] = "42"
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {
                "/xapi/commands": [SAMPLE_COMMAND],
                "/xapi/projects/TESTPROJ/containers": _Seq([[], [launched]]),
                "/xapi/containers/777": launched,
            }
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "42",
            }
        )

        result = authenticated_cli.invoke(["container", "launch", "34", "--wait", "--timeout", "5"])

        assert result.exit_code == 0
        assert "Complete" in result.output

    def test_launch_wait_snapshot_taken_before_launch_not_after(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        launched = dict(SAMPLE_CONTAINER, id="777", status="Complete", inputs=[])
        launched["workflow-id"] = "99"
        launched["user-id"] = "user"  # the test client's whoami()

        post_has_fired = False

        def get_json_dispatch(path: str, **_kwargs: Any) -> Any:
            if path == "/xapi/commands":
                return [SAMPLE_COMMAND]
            if path == "/xapi/projects/TESTPROJ/containers":
                return [launched] if post_has_fired else []
            if path == "/xapi/containers/777":
                return launched
            raise AssertionError(f"unexpected get_json call: {path}")

        def post_dispatch(_path: str, **_kwargs: Any) -> Any:
            nonlocal post_has_fired
            post_has_fired = True
            return make_response(
                {
                    "status": "success",
                    "params": {},
                    "command-id": 12,
                    "wrapper-id": 34,
                    "workflow-id": "To be assigned",
                }
            )

        authenticated_cli.client.get_json.side_effect = get_json_dispatch
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.side_effect = post_dispatch

        result = authenticated_cli.invoke(["container", "launch", "34", "--wait", "--timeout", "5"])

        assert result.exit_code == 0
        assert "Complete" in result.output

    def test_launch_failure_report_exits_nonzero_with_message(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {"/xapi/commands": [SAMPLE_COMMAND]}
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {"status": "failure", "message": "Docker unavailable"}
        )

        result = authenticated_cli.invoke(["container", "launch", "34", "-E", "XNAT_E00001"])

        assert result.exit_code != 0
        assert "Docker unavailable" in result.output

    def test_launch_negative_timeout_rejected_before_mutation(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {
                "/xapi/commands": [SAMPLE_COMMAND],
                "/xapi/projects/TESTPROJ/containers": [],
            }
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "To be assigned",
            }
        )

        result = authenticated_cli.invoke(
            ["container", "launch", "34", "--wait", "--timeout", "-1"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()


SCAN_WRAPPER_LABEL = "DICOM to NIfTI (KCNI default)"
SCAN_LAUNCH_GET = {
    "/data/projects/TESTPROJ": PROJECT_FOUND,
    "/data/projects/TESTPROJ/experiments/TESTPROJ_S1_MR": {
        "items": [
            {
                "meta": {"xsi:type": "xnat:mrSessionData"},
                "data_fields": {"ID": "XNAT_E00001", "label": "TESTPROJ_S1_MR"},
            }
        ]
    },
}
SCAN_LAUNCH_GET_JSON = {
    "/data/experiments/XNAT_E00001/scans/5": {
        "items": [{"meta": {"xsi:type": "xnat:mrScanData"}, "data_fields": {"ID": "5"}}]
    },
    "/xapi/projects/TESTPROJ/commands/available": [
        {
            "wrapper-id": 117,
            "wrapper-name": SCAN_WRAPPER_LABEL,
            "wrapper-label": SCAN_WRAPPER_LABEL,
            "enabled": True,
            "root-element-name": "scan",
        }
    ],
}


def _dispatch_get(mapping: dict[str, Any]):
    """A `client.get` side_effect that routes by exact path to a JSON response."""

    def _dispatch(path: str, **_kwargs: Any) -> Any:
        if path not in mapping:
            raise AssertionError(f"unexpected get call: {path}")
        return make_response(mapping[path])

    return _dispatch


class TestContainerLaunchScan:
    """`container launch --scan`: what a project Member sees in the UI is all it needs.

    The wrapper is matched among those the project has enabled for the
    scan's own type, through the Run menu's own route (`commands/available`,
    Read access) -- never `GET /xapi/commands`, which the dispatchers here
    would reject as an unexpected call.
    """

    def _arrange(self, cli: AuthenticatedCLI) -> None:
        cli.client.get.side_effect = _dispatch_get(SCAN_LAUNCH_GET)
        cli.client.get_json.side_effect = _dispatch_get_json(SCAN_LAUNCH_GET_JSON)
        cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {"scan": "/archive/experiments/XNAT_E00001/scans/5"},
                "command-id": 108,
                "wrapper-id": 117,
                "workflow-id": "To be assigned",
            }
        )

    def test_launch_by_run_menu_label_and_session_label(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        self._arrange(authenticated_cli)

        result = authenticated_cli.invoke(
            ["container", "launch", SCAN_WRAPPER_LABEL, "-E", "TESTPROJ_S1_MR", "--scan", "5"]
        )

        assert result.exit_code == 0, result.output
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/TESTPROJ/wrappers/117/launch",
            json={"scan": "/archive/experiments/XNAT_E00001/scans/5"},
        )

    def test_dry_run_names_the_wrapper_without_posting(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        self._arrange(authenticated_cli)

        result = authenticated_cli.invoke(
            [
                "container",
                "launch",
                SCAN_WRAPPER_LABEL,
                "-E",
                "TESTPROJ_S1_MR",
                "--scan",
                "5",
                "--dry-run",
            ]
        )

        assert result.exit_code == 0, result.output
        assert "DRY-RUN" in result.output
        assert "117" in result.output
        assert SCAN_WRAPPER_LABEL in result.output
        authenticated_cli.client.post.assert_not_called()

    def test_post_uses_the_wrappers_own_input_name(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        self._arrange(authenticated_cli)
        get_json = dict(SCAN_LAUNCH_GET_JSON)
        get_json["/xapi/projects/TESTPROJ/commands/available"] = [
            dict(
                get_json["/xapi/projects/TESTPROJ/commands/available"][0],
                **{"root-element-name": "image"},
            )
        ]
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(get_json)

        result = authenticated_cli.invoke(
            ["container", "launch", SCAN_WRAPPER_LABEL, "-E", "TESTPROJ_S1_MR", "--scan", "5"]
        )

        assert result.exit_code == 0, result.output
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/TESTPROJ/wrappers/117/launch",
            json={"image": "/archive/experiments/XNAT_E00001/scans/5"},
        )

    def test_accession_id_works_as_the_session(self, authenticated_cli: AuthenticatedCLI) -> None:
        self._arrange(authenticated_cli)
        by_accession = dict(SCAN_LAUNCH_GET)
        by_accession["/data/projects/TESTPROJ/experiments/XNAT_E00001"] = by_accession.pop(
            "/data/projects/TESTPROJ/experiments/TESTPROJ_S1_MR"
        )
        authenticated_cli.client.get.side_effect = _dispatch_get(by_accession)

        result = authenticated_cli.invoke(
            ["container", "launch", SCAN_WRAPPER_LABEL, "-E", "XNAT_E00001", "--scan", "5"]
        )

        assert result.exit_code == 0, result.output
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/projects/TESTPROJ/wrappers/117/launch",
            json={"scan": "/archive/experiments/XNAT_E00001/scans/5"},
        )

    def test_dry_run_with_wait_previews_params_and_touches_no_container(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """The dispatchers reject the container routes --wait would poll."""
        self._arrange(authenticated_cli)

        result = authenticated_cli.invoke(
            [
                "container",
                "launch",
                SCAN_WRAPPER_LABEL,
                "-E",
                "TESTPROJ_S1_MR",
                "--scan",
                "5",
                "--dry-run",
                "--wait",
            ]
        )

        assert result.exit_code == 0, result.output
        assert "{'scan': '/archive/experiments/XNAT_E00001/scans/5'}" in " ".join(
            result.output.split()
        )
        authenticated_cli.client.post.assert_not_called()

    def test_scan_without_experiment_is_a_usage_error(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            ["container", "launch", SCAN_WRAPPER_LABEL, "--scan", "5"]
        )

        assert result.exit_code == 2
        assert "-E" in result.output
        authenticated_cli.client.get.assert_not_called()
        authenticated_cli.client.post.assert_not_called()

    def test_unknown_pipeline_rejected_before_post(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        self._arrange(authenticated_cli)

        result = authenticated_cli.invoke(
            ["container", "launch", "No such pipeline", "-E", "TESTPROJ_S1_MR", "--scan", "5"]
        )

        assert result.exit_code != 0
        assert SCAN_WRAPPER_LABEL in " ".join(result.output.split())
        authenticated_cli.client.post.assert_not_called()


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Replace the module's clock so --wait reaches its deadline without real waiting.

    Returns the function that advances it, for a test that lets time pass
    outside ``sleep`` (a slow request).
    """
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(
        container_cli, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    )
    return sleep


def _scan_container(
    container_id: str, scan_uri: str, *, workflow_id: str = "1", user: str = "user"
) -> dict[str, Any]:
    """A container row for wrapper 117, its `inputs` shaped as verified live (type/name/value).

    ``user`` defaults to the test client's ``whoami()`` username.
    """
    return {
        "id": container_id,
        "wrapper-id": 117,
        "status": "Complete",
        "workflow-id": workflow_id,
        "user-id": user,
        "inputs": [
            {"type": "raw", "name": "scan", "value": scan_uri},
            {"type": "wrapper-external", "name": "scan", "value": scan_uri},
        ],
    }


class TestContainerLaunchWaitCorrelation:
    """`--wait` after a placeholder workflow-id must follow OUR container, not a neighbour's.

    In a room where everyone launches the same wrapper in the same project,
    "the first new container for this wrapper" is often someone else's.
    """

    OURS = "/archive/experiments/XNAT_E00001/scans/5"

    def _run(
        self,
        cli: AuthenticatedCLI,
        before: list[Any],
        after: list[Any],
        *,
        workflow_id: Any = "To be assigned",
        later: list[list[Any]] | None = None,
    ) -> Any:
        """Run the launch; ``after`` is the first post-launch listing, ``later`` the next ones."""
        post_has_fired = False
        get_json = dict(SCAN_LAUNCH_GET_JSON)
        polls = [after, *(later or [])]
        rows = [r for listing in [before, *polls] for r in listing if isinstance(r, dict)]
        details = {f"/xapi/containers/{row['id']}": row for row in rows}

        def get_json_dispatch(path: str, **_kwargs: Any) -> Any:
            if path == "/xapi/projects/TESTPROJ/containers":
                if not post_has_fired:
                    return before
                return polls.pop(0) if len(polls) > 1 else polls[0]
            if path in details:
                return details[path]
            if path in get_json:
                return get_json[path]
            raise AssertionError(f"unexpected get_json call: {path}")

        def post_dispatch(_path: str, **_kwargs: Any) -> Any:
            nonlocal post_has_fired
            post_has_fired = True
            return make_response(
                {
                    "status": "success",
                    "params": {"scan": self.OURS},
                    "command-id": 108,
                    "wrapper-id": 117,
                    "workflow-id": workflow_id,
                }
            )

        cli.client.get.side_effect = _dispatch_get(SCAN_LAUNCH_GET)
        cli.client.get_json.side_effect = get_json_dispatch
        cli.client.post.side_effect = post_dispatch
        return cli.invoke(
            [
                "container",
                "launch",
                SCAN_WRAPPER_LABEL,
                "-E",
                "TESTPROJ_S1_MR",
                "--scan",
                "5",
                "--wait",
                "--timeout",
                "5",
                "-o",
                "json",
            ]
        )

    def test_follows_the_new_container_with_our_inputs(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        neighbour = _scan_container("801", "/archive/experiments/XNAT_E00002/scans/5")
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[neighbour, ours])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_placeholder_workflow_id_on_an_old_row_is_not_a_match(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        stale = _scan_container("700", self.OURS, workflow_id="To be assigned")
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[stale], after=[stale, ours])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_extra_inputs_are_not_a_match(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        superset = _scan_container("801", self.OURS)
        superset["inputs"].append({"type": "raw", "name": "deface", "value": "true"})

        result = self._run(authenticated_cli, before=[], after=[superset])

        assert result.exit_code != 0
        assert "could be tied to this launch" in " ".join(result.output.split())
        assert "801" in result.output

    def test_two_identical_matches_are_refused_not_guessed(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        first = _scan_container("801", self.OURS)
        second = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[first, second])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "801, 802" in output
        assert "cannot tell" in output

    @pytest.mark.parametrize("workflow_id", [None, "", True], ids=["null", "empty", "boolean"])
    def test_launch_without_a_real_workflow_id_ignores_old_rows_without_one(
        self, authenticated_cli: AuthenticatedCLI, workflow_id: Any
    ) -> None:
        old = _scan_container("700", "/archive/experiments/XNAT_E00009/scans/1")
        old["workflow-id"] = None
        ours = _scan_container("802", self.OURS)

        result = self._run(
            authenticated_cli, before=[old], after=[old, ours], workflow_id=workflow_id
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_raw_input_with_a_non_string_name_is_skipped_not_a_crash(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        odd = _scan_container("801", self.OURS)
        odd["inputs"].append({"type": "raw", "name": ["scan"], "value": self.OURS})
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[odd, ours])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_another_users_identical_launch_seen_first_is_not_followed(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        """A neighbour running the same request shows up one poll before ours."""
        theirs = _scan_container("801", self.OURS, user="neighbour")
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[theirs], later=[[theirs, ours]])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_login_is_compared_exactly_as_container_service_does(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        """A differently cased user-id is another account to Container Service."""
        other_case = _scan_container("802", self.OURS, user="USER")

        result = self._run(authenticated_cli, before=[], after=[other_case])

        assert result.exit_code != 0
        assert "(802)" in " ".join(result.output.split())

    @pytest.mark.parametrize(
        "login",
        [None, ""],
        ids=["no-login", "empty-login"],
    )
    def test_unconfirmed_login_reports_the_launch_as_queued(
        self, authenticated_cli: AuthenticatedCLI, login: str | None
    ) -> None:
        """whoami()'s configured-username fallback ("user" here) is not used in its place."""
        authenticated_cli.client.current_username.return_value = login
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[ours])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "The launch was queued" in output
        assert "did not confirm which user is logged in" in output

    def test_login_lookup_failure_after_the_launch_reports_it_as_queued(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.current_username.side_effect = XNATCtlError("denied")
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[ours])

        assert result.exit_code != 0
        assert "The launch was queued" in " ".join(result.output.split())
        assert authenticated_cli.client.post.call_count == 1

    def test_login_read_on_a_re_established_session_is_not_trusted(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """A 401 re-login during the lookup may be another account's credentials."""
        client = authenticated_cli.client
        client.session_token = "launching-session"

        def relogin_as_someone_else() -> str:
            client.session_token = "new-session"
            return "someone-else"

        client.current_username.side_effect = relogin_as_someone_else
        theirs = _scan_container("801", self.OURS, user="someone-else")

        result = self._run(authenticated_cli, before=[], after=[theirs])

        assert result.exit_code != 0
        assert "did not confirm which user is logged in" in " ".join(result.output.split())
        fetched = [c.args[0] for c in client.get_json.call_args_list]
        assert "/xapi/containers/801" not in fetched

    def test_denied_login_lookup_keeps_its_exit_code(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.current_username.side_effect = PermissionDeniedError("denied")
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[ours])

        assert result.exit_code == 6  # ExitCode.PERMISSION_ERROR
        assert "The launch was queued" in " ".join(result.output.split())

    def test_slow_login_lookup_counts_against_the_timeout(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: Callable[[float], None]
    ) -> None:
        def slow_lookup() -> str:
            fake_clock(60)
            return "user"

        authenticated_cli.client.current_username.side_effect = slow_lookup
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[], after=[ours])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "timed out" in output
        assert "The launch itself was queued" in output
        fetched = [c.args[0] for c in authenticated_cli.client.get_json.call_args_list]
        assert "/xapi/containers/802" not in fetched

    def test_real_workflow_id_needs_no_login(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.current_username.side_effect = XNATCtlError("denied")
        ours = _scan_container("802", self.OURS, workflow_id="42")

        result = self._run(authenticated_cli, before=[], after=[ours], workflow_id="42")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"
        authenticated_cli.client.current_username.assert_not_called()

    def test_real_workflow_id_on_another_wrappers_row_is_not_a_match(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        other_wrapper = dict(
            _scan_container("801", self.OURS, workflow_id="42"), **{"wrapper-id": 99}
        )
        ours = _scan_container("802", self.OURS, workflow_id="42")

        result = self._run(
            authenticated_cli, before=[], after=[other_wrapper, ours], workflow_id="42"
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"

    def test_timeout_lists_unmatched_containers_from_every_poll(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        gone = _scan_container("803", "/archive/experiments/XNAT_E00002/scans/5")

        result = self._run(authenticated_cli, before=[], after=[gone], later=[[]])

        assert result.exit_code != 0
        assert "803" in result.output

    def test_malformed_listing_row_is_skipped(self, authenticated_cli: AuthenticatedCLI) -> None:
        ours = _scan_container("802", self.OURS)

        result = self._run(authenticated_cli, before=[None], after=[ours, None])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == "802"


class TestContainerLaunchWaitWithoutScan:
    """The input match also applies to the long-standing -E / --param path."""

    def _run(self, cli: AuthenticatedCLI, after: list[Any]) -> Any:
        post_has_fired = False
        details = {f"/xapi/containers/{row['id']}": row for row in after}

        def get_json_dispatch(path: str, **_kwargs: Any) -> Any:
            if path == "/xapi/commands":
                return [SAMPLE_COMMAND]
            if path == "/xapi/projects/TESTPROJ/containers":
                return after if post_has_fired else []
            if path in details:
                return details[path]
            raise AssertionError(f"unexpected get_json call: {path}")

        def post_dispatch(_path: str, **_kwargs: Any) -> Any:
            nonlocal post_has_fired
            post_has_fired = True
            return make_response(
                {
                    "status": "success",
                    "params": {"session": "XNAT_E00001"},
                    "command-id": 12,
                    "wrapper-id": 34,
                    "workflow-id": "To be assigned",
                }
            )

        cli.client.get_json.side_effect = get_json_dispatch
        cli.client.get.return_value = make_response(PROJECT_FOUND)
        cli.client.post.side_effect = post_dispatch
        return cli.invoke(
            ["container", "launch", "34", "-E", "XNAT_E00001", "--wait", "--timeout", "5"]
        )

    def test_follows_the_container_that_recorded_the_session(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        other = dict(SAMPLE_CONTAINER, id="801", inputs=[], **{"user-id": "user"})
        ours = dict(
            SAMPLE_CONTAINER,
            id="802",
            inputs=[{"type": "raw", "name": "session", "value": "XNAT_E00001"}],
            **{"user-id": "user"},
        )

        result = self._run(authenticated_cli, after=[other, ours])

        assert result.exit_code == 0, result.output
        authenticated_cli.client.get_json.assert_any_call("/xapi/containers/802")

    def test_timeout_names_the_new_containers_that_did_not_match(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        other = dict(SAMPLE_CONTAINER, id="801", inputs=[], **{"user-id": "user"})

        result = self._run(authenticated_cli, after=[other])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "timed out" in output
        assert "801" in output

    def test_row_without_inputs_is_not_a_parameterless_match(
        self, authenticated_cli: AuthenticatedCLI, fake_clock: None
    ) -> None:
        """A row that does not say what it was sent cannot match even a launch that sent nothing."""
        unknown = dict(SAMPLE_CONTAINER, id="801", **{"user-id": "user"})
        unknown.pop("inputs", None)
        authenticated_cli.client.get_json.side_effect = _dispatch_get_json(
            {
                "/xapi/commands": [SAMPLE_COMMAND],
                "/xapi/projects/TESTPROJ/containers": _Seq([[], [unknown], [unknown]]),
            }
        )
        authenticated_cli.client.get.return_value = make_response(PROJECT_FOUND)
        authenticated_cli.client.post.return_value = make_response(
            {
                "status": "success",
                "params": {},
                "command-id": 12,
                "wrapper-id": 34,
                "workflow-id": "To be assigned",
            }
        )

        result = authenticated_cli.invoke(["container", "launch", "34", "--wait", "--timeout", "5"])

        assert result.exit_code != 0
        output = " ".join(result.output.split())
        assert "timed out" in output
        assert "(801)" in output
        fetched = [c.args[0] for c in authenticated_cli.client.get_json.call_args_list]
        assert "/xapi/containers/801" not in fetched
