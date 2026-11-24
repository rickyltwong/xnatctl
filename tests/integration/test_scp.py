from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.services.scp import DicomScpService

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def probe_scp(xnat_client: Any) -> Iterator[dict[str, Any]]:
    service = DicomScpService(xnat_client)
    identifier = service.resolve_identifier(None)
    created = service.create_scp("XNATCTLIT", _unused_port(), identifier)
    try:
        yield created
    finally:
        try:
            service.delete_scp(created["id"])
        except Exception as exc:  # noqa: BLE001 -- teardown must not mask a test result
            print(f"\nWARNING: could not delete probe SCP receiver {created['id']}: {exc}")


class TestListAndIdentifiers:
    def test_list_scps_returns_at_least_the_default_receiver(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)

        receivers = service.list_scps()

        assert len(receivers) >= 1
        assert all("aeTitle" in r and "port" in r for r in receivers)

    def test_list_identifiers_includes_the_default_object_identifier(
        self, xnat_client: Any
    ) -> None:
        service = DicomScpService(xnat_client)

        identifiers = service.list_identifiers()

        assert "dicomObjectIdentifier" in identifiers

    def test_get_unknown_scp_raises_not_found(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.get_scp(999_999_999)


class TestCreateAndDelete:
    def test_create_then_get_then_delete(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)
        identifier = service.resolve_identifier(None)
        port = _unused_port()

        created = service.create_scp("XNATCTLIT", port, identifier)
        try:
            assert created["aeTitle"] == "XNATCTLIT"
            assert created["port"] == port
            assert created["identifier"] == identifier

            fetched = service.get_scp(created["id"])
            assert fetched["aeTitle"] == "XNATCTLIT"
        finally:
            service.delete_scp(created["id"])

        with pytest.raises(ResourceNotFoundError):
            service.get_scp(created["id"])

    def test_delete_unknown_scp_raises_not_found(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.delete_scp(999_999_999)

    def test_create_does_not_validate_port_server_side(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)
        identifier = service.resolve_identifier(None)

        created = service.create_scp("XNATCTLITBADPORT", 0, identifier)
        try:
            assert created["port"] == 0
        finally:
            service.delete_scp(created["id"])


class TestEnableDisable:
    def test_enable_disable_round_trips_without_touching_other_fields(
        self, xnat_client: Any, probe_scp: dict[str, Any]
    ) -> None:
        service = DicomScpService(xnat_client)

        disabled = service.set_enabled(probe_scp["id"], False)
        assert disabled["enabled"] is False
        assert disabled["aeTitle"] == probe_scp["aeTitle"]
        assert disabled["port"] == probe_scp["port"]

        enabled = service.set_enabled(probe_scp["id"], True)
        assert enabled["enabled"] is True
        assert enabled["aeTitle"] == probe_scp["aeTitle"]

    def test_set_enabled_unknown_scp_raises_not_found(self, xnat_client: Any) -> None:
        service = DicomScpService(xnat_client)

        with pytest.raises(ResourceNotFoundError):
            service.set_enabled(999_999_999, True)
