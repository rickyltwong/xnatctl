"""Event Service subscriptions, checked against a real server.

Every endpoint EventService calls was verified once, by hand, against this
same stack: kebab-case create/response bodies, a plain-text
``"{name}:{id}"`` body (not JSON) from a successful create, POST (not PUT)
for activate/deactivate, and clean 404s from GET/DELETE/activate/deactivate
against an unknown id. This file is what keeps that verification from going
stale.

The Event Service can be switched off site-wide (``GET /xapi/events/prefs``
-> ``{"enabled": false}`` on a fresh install) -- listing/reading still works
either way, but creating a subscription 405s with "Event Service disabled."
when it is off. This module owns no CLI-facing command for that toggle (see
``xnatctl.services.events`` module docstring), so the ``event_service_enabled``
fixture below talks to the raw client directly to guarantee subscription
creation works for the tests that need it, and restores whatever state it
found on the way out -- this tier runs against a shared stack, and flipping
a site-wide setting and leaving it flipped would be a surprise for whoever
runs the suite next.

Every subscription created here is deleted in a ``finally`` block.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
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
    """Ensure the site-wide Event Service is on for this module, restore after.

    Subscription creation 405s with "Event Service disabled." when this
    site-wide preference is off (verified live) -- read-only tests do not
    need it, but every creating test does.
    """
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
    """Create a probe subscription, yield its full record, delete it after."""
    service = EventService(xnat_client)
    subscription_id = service.create_subscription(_probe_payload("xnatctl-it-probe"))
    created = service.get_subscription(subscription_id)
    try:
        yield created
    finally:
        try:
            service.delete_subscription(subscription_id)
        except Exception as exc:  # noqa: BLE001 -- teardown must not mask a test result
            print(f"\nWARNING: could not delete probe subscription {subscription_id}: {exc}")


class TestListActionsAndTypes:
    """Read-only endpoints -- unaffected by the site-wide enabled toggle."""

    def test_list_subscriptions_returns_a_bare_array(self, xnat_client: Any) -> None:
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

    def test_get_unknown_subscription_raises_not_found(self, xnat_client: Any) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_subscription(999_999_999)


class TestCreateAndDelete:
    """The mutating half: create, delete, and the existence-check guards."""

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

    def test_delete_unknown_subscription_raises_not_found(self, xnat_client: Any) -> None:
        """Verifies the real DELETE contract: a clean 404, unlike
        CommandService.delete_command's idempotent-succeeds DELETE.
        """
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_subscription(999_999_999)


class TestActivateDeactivate:
    """POST-based enable/disable, and the unknown-id guard."""

    def test_activate_deactivate_round_trips(
        self, xnat_client: Any, probe_subscription: dict[str, Any]
    ) -> None:
        service = EventService(xnat_client)
        subscription_id = probe_subscription["id"]

        service.deactivate_subscription(subscription_id)
        assert service.get_subscription(subscription_id)["active"] is False

        service.activate_subscription(subscription_id)
        assert service.get_subscription(subscription_id)["active"] is True

    def test_activate_unknown_subscription_raises_not_found(self, xnat_client: Any) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.activate_subscription(999_999_999)

    def test_deactivate_unknown_subscription_raises_not_found(self, xnat_client: Any) -> None:
        service = EventService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.deactivate_subscription(999_999_999)
