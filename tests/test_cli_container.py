"""Tests for xnatctl CLI `container` commands."""

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

#: A command with an embedded wrapper carrying one Session-typed external
#: input -- resolvable by id (34) or name (dcm2niix-scan) via `wrapper
#: list`'s client-side derivation from `GET /xapi/commands`.
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

#: A project GET, in the `HierarchyService.extract_rows`-shaped envelope
#: `ProjectService.get` expects -- matches `TESTPROJ`, `authenticated_cli`'s
#: default project.
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
    """A `_dispatch_get_json` mapping value that returns successive queued responses.

    Plain (non-`_Seq`) mapping values are returned unchanged on every call
    to their path -- `_Seq` is only for a path polled more than once per
    test where each poll must see a different response.
    """

    def __init__(self, values: list[Any]) -> None:
        self._values = list(values)

    def __call__(self) -> Any:
        return self._values.pop(0)


def _dispatch_get_json(mapping: dict[str, Any]):
    """A `client.get_json` side_effect that routes by exact path.

    Args:
        mapping: path -> value to return for `get_json(path, ...)`, or a
            `_Seq` for a path that must return a different value on each
            successive call.
    """

    def _dispatch(path: str, **_kwargs: Any) -> Any:
        if path not in mapping:
            raise AssertionError(f"unexpected get_json call: {path}")
        value = mapping[path]
        return value() if isinstance(value, _Seq) else value

    return _dispatch


class TestStartedDisplay:
    """The derived "Started" value, which no single container field supplies."""

    def test_earliest_history_entry_wins_across_a_dst_change(self) -> None:
        """Ordering must compare instants, not strings.

        Container Service stamps history entries with a UTC offset, and the
        offset changes at a DST boundary. `01:10-0500` sorts BEFORE
        `01:50-0400` as text while being the later moment by twenty minutes,
        so a lexicographic `min` reports the wrong start time exactly when a
        run straddles the changeover.
        """
        from xnatctl.cli.container import _started_display

        earlier = "2026-11-01T01:50:00.000-0400"
        later = "2026-11-01T01:10:00.000-0500"
        assert later < earlier  # the trap: text order is the wrong order

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
    """Tests for `container list`."""

    def test_list_uses_profile_default_project(self, authenticated_cli: AuthenticatedCLI) -> None:
        """authenticated_cli's profile carries default_project=TESTPROJ."""
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
        """A row with no `history` and no `status-time` must not blank-render."""
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
    """Tests for `container show`."""

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
    """Tests for `container logs` -- streamed byte-exactly."""

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
        """Verified live: a bad ID's logs/stdout answers 500, not 404 -- the
        existence check against GET /xapi/containers/{id} (which does answer
        404) is what makes this a clean not-found instead of a retry-ladder
        walk ending in RetryExhaustedError.
        """
        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError("container", "999")

        result = authenticated_cli.invoke(["container", "logs", "999"])

        assert result.exit_code != 0
        authenticated_cli.client.stream.assert_not_called()


class TestContainerLogsFollow:
    """Tests for `container logs --follow` -- poll-and-refetch, not a true tail.

    See `_follow_logs`'s docstring in `cli/container.py`: `since` on
    `logSince` could not be verified content-wise, so this re-fetches the
    full log each poll via the already-verified plain `logs/{stream}` route
    and prints only the bytes not already written.
    """

    def test_follow_stops_at_terminal_status_after_one_poll(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER  # status: Complete
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
            running,  # stream_logs existence check, poll 1
            running,  # status check, poll 1
            complete,  # stream_logs existence check, poll 2
            complete,  # status check, poll 2
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
        """A negative --interval must be a clean usage error.

        The container existence/status checks are mocked to succeed so that
        --interval validation is the only thing standing between this
        command and a log fetch. Before the fix, --interval took a bare
        `float`, so this fetched the log once (`client.stream` WAS called)
        and only failed afterward on a raw `ValueError` out of
        `time.sleep(-1)`.
        """
        authenticated_cli.client.get_json.return_value = dict(SAMPLE_CONTAINER, status="Running")
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "-1"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.stream.assert_not_called()


class TestContainerKill:
    """Tests for `container kill` -- destructive: --yes/--dry-run via @confirm_destructive."""

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
        """--dry-run must run the same existence check execution would, not just echo."""
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
    """Tests for `container launch`.

    WRAPPER resolution goes through CommandService.resolve_wrapper (same as
    `wrapper` commands): `GET /xapi/commands`, derived client-side. The
    launch route itself does not validate the wrapper, project, or inputs
    server-side (verified live -- see ContainerService.launch), so these
    tests check the CLIENT-side rejections that stand in for it.
    """

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
        """The server would answer 200 "success" for this -- the client refuses first."""
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
        """Regression: the correlation snapshot must be taken BEFORE the launch fires.

        This is the common real-server case the bug report describes: the
        launch response carries the literal ``"To be assigned"`` placeholder
        (so the workflow-id path can never match -- the container's real,
        resolved workflow-id is never that string), and the container has
        already appeared in the listing by the time anything polls again.
        `get_json` here is stateful on whether `client.post` (the launch
        call) has fired yet, modeling that race directly instead of relying
        on mock call *order*, which can't distinguish a pre-launch snapshot
        from a post-launch one when both happen to be the first call to the
        path.

        Before the fix (snapshot taken after `service.launch()`), the very
        first listing call already showed the container, so it landed in
        `known_ids` and was permanently excluded from the "new container"
        fallback -- `--wait` polled to timeout even though the container was
        already `Complete`. This test fails with a timed-out `ClickException`
        (non-zero exit, no "Complete" in the output) against that code.
        """
        launched = dict(SAMPLE_CONTAINER, id="777", status="Complete")
        launched["workflow-id"] = "99"  # a resolved id, never the placeholder string

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
        """`LaunchReport.Failure` (declared on the plugin DTO, never observed live)
        must not be treated as a queued launch. Before the fix, any dict-shaped
        response was printed as "Launch queued: workflow-id=None ..." with
        exit code 0.
        """
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
        """A non-positive --timeout must be a clean usage error, not a queued
        launch followed by an immediate (already-past-deadline) timeout.

        Wrapper resolution and project lookup are mocked to succeed so that
        --timeout validation is the only thing standing between this command
        and a POST -- without that, a test that never reaches the mutation
        for an unrelated reason (an unmocked call blowing up first) would
        pass for the wrong reason. Before the fix, --timeout took a bare
        `int`, so this reached `service.launch()` (`client.post` WAS
        called) and only failed afterward on an already-past deadline.
        """
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
