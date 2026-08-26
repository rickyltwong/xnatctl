"""Unit tests for EventService.

Fixture shapes are the real ones observed live against XNAT 1.9.2.1 core:
kebab-case JSON keys throughout, a bare JSON array from list/actions/types,
a plain-text "{name}:{id}" body (not JSON) from create, and empty 200/204
bodies from update/delete/activate/deactivate.
"""

from __future__ import annotations

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.events import EventService, _parse_created_id

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


class TestParseCreatedId:
    """Tests for the `"{name}:{id}"` plain-text create response parser."""

    def test_simple_name(self) -> None:
        assert _parse_created_id("probe-test-2:1", "/xapi/events/subscription") == 1

    def test_name_containing_colons_uses_trailing_digits(self) -> None:
        """Verified live: a name with colons in it still parses correctly --
        the id is always the text after the LAST colon.
        """
        assert _parse_created_id("weird:name:test:3", "/xapi/events/subscription") == 3

    def test_server_generated_name_when_name_omitted(self) -> None:
        text = "Logging Action on Project CREATED for Site:4"
        assert _parse_created_id(text, "/xapi/events/subscription") == 4

    def test_no_colon_raises(self) -> None:
        with pytest.raises(XNATCtlError, match="expected"):
            _parse_created_id("nope", "/xapi/events/subscription")

    def test_non_digit_suffix_raises(self) -> None:
        with pytest.raises(XNATCtlError, match="expected"):
            _parse_created_id("probe:abc", "/xapi/events/subscription")


class TestListSubscriptions:
    """Tests for EventService.list_subscriptions."""

    def test_list_subscriptions(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_SUBSCRIPTION]
        service = EventService(fake_client)

        result = service.list_subscriptions()

        assert result == [SAMPLE_SUBSCRIPTION]
        fake_client.get_json.assert_called_once_with("/xapi/events/subscriptions")

    def test_list_subscriptions_empty(self, fake_client) -> None:
        fake_client.get_json.return_value = []
        service = EventService(fake_client)

        assert service.list_subscriptions() == []

    def test_list_subscriptions_non_list_response_raises(self, fake_client) -> None:
        """An unexpected 2xx shape must raise, not silently become []."""
        fake_client.get_json.return_value = {"error": "nope"}
        service = EventService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_subscriptions()


class TestGetSubscription:
    """Tests for EventService.get_subscription."""

    def test_get_subscription(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_SUBSCRIPTION
        service = EventService(fake_client)

        result = service.get_subscription(5)

        assert result == SAMPLE_SUBSCRIPTION
        fake_client.get_json.assert_called_once_with("/xapi/events/subscription/5")

    def test_get_subscription_not_found_rewraps_with_type(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999"
        )
        service = EventService(fake_client)

        with pytest.raises(ResourceNotFoundError, match="event subscription not found: 999"):
            service.get_subscription(999)


class TestCreateSubscription:
    """Tests for EventService.create_subscription."""

    def test_create_subscription(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(
            text="cli-probe-1:5", content_type="text/plain"
        )
        service = EventService(fake_client)

        result = service.create_subscription(CREATE_PAYLOAD)

        assert result == 5
        fake_client.post.assert_called_once_with("/xapi/events/subscription", json=CREATE_PAYLOAD)


class TestDeleteSubscription:
    """Tests for EventService.delete_subscription."""

    def test_delete_subscription(self, fake_client, response_factory) -> None:
        fake_client.delete.return_value = response_factory(text="", content_type="text/plain")
        service = EventService(fake_client)

        service.delete_subscription(5)

        fake_client.delete.assert_called_once_with("/xapi/events/subscription/5")

    def test_delete_unknown_subscription_raises(self, fake_client) -> None:
        """DELETE itself 404s cleanly on an unknown id -- no preflight GET needed."""
        fake_client.delete.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999"
        )
        service = EventService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_subscription(999)


class TestActivateDeactivateSubscription:
    """Tests for EventService.activate_subscription / deactivate_subscription."""

    def test_activate_subscription(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(text="", content_type="text/plain")
        service = EventService(fake_client)

        service.activate_subscription(5)

        fake_client.post.assert_called_once_with("/xapi/events/subscription/5/activate")

    def test_deactivate_subscription(self, fake_client, response_factory) -> None:
        fake_client.post.return_value = response_factory(text="", content_type="text/plain")
        service = EventService(fake_client)

        service.deactivate_subscription(5)

        fake_client.post.assert_called_once_with("/xapi/events/subscription/5/deactivate")

    def test_activate_unknown_subscription_raises(self, fake_client) -> None:
        fake_client.post.side_effect = ResourceNotFoundError(
            "resource", "/xapi/events/subscription/999/activate"
        )
        service = EventService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.activate_subscription(999)


class TestListActions:
    """Tests for EventService.list_actions."""

    def test_list_actions(self, fake_client) -> None:
        actions = [
            {
                "action-key": "org.nrg...EventServiceLoggingAction:org.nrg...EventServiceLoggingAction",
                "display-name": "Logging Action",
                "description": "Simple action that logs event detection.",
                "attributes": {},
            }
        ]
        fake_client.get_json.return_value = actions
        service = EventService(fake_client)

        result = service.list_actions()

        assert result == actions
        fake_client.get_json.assert_called_once_with("/xapi/events/actions")

    def test_list_actions_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"error": "nope"}
        service = EventService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_actions()


class TestListEventTypes:
    """Tests for EventService.list_event_types."""

    def test_list_event_types(self, fake_client) -> None:
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
        fake_client.get_json.return_value = types_
        service = EventService(fake_client)

        result = service.list_event_types()

        assert result == types_
        fake_client.get_json.assert_called_once_with("/xapi/events/events")

    def test_list_event_types_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {"error": "nope"}
        service = EventService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_event_types()
