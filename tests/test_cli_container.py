from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from conftest import AuthenticatedCLI, make_response

from xnatctl.core.exceptions import ResourceNotFoundError

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
        launched = dict(SAMPLE_CONTAINER, id="777", status="Complete")
        launched["workflow-id"] = "99"

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
