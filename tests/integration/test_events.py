from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.services.events import EventService

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

_PREFS_PATH = "/xapi/events/prefs"

_PROBE_ACTION_KEY = (
    "org.nrg.xnat.eventservice.actions.EventServiceLoggingAction:"
    "org.nrg.xnat.eventservice.actions.EventServiceLoggingAction"
)


def _probe_payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "active": True,
        "event-filter": {
            "event-type": "org.nrg.xnat.eventservice.events.ProjectEvent",
            "status": "CREATED",
            "project-ids": [],
        },
        "act-as-event-user": True,
        "action-key": _PROBE_ACTION_KEY,
        "attributes": {"param1": "xnatctl-integration", "param2": "probe"},
    }


@pytest.fixture(scope="module")
def event_service_enabled(xnat_client: Any) -> Iterator[None]:
    was_enabled = bool(xnat_client.get_json(_PREFS_PATH).get("enabled", False))
    if not was_enabled:
        xnat_client.put(_PREFS_PATH, json={"enabled": True})
    try:
        yield
    finally:
        if not was_enabled:
            xnat_client.put(_PREFS_PATH, json={"enabled": False})


@pytest.fixture
def probe_subscription(xnat_client: Any, event_service_enabled: None) -> Iterator[dict[str, Any]]:
    service = EventService(xnat_client)
    subscription_id = service.create_subscription(_probe_payload("xnatctl-it-probe"))
    created = service.get_subscription(subscription_id)
    try:
        yield created
    finally:
        try:
            service.delete_subscription(subscription_id)
        except XNATCtlError as exc:
            print(f"\nWARNING: could not delete probe subscription {subscription_id}: {exc}")


class TestListActionsAndTypes:
    def test_list_subscriptions_returns_a_bare_array(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        result = service.list_subscriptions()

        assert isinstance(result, list)

    def test_list_actions_includes_the_builtin_logging_action(self, xnat_client: Any) -> None:
        service = EventService(xnat_client)

        actions = service.list_actions()

        assert any(a.get("action-key") == _PROBE_ACTION_KEY for a in actions)

    def test_list_event_types_includes_project_event(self, xnat_client: Any) -> None:
        service = EventService(xnat_client)

        types_ = service.list_event_types()

        assert any(t.get("type") == "org.nrg.xnat.eventservice.events.ProjectEvent" for t in types_)

    def test_get_unknown_subscription_raises_not_found(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_subscription(999_999_999)


class TestCreateAndDelete:
    def test_create_then_get_then_delete(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        subscription_id = service.create_subscription(_probe_payload("xnatctl-it-create"))
        try:
            fetched = service.get_subscription(subscription_id)
            assert fetched["name"] == "xnatctl-it-create"
            assert fetched["action-key"] == _PROBE_ACTION_KEY
            assert fetched["active"] is True
        finally:
            service.delete_subscription(subscription_id)

        with pytest.raises(ResourceNotFoundError):
            service.get_subscription(subscription_id)

    def test_delete_unknown_subscription_raises_not_found(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_subscription(999_999_999)


class TestActivateDeactivate:
    def test_activate_deactivate_round_trips(
        self, xnat_client: Any, probe_subscription: dict[str, Any]
    ) -> None:
        service = EventService(xnat_client)
        subscription_id = probe_subscription["id"]

        service.deactivate_subscription(subscription_id)
        assert service.get_subscription(subscription_id)["active"] is False

        service.activate_subscription(subscription_id)
        assert service.get_subscription(subscription_id)["active"] is True

    def test_activate_unknown_subscription_raises_not_found(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.activate_subscription(999_999_999)

    def test_deactivate_unknown_subscription_raises_not_found(
        self, xnat_client: Any, event_service_enabled: None
    ) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.deactivate_subscription(999_999_999)
