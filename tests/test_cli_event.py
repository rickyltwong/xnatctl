"""Tests for xnatctl CLI `event` commands."""

from __future__ import annotations

import json

from conftest import AuthenticatedCLI, make_response

SAMPLE_SUBSCRIPTION = {
    "id": 5,
    "name": "cli-probe-1",
    "active": True,
    "action-key": (
        "org.nrg.xnat.eventservice.actions.EventServiceLoggingAction:"
        "org.nrg.xnat.eventservice.actions.EventServiceLoggingAction"
    ),
    "attributes": {"param1": "hi", "param2": "there"},
    "event-filter": {
        "reactorCriteriaHash": -1466181995,
        "event-type": "org.nrg.xnat.eventservice.events.ProjectEvent",
        "project-ids": [],
        "status": "CREATED",
    },
    "act-as-event-user": True,
    "subscription-owner": "admin",
    "valid": True,
    "created": "2026-08-25 11:03:40 UTC",
}

CREATE_PAYLOAD = {
    "name": "cli-probe-1",
    "active": True,
    "event-filter": {
        "event-type": "org.nrg.xnat.eventservice.events.ProjectEvent",
        "status": "CREATED",
        "project-ids": [],
    },
    "act-as-event-user": True,
    "action-key": SAMPLE_SUBSCRIPTION["action-key"],
    "attributes": {"param1": "hi", "param2": "there"},
}


class TestEventList:
    """Tests for `event list`."""

    def test_list_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_SUBSCRIPTION]

        result = authenticated_cli.invoke(["event", "list"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/subscriptions")
        assert "cli-probe-1" in result.output

    def test_list_quiet(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [SAMPLE_SUBSCRIPTION]

        result = authenticated_cli.invoke(["event", "list", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "5"

    def test_list_empty(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = []

        result = authenticated_cli.invoke(["event", "list"])

        assert result.exit_code == 0


class TestEventShow:
    """Tests for `event show`."""

    def test_show(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SUBSCRIPTION

        result = authenticated_cli.invoke(["event", "show", "5"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/subscription/5")
        assert "cli-probe-1" in result.output

    def test_show_not_found(self, authenticated_cli: AuthenticatedCLI) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999"
        )

        result = authenticated_cli.invoke(["event", "show", "999"])

        assert result.exit_code != 0


class TestEventCreate:
    """Tests for `event create` -- happy path, declined confirmation, dry-run, stdin."""

    def test_create_from_file(self, authenticated_cli: AuthenticatedCLI, tmp_path) -> None:
        payload_file = tmp_path / "subscription.json"
        payload_file.write_text(json.dumps(CREATE_PAYLOAD))
        authenticated_cli.client.post.return_value = make_response(
            text="cli-probe-1:5", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["event", "create", str(payload_file), "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/events/subscription", json=CREATE_PAYLOAD
        )
        assert "5" in result.output

    def test_create_from_stdin(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = make_response(
            text="cli-probe-1:5", content_type="text/plain"
        )

        result = authenticated_cli.invoke(
            ["event", "create", "-", "--yes"], input=json.dumps(CREATE_PAYLOAD)
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/events/subscription", json=CREATE_PAYLOAD
        )

    def test_create_stdin_without_yes_or_dry_run_rejected(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        result = authenticated_cli.invoke(
            ["event", "create", "-"], input=json.dumps(CREATE_PAYLOAD)
        )

        assert result.exit_code != 0
        assert "--yes or --dry-run" in result.output
        authenticated_cli.client.post.assert_not_called()

    def test_create_dry_run_no_http_call(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "subscription.json"
        payload_file.write_text(json.dumps(CREATE_PAYLOAD))

        result = authenticated_cli.invoke(["event", "create", str(payload_file), "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_create_prompt_abort_no_mutation(
        self, authenticated_cli: AuthenticatedCLI, tmp_path
    ) -> None:
        payload_file = tmp_path / "subscription.json"
        payload_file.write_text(json.dumps(CREATE_PAYLOAD))

        result = authenticated_cli.invoke(["event", "create", str(payload_file)], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()


class TestEventDelete:
    """Tests for `event delete` -- happy path, declined confirmation, dry-run."""

    def test_delete(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.delete.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["event", "delete", "5", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.delete.assert_called_once_with("/xapi/events/subscription/5")

    def test_delete_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(["event", "delete", "5"], input="n\n")

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()

    def test_delete_dry_run_checks_existence_no_delete(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SUBSCRIPTION

        result = authenticated_cli.invoke(["event", "delete", "5", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/subscription/5")
        authenticated_cli.client.delete.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_delete_dry_run_unknown_id_refused(self, authenticated_cli: AuthenticatedCLI) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999"
        )

        result = authenticated_cli.invoke(["event", "delete", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.delete.assert_not_called()


class TestEventEnableDisable:
    """Tests for `event enable`/`event disable`."""

    def test_enable(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["event", "enable", "5", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/events/subscription/5/activate"
        )

    def test_disable(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = make_response(
            text="", content_type="text/plain"
        )

        result = authenticated_cli.invoke(["event", "disable", "5", "--yes"])

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/events/subscription/5/deactivate"
        )

    def test_enable_dry_run_checks_existence_no_post(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_SUBSCRIPTION

        result = authenticated_cli.invoke(["event", "enable", "5", "--dry-run"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/subscription/5")
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_disable_dry_run_unknown_id_refused(self, authenticated_cli: AuthenticatedCLI) -> None:
        from xnatctl.core.exceptions import ResourceNotFoundError

        authenticated_cli.client.get_json.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999"
        )

        result = authenticated_cli.invoke(["event", "disable", "999", "--dry-run"])

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()


class TestEventActions:
    """Tests for `event actions`."""

    def test_actions(self, authenticated_cli: AuthenticatedCLI) -> None:
        actions = [
            {
                "action-key": "org.nrg...EventServiceLoggingAction:org.nrg...EventServiceLoggingAction",
                "display-name": "Logging Action",
                "description": "Simple action that logs event detection.",
                "attributes": {},
            }
        ]
        authenticated_cli.client.get_json.return_value = actions

        result = authenticated_cli.invoke(["event", "actions"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/actions")
        assert "Logging Action" in result.output


class TestEventTypes:
    """Tests for `event types`."""

    def test_types(self, authenticated_cli: AuthenticatedCLI) -> None:
        types_ = [
            {
                "type": "org.nrg.xnat.eventservice.events.ProjectEvent",
                "statuses": ["CREATED", "DELETED"],
                "display-name": "Project",
                "description": "Project created or deleted.",
                "event-scope": ["SITE"],
                "is-xsi-type": True,
            }
        ]
        authenticated_cli.client.get_json.return_value = types_

        result = authenticated_cli.invoke(["event", "types"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/events/events")
        assert "Project" in result.output
