"""DICOM SCP Service: XNAT DICOM SCP receiver (AE title / port) administration.

Every endpoint here was verified live against XNAT 1.9.2.1 -- including the
mutating verbs (create, delete, enable/disable). Two shapes are surprising
enough to call out loudly at their call site: ``PUT /xapi/dicomscp/{id}`` is
a PARTIAL MERGE, not a full replace (the opposite of
``xnatctl.services.commands.CommandService.update_command``'s ``POST
/xapi/commands/{id}``), and the server does not validate ``port`` at all --
0 and a port already bound by another receiver are both accepted silently.
"""

from __future__ import annotations

from typing import Any, cast

from xnatctl.core.exceptions import InputValidationError, XNATCtlError
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


class DicomScpService(BaseService):
    """Service for XNAT DICOM SCP receiver administration.

    Every method returns plain ``dict``/``list[dict]`` -- ``DicomSCPInstance``
    JSON is a plugin-internal shape with no library-consumer need distinct
    from the CLI's own rendering, per the data-flow rule in ``AGENTS.md``.
    """

    def list_scps(self) -> list[dict[str, Any]]:
        """List all registered DICOM SCP receivers.

        Verified live: ``GET /xapi/dicomscp`` returns a bare JSON array.

        Returns:
            List of receiver dicts. Verified key set: ``id``, ``identifier``,
            ``label``, ``port``, ``aeTitle``, ``enabled``, ``disabled``,
            ``customProcessing``, ``directArchive``, ``anonymizationEnabled``,
            ``whitelistEnabled``, ``routingExpressionsEnabled``,
            ``whitelist``, ``created``, ``timestamp``.
        """
        path = "/xapi/dicomscp"
        data = self.client.get_json(path)
        return _expect_list(data, path)

    def get_scp(self, scp_id: int) -> dict[str, Any]:
        """Get one DICOM SCP receiver's full definition.

        Verified live: ``GET /xapi/dicomscp/{id}`` -> 200 with the same key
        set as :meth:`list_scps`'s rows, or a clean 404 ("Unable to find
        requested file or resource: DicomSCPInstance(id: {id})") for an
        unknown id -- mapped to ``ResourceNotFoundError`` by the client's
        general 404 handling, no extra translation needed here.

        Args:
            scp_id: Numeric receiver ID.

        Returns:
            Receiver dict -- see :meth:`list_scps` for the key set.
        """
        path = f"/xapi/dicomscp/{quote_path_segment(str(scp_id))}"
        data = self.client.get_json(path)
        return _expect_dict(data, path)

    def list_identifiers(self) -> dict[str, str]:
        """List registered DICOM object identifiers.

        Verified live: ``GET /xapi/dicomscp/identifiers`` -> 200 with a JSON
        object mapping identifier key to a human-readable description, e.g.
        ``{"dicomObjectIdentifier": "Default DICOM object identifier
        (ClassicDicomObjectIdentifier)"}``. A receiver's ``identifier`` field
        must be one of these keys -- ``POST /xapi/dicomscp`` with an unknown
        or missing identifier answers 400/500 (verified live), so
        :meth:`resolve_identifier` checks against this list before create.
        """
        path = "/xapi/dicomscp/identifiers"
        data = self.client.get_json(path)
        return _expect_dict(data, path)

    def resolve_identifier(self, identifier: str | None) -> str:
        """Resolve ``--identifier``, defaulting when exactly one is registered.

        Args:
            identifier: The caller's explicit choice, or ``None`` to default.

        Returns:
            A valid, registered identifier key.

        Raises:
            InputValidationError: ``identifier`` is given but not registered,
                or omitted while more than one identifier is registered (an
                ambiguous default -- the caller must pick).
        """
        identifiers = self.list_identifiers()
        if identifier is not None:
            if identifier not in identifiers:
                raise InputValidationError(
                    f"Unknown DICOM object identifier {identifier!r}. Registered: "
                    f"{', '.join(sorted(identifiers)) or '(none)'}.",
                    field="identifier",
                    value=identifier,
                )
            return identifier
        if len(identifiers) == 1:
            return next(iter(identifiers))
        raise InputValidationError(
            "No --identifier given, and more than one DICOM object identifier is "
            f"registered ({', '.join(sorted(identifiers)) or '(none)'}). "
            "Pass one explicitly.",
            field="identifier",
            value=None,
        )

    def create_scp(self, ae_title: str, port: int, identifier: str) -> dict[str, Any]:
        """Register a new DICOM SCP receiver.

        Verified live: ``POST /xapi/dicomscp`` answers 200 (not 201) with the
        full created object, including its new numeric ``id``. The server
        does NOT validate ``port`` -- ``0`` and a port already bound by
        another receiver are both accepted silently (verified live); callers
        should validate the port client-side first (see
        ``xnatctl.core.validation.validate_port``).

        Args:
            ae_title: DICOM AE title. Validate with
                ``xnatctl.core.validation.validate_ae_title`` first.
            port: TCP port for the receiver to listen on.
            identifier: A registered DICOM object identifier (see
                :meth:`resolve_identifier`).

        Returns:
            The created receiver dict -- see :meth:`list_scps` for the key set.
        """
        path = "/xapi/dicomscp"
        payload = {"aeTitle": ae_title, "port": port, "identifier": identifier}
        data = self.client.post(path, json=payload).json()
        return _expect_dict(data, path)

    def delete_scp(self, scp_id: int) -> None:
        """Delete a DICOM SCP receiver.

        Verified live: ``DELETE /xapi/dicomscp/{id}`` answers 200 for an
        existing receiver and a clean 404 ("Could not find DICOM SCP
        instance with ID {id}") for an unknown one -- unlike
        ``CommandService.delete_command``'s ``DELETE /xapi/commands/{id}``
        (idempotent-succeeds, verified live), there is no preflight needed
        here to avoid a misleading success.

        Args:
            scp_id: Numeric receiver ID.

        Raises:
            ResourceNotFoundError: If the receiver does not exist.
        """
        path = f"/xapi/dicomscp/{quote_path_segment(str(scp_id))}"
        self.client.delete(path)

    def set_enabled(self, scp_id: int, enabled: bool) -> dict[str, Any]:
        """Enable or disable a DICOM SCP receiver.

        Verified live: ``PUT /xapi/dicomscp/{id}`` is a PARTIAL MERGE -- a
        body of just ``{"enabled": ...}`` leaves every other field
        (``aeTitle``, ``port``, ``identifier``, ...) untouched, and the
        response is the receiver's full updated object. A nonexistent
        ``scp_id`` answers a raw 500 ("No row with the given identifier
        exists: [...DicomSCPInstance#{id}]"), not a clean 404 -- this checks
        existence first via :meth:`get_scp` so an unknown id still raises
        ``ResourceNotFoundError``.

        Args:
            scp_id: Numeric receiver ID.
            enabled: Whether the receiver should be enabled.

        Returns:
            The receiver's updated dict.

        Raises:
            ResourceNotFoundError: If the receiver does not exist.
        """
        self.get_scp(scp_id)
        path = f"/xapi/dicomscp/{quote_path_segment(str(scp_id))}"
        data = self.client.put(path, json={"enabled": enabled}).json()
        return _expect_dict(data, path)
