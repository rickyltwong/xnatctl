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


class TestVerifyResource:
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


class TestVerifyScanSet:
    def test_all_scans_present(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        scans = [{"ID": "1"}, {"ID": "2"}, {"ID": "3"}]
        source_client.get.return_value = _make_response({"ResultSet": {"Result": scans}})
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": scans}})

        result = verifier.verify_scan_set("/data/experiments/E1", "/data/experiments/E2")

        assert result.verified is True
        assert result.source_count == 3
        assert result.dest_count == 3
        assert result.missing_scans == ()

    def test_detects_missing_scans(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "1"}, {"ID": "2"}, {"ID": "3"}]}}
        )
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": [{"ID": "1"}]}})

        result = verifier.verify_scan_set("/data/experiments/E1", "/data/experiments/E2")

        assert result.verified is False
        assert result.missing_scans == ("2", "3")
        assert "Missing 2 scans" in result.message


class TestVerifyExperiment:
    def test_passes_when_scans_and_files_match(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        scans_resp = _make_response({"ResultSet": {"Result": [{"ID": "1"}]}})
        resources_resp = _make_response({"ResultSet": {"Result": [{"label": "DICOM"}]}})
        files_resp = _make_response({"ResultSet": {"Result": [{"Name": "f1"}, {"Name": "f2"}]}})

        # Source: scans (tier1+tier2 reused), resources, files
        # Dest: scans, files
        source_client.get.side_effect = [
            scans_resp,
            resources_resp,
            files_resp,
        ]
        dest_client.get.side_effect = [scans_resp, files_resp]

        result = verifier.verify_experiment("/data/experiments/E1", "/data/experiments/E2")

        assert result.verified is True

    def test_fails_when_scan_missing(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        source_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "1"}, {"ID": "2"}]}}
        )
        dest_client.get.return_value = _make_response({"ResultSet": {"Result": [{"ID": "1"}]}})

        result = verifier.verify_experiment("/data/experiments/E1", "/data/experiments/E2")

        assert result.verified is False
        assert "2" in result.missing_scans

    def test_fails_when_file_count_mismatches(
        self,
        verifier: Verifier,
        source_client: MagicMock,
        dest_client: MagicMock,
    ) -> None:
        scans_resp = _make_response({"ResultSet": {"Result": [{"ID": "1"}]}})
        resources_resp = _make_response({"ResultSet": {"Result": [{"label": "DICOM"}]}})
        src_files = _make_response({"ResultSet": {"Result": [{"Name": "f1"}, {"Name": "f2"}]}})
        dst_files = _make_response({"ResultSet": {"Result": [{"Name": "f1"}]}})

        # Source: scans (tier1+tier2 reused), resources, files
        # Dest: scans, files
        source_client.get.side_effect = [
            scans_resp,
            resources_resp,
            src_files,
        ]
        dest_client.get.side_effect = [scans_resp, dst_files]

        result = verifier.verify_experiment("/data/experiments/E1", "/data/experiments/E2")

        assert result.verified is False
        assert len(result.mismatched_resources) == 1
        assert result.mismatched_resources[0][0] == "1"
