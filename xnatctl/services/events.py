"""Event Service: XNAT Event Service subscription management.

Every endpoint here was verified live against XNAT 1.9.2.1 core -- the Event
Service ships in XNAT core, not as a plugin (``GET /xapi/plugins`` lists
only ``containers`` on the instance this was verified against).

The REST surface is asymmetric in a way worth calling out up front:
subscription *listing* lives under the plural ``/xapi/events/subscriptions``
(``GET`` only -- ``OPTIONS`` answers ``Allow: GET,HEAD,OPTIONS``), while
every single-subscription operation (create, show, delete, activate,
deactivate) lives under the *singular* ``/xapi/events/subscription`` --
a different noun, not a different HTTP verb on the same path.
``GET /xapi/events/subscriptions/{id}`` (plural + id) is not routed at all
(404, no ``Allow`` header) -- do not "simplify" the singular/plural split
back into one path, it does not exist on the server.

Subscription creation bodies use kebab-case JSON keys (``action-key``,
``event-filter``, ``act-as-event-user``), matching the kebab-case the
server itself uses in its own responses (e.g. ``GET /xapi/events/actions``
returns ``action-key``). camelCase (``actionKey``) fails Jackson
deserialization with a 500 ("JSON parse error ... Null actionKey") rather
than a validation error, because it never reaches the validator: the JSON
simply does not parse into the server's ``SubscriptionCreator`` model.

**The site can have the Event Service switched off entirely**
(``GET /xapi/events/prefs`` -> ``{"enabled": false}``; a fresh 1.9.2.1
install ships this way). ``list``/``show``/``actions``/``events`` all still
answer normally when it is off -- only ``POST .../subscription`` (create)
is gated, answering 405 with the plain-text body ``"Event Service
disabled."`` (verified live both ways: the same payload that 405s while
disabled succeeds once ``PUT /xapi/events/prefs {"enabled": true}`` is
sent). Toggling that site-wide flag is deliberately not exposed by this
module or the ``event`` command group -- it is a distinct REST resource
from ``/xapi/siteConfig`` (not one of the properties ``admin site-config``
reads/writes), and whether an ``admin``-level toggle command is worth adding
is a separate decision from subscription CRUD.
"""

from __future__ import annotations

from typing import Any, cast

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.core.validation import quote_path_segment

from .base import BaseService


def _expect_list(data: Any, path: str) -> list[dict[str, Any]]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``[]``.

    See ``xnatctl.services.commands._expect_list`` for the same reasoning:
    a server that answers 200 with something other than a JSON array is a
    shape xnatctl does not understand, not "no results".
    """
    if not isinstance(data, list):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON array, "
            f"got {type(data).__name__}."
        )
    return cast(list[dict[str, Any]], data)


def _expect_dict(data: Any, path: str) -> dict[str, Any]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``{}``. See :func:`_expect_list`."""
    if not isinstance(data, dict):
        raise XNATCtlError(
            f"Unexpected response from {path}: expected a JSON object, got {type(data).__name__}."
        )
    return cast(dict[str, Any], data)


def _parse_created_id(text: str, path: str) -> int:
    """Parse the ``"{name}:{id}"`` plain-text body ``POST .../subscription`` answers on success.

    Verified live: a successful create answers 201 with plain text, not
    JSON -- the subscription's (possibly server-generated, when ``name`` is
    omitted from the payload) display name, a literal colon, then its new
    numeric id. The name half can itself legitimately contain colons
    (verified live with ``name: "weird:name:test"`` -> body
    ``"weird:name:test:3"``), so this splits on the *last* colon only --
    the id is always the trailing run of digits, never the name.

    Args:
        text: The raw response body.
        path: The request path, for the error message if parsing fails.

    Returns:
        The new subscription's numeric id.

    Raises:
        XNATCtlError: If the body is not of the form ``"...:<digits>"``.
    """
    stripped = text.strip()
    id_part = stripped.rsplit(":", 1)[-1] if ":" in stripped else ""
    if not id_part.isdigit():
        raise XNATCtlError(
            f'Unexpected response from POST {path}: expected "<name>:<id>" plain text, '
            f"got {stripped!r}."
        )
    return int(id_part)


class EventService(BaseService):
    """Service for XNAT Event Service subscription management.

    Every method returns plain ``dict``/``list[dict]``/``int`` -- subscription
    JSON is a core-XNAT-version-dependent shape with no library-consumer need
    distinct from the CLI's own rendering, per the data-flow rule in
    ``AGENTS.md``.
    """

    def list_subscriptions(self) -> list[dict[str, Any]]:
        """List all event subscriptions.

        Verified live: ``GET /xapi/events/subscriptions`` returns a bare
        JSON array, whether or not the Event Service is enabled site-wide
        (see the module docstring) and whether or not any subscriptions
        exist (an empty site answers ``[]``, not an empty body).

        Returns:
            List of subscription dicts. Verified key set: ``id``, ``name``,
            ``active``, ``action-key``, ``attributes`` (the action's input
            values, keyed by attribute name), ``event-filter`` (an object
            with ``event-type``, ``status``, ``project-ids``, and a
            server-internal ``reactorCriteriaHash``), ``act-as-event-user``,
            ``subscription-owner``, ``valid``, ``created``.
        """
        path = "/xapi/events/subscriptions"
        data = self.client.get_json(path)
        return _expect_list(data, path)

    def get_subscription(self, subscription_id: int) -> dict[str, Any]:
        """Get one subscription's full definition.

        Verified live: ``GET /xapi/events/subscription/{id}`` -> 200 with
        the same key set as :meth:`list_subscriptions`'s rows, or a clean
        404 ("Could not find entity with ID {id}") for an unknown id.

        Args:
            subscription_id: Numeric subscription ID.

        Returns:
            Subscription dict -- see :meth:`list_subscriptions` for the key set.

        Raises:
            ResourceNotFoundError: If the subscription does not exist.
        """
        path = f"/xapi/events/subscription/{quote_path_segment(str(subscription_id))}"
        try:
            data = self.client.get_json(path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("event subscription", str(subscription_id)) from e
        return _expect_dict(data, path)

    def create_subscription(self, payload: dict[str, Any]) -> int:
        """Register a new event subscription.

        Verified live: ``POST /xapi/events/subscription`` (singular --
        ``POST`` on the plural ``/xapi/events/subscriptions`` answers 405)
        with a subscription definition answers 201 on success -- see
        :func:`_parse_created_id` for the plain-text response shape.

        A payload that fails server-side validation (unknown ``action-key``,
        unknown ``event-filter.event-type``) answers 424 with a
        human-readable multi-line plain-text body, surfaced as
        ``ClientRequestError``. Note the server does *not* enforce an
        action's declared ``required`` attributes (see
        ``GET /xapi/events/actions``) -- creating an Email Action
        subscription with no ``to``/``subject``/``body`` in ``attributes``
        was verified live to succeed anyway (201), so client-side code
        cannot rely on the 424 path to catch a missing required attribute.

        Args:
            payload: The subscription definition to register, with
                kebab-case keys (see the module docstring) -- typically
                ``name``, ``active``, ``event-filter``, ``act-as-event-user``,
                ``action-key``, ``attributes``.

        Returns:
            The new subscription's numeric id.

        Raises:
            ClientRequestError: On a 424 validation failure, or a 405 with
                body ``"Event Service disabled."`` when the site-wide Event
                Service toggle is off (see the module docstring).
        """
        path = "/xapi/events/subscription"
        resp = self.client.post(path, json=payload)
        return _parse_created_id(resp.text, path)

    def delete_subscription(self, subscription_id: int) -> None:
        """Delete an event subscription.

        Verified live: ``DELETE /xapi/events/subscription/{id}`` answers
        204 for an existing subscription and a clean 404 ("Could not find
        entity with ID {id}") for an unknown one -- unlike
        ``CommandService.delete_command``'s idempotent-succeeds ``DELETE
        /xapi/commands/{id}``, no preflight is needed here to avoid a
        misleading success.

        Args:
            subscription_id: Numeric subscription ID.

        Raises:
            ResourceNotFoundError: If the subscription does not exist.
        """
        path = f"/xapi/events/subscription/{quote_path_segment(str(subscription_id))}"
        self.client.delete(path)

    def activate_subscription(self, subscription_id: int) -> None:
        """Activate (enable) an event subscription.

        Verified live: ``POST /xapi/events/subscription/{id}/activate`` --
        note ``POST``, not ``PUT`` (``OPTIONS`` on this path answers
        ``Allow: POST,OPTIONS``; a ``PUT`` here is 405) -- answers 200 with
        an empty body, and re-activating an already-active subscription is a
        harmless no-op (still 200). A clean 404 ("Could not find entity with
        ID {id}") for an unknown id, same as :meth:`delete_subscription`.

        Args:
            subscription_id: Numeric subscription ID.

        Raises:
            ResourceNotFoundError: If the subscription does not exist.
        """
        path = f"/xapi/events/subscription/{quote_path_segment(str(subscription_id))}/activate"
        self.client.post(path)

    def deactivate_subscription(self, subscription_id: int) -> None:
        """Deactivate (disable) an event subscription. See :meth:`activate_subscription`.

        Verified live: ``POST /xapi/events/subscription/{id}/deactivate`` --
        a separate route from ``activate``, not the same route with a query
        parameter.

        Raises:
            ResourceNotFoundError: If the subscription does not exist.
        """
        path = f"/xapi/events/subscription/{quote_path_segment(str(subscription_id))}/deactivate"
        self.client.post(path)

    def list_actions(self) -> list[dict[str, Any]]:
        """List available Event Service actions (the ``action-key`` catalog).

        Verified live: ``GET /xapi/events/actions`` returns a bare JSON
        array, whether or not the Event Service is enabled site-wide.

        Returns:
            List of action dicts. Verified key set: ``action-key``,
            ``display-name``, ``description``, ``attributes`` (an object
            keyed by attribute name, each describing ``type``,
            ``default-value``, ``required``, and -- for a couple of the
            built-in Email Action attributes -- a ``restrict-to-list`` of
            candidate values).
        """
        path = "/xapi/events/actions"
        data = self.client.get_json(path)
        return _expect_list(data, path)

    def list_event_types(self) -> list[dict[str, Any]]:
        """List available Event Service trigger types (for building ``event-filter.event-type``).

        Verified live: ``GET /xapi/events/events`` returns a bare JSON
        array, whether or not the Event Service is enabled site-wide.

        Returns:
            List of event-type dicts. Verified key set: ``type`` (the fully
            qualified Java class name to use as
            ``event-filter.event-type`` in a create payload), ``statuses``
            (valid values for ``event-filter.status``), ``display-name``,
            ``description``, ``event-scope`` (``PROJECT``, ``SITE``, or
            both), ``is-xsi-type``.
        """
        path = "/xapi/events/events"
        data = self.client.get_json(path)
        return _expect_list(data, path)
