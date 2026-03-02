"""Tests for transfer discovery service."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.services.transfer.discovery import (
    ChangeType,
    DiscoveryService,
)


def _make_response(json_data: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.headers = {"content-type": "application/json"}
    return resp


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> DiscoveryService:
    return DiscoveryService(mock_client)


class TestDiscoverSubjects:
    def test_all_new_when_no_last_sync(
        self, service: DiscoveryService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = _make_response({
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_S001",
                        "label": "SUB001",
                        "project": "PROJ",
                        "insert_date": "2026-01-01 10:00:00.0",
                        "last_modified": "2026-01-15 10:00:00.0",
                    },
                ]
            }
        })

        entities = service.discover_subjects("PROJ", last_sync_time=None)

        assert len(entities) == 1
        assert entities[0].change_type == ChangeType.NEW
        assert entities[0].local_id == "XNAT_S001"

    def test_classifies_modified(
        self, service: DiscoveryService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = _make_response({
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_S001",
                        "label": "SUB001",
                        "project": "PROJ",
                        "insert_date": "2026-01-01 10:00:00.0",
                        "last_modified": "2026-02-01 10:00:00.0",
                    },
                ]
            }
        })

        entities = service.discover_subjects(
            "PROJ",
            last_sync_time="2026-01-15T00:00:00+00:00",
        )

        assert len(entities) == 1
        assert entities[0].change_type == ChangeType.MODIFIED

    def test_skips_shared_subjects(
        self, service: DiscoveryService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = _make_response({
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_S001",
                        "label": "SUB001",
                        "project": "OTHER_PROJ",
                        "insert_date": "2026-01-01 10:00:00.0",
                        "last_modified": "2026-01-01 10:00:00.0",
                    },
                ]
            }
        })

        entities = service.discover_subjects("PROJ", last_sync_time=None)

        assert len(entities) == 0


class TestDiscoverExperiments:
    def test_discovers_experiments_for_subject(
        self, service: DiscoveryService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = _make_response({
            "ResultSet": {
                "Result": [
                    {
                        "ID": "XNAT_E001",
                        "label": "EXP001",
                        "xsiType": "xnat:mrSessionData",
                        "project": "PROJ",
                        "insert_date": "2026-01-01 10:00:00.0",
                        "last_modified": "2026-01-01 10:00:00.0",
                    },
                ]
            }
        })

        entities = service.discover_experiments(
            "PROJ", "XNAT_S001", last_sync_time=None
        )

        assert len(entities) == 1
        assert entities[0].xsi_type == "xnat:mrSessionData"
        assert entities[0].parent_id == "XNAT_S001"
