"""Unit tests for DicomScpService.

Fixture shapes are the real ones observed live against XNAT 1.9.2.1: bare
JSON array from list, 200 (not 201) with the full object from create, a
PARTIAL MERGE from the enable/disable PUT, and a raw 500 (not 404) from that
same PUT against an unknown id.
"""

from __future__ import annotations

import pytest

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.services.scp import DicomScpService

SAMPLE_SCP = {
    "identifier": "dicomObjectIdentifier",
    "label": "XNAT:8104",
    "port": 8104,
    "aeTitle": "XNAT",
    "customProcessing": False,
    "directArchive": False,
    "anonymizationEnabled": True,
    "whitelistEnabled": False,
    "routingExpressionsEnabled": False,
    "whitelist": [],
    "created": 1787612569890,
    "timestamp": 1787612569890,
    "enabled": True,
    "id": 1,
    "disabled": 0,
}


class TestListScps:
    """Tests for DicomScpService.list_scps."""

    def test_list_scps(self, fake_client) -> None:
        fake_client.get_json.return_value = [SAMPLE_SCP]
        service = DicomScpService(fake_client)

        result = service.list_scps()

        assert result == [SAMPLE_SCP]
        fake_client.get_json.assert_called_once_with("/xapi/dicomscp")

    def test_list_scps_non_list_response_raises(self, fake_client) -> None:
        """An unexpected 2xx shape must raise, not silently become []."""
        fake_client.get_json.return_value = {"error": "nope"}
        service = DicomScpService(fake_client)

        with pytest.raises(XNATCtlError):
            service.list_scps()


class TestGetScp:
    """Tests for DicomScpService.get_scp."""

    def test_get_scp(self, fake_client) -> None:
        fake_client.get_json.return_value = SAMPLE_SCP
        service = DicomScpService(fake_client)

        result = service.get_scp(1)

        assert result == SAMPLE_SCP
        fake_client.get_json.assert_called_once_with("/xapi/dicomscp/1")

    def test_get_scp_not_found(self, fake_client) -> None:
        fake_client.get_json.side_effect = ResourceNotFoundError("resource", "/xapi/dicomscp/999")
        service = DicomScpService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_scp(999)


class TestResolveIdentifier:
    """Tests for DicomScpService.resolve_identifier."""

    def test_explicit_identifier_validated_against_registered_list(self, fake_client) -> None:
        fake_client.get_json.return_value = {"dicomObjectIdentifier": "Default..."}
        service = DicomScpService(fake_client)

        assert service.resolve_identifier("dicomObjectIdentifier") == "dicomObjectIdentifier"

    def test_unknown_explicit_identifier_rejected(self, fake_client) -> None:
        fake_client.get_json.return_value = {"dicomObjectIdentifier": "Default..."}
        service = DicomScpService(fake_client)

        with pytest.raises(InputValidationError, match="Unknown DICOM object identifier"):
            service.resolve_identifier("nope")

    def test_omitted_defaults_when_exactly_one_registered(self, fake_client) -> None:
        fake_client.get_json.return_value = {"dicomObjectIdentifier": "Default..."}
        service = DicomScpService(fake_client)

        assert service.resolve_identifier(None) == "dicomObjectIdentifier"

    def test_omitted_ambiguous_with_more_than_one_registered(self, fake_client) -> None:
        fake_client.get_json.return_value = {"a": "A", "b": "B"}
        service = DicomScpService(fake_client)

        with pytest.raises(InputValidationError, match="more than one"):
            service.resolve_identifier(None)


class TestCreateScp:
    """Tests for DicomScpService.create_scp."""

    def test_create_scp(self, fake_client, response_factory) -> None:
        created = {**SAMPLE_SCP, "id": 2, "aeTitle": "TESTSCP", "port": 18104}
        fake_client.post.return_value = response_factory(created)
        service = DicomScpService(fake_client)

        result = service.create_scp("TESTSCP", 18104, "dicomObjectIdentifier")

        assert result["id"] == 2
        fake_client.post.assert_called_once_with(
            "/xapi/dicomscp",
            json={"aeTitle": "TESTSCP", "port": 18104, "identifier": "dicomObjectIdentifier"},
        )


class TestDeleteScp:
    """Tests for DicomScpService.delete_scp."""

    def test_delete_scp(self, fake_client, response_factory) -> None:
        fake_client.delete.return_value = response_factory(text="", content_type="text/plain")
        service = DicomScpService(fake_client)

        service.delete_scp(2)

        fake_client.delete.assert_called_once_with("/xapi/dicomscp/2")

    def test_delete_unknown_scp_raises(self, fake_client) -> None:
        """DELETE itself 404s cleanly on an unknown id -- no preflight GET needed here."""
        fake_client.delete.side_effect = ResourceNotFoundError("resource", "/xapi/dicomscp/999")
        service = DicomScpService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_scp(999)


class TestSetEnabled:
    """Tests for DicomScpService.set_enabled."""

    def test_set_enabled_sends_partial_body(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = SAMPLE_SCP  # preflight get_scp
        updated = {**SAMPLE_SCP, "enabled": False}
        fake_client.put.return_value = response_factory(updated)
        service = DicomScpService(fake_client)

        result = service.set_enabled(1, False)

        assert result["enabled"] is False
        # A PARTIAL merge: only "enabled" in the body, not the full object.
        fake_client.put.assert_called_once_with("/xapi/dicomscp/1", json={"enabled": False})

    def test_set_enabled_unknown_scp_raises_before_put(self, fake_client) -> None:
        """The PUT itself 500s (raw Hibernate error) for an unknown id -- the
        preflight get_scp() call must turn that into a clean 404 first.
        """
        fake_client.get_json.side_effect = ResourceNotFoundError("resource", "/xapi/dicomscp/999")
        service = DicomScpService(fake_client)

        with pytest.raises(ResourceNotFoundError):
            service.set_enabled(999, True)
        fake_client.put.assert_not_called()
