"""Tests for transfer verifier."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.services.transfer.verifier import VerificationResult, Verifier


def _make_response(json_data: dict) -> MagicMock:
    """Create a mock httpx.Response with given JSON payload."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.headers = {"content-type": "application/json"}
    return resp


@pytest.fixture
def source_client() -> MagicMock:
    """Mock source XNAT client."""
    client = MagicMock()
    client.base_url = "https://src.example.org"
    return client


@pytest.fixture
def dest_client() -> MagicMock:
    """Mock destination XNAT client."""
    client = MagicMock()
    client.base_url = "https://dst.example.org"
    return client


@pytest.fixture
def verifier(source_client: MagicMock, dest_client: MagicMock) -> Verifier:
    """Create a Verifier with mock clients."""
    return Verifier(source_client, dest_client)


class TestVerifier:
    def test_verified_when_counts_match(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        files = [{"Name": "file1.dcm"}, {"Name": "file2.dcm"}]
        source_client.get.return_value = _make_response({"ResultSet": {"Result": files}})
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": files}})

        result: VerificationResult = verifier.verify_resource(
            source_path="/data/experiments/E1/resources/DICOM/files",
            dest_path="/data/experiments/E2/resources/DICOM/files",
        )

        assert result.verified is True
        assert result.source_count == 2
        assert result.dest_count == 2

    def test_not_verified_when_counts_differ(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"Name": "f1"}, {"Name": "f2"}]}}
        )
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": [{"Name": "f1"}]}})

        result = verifier.verify_resource(
            source_path="/data/experiments/E1/resources/DICOM/files",
            dest_path="/data/experiments/E2/resources/DICOM/files",
        )

        assert result.verified is False
        assert result.source_count == 2
        assert result.dest_count == 1
