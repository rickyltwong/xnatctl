"""Tests for transfer conflict checker."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.services.transfer.conflicts import ConflictChecker, ConflictResult


def _make_response(json_data: dict) -> MagicMock:
    """Build a mock httpx.Response with the given JSON payload."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.headers = {"content-type": "application/json"}
    return resp


@pytest.fixture
def mock_dest_client() -> MagicMock:
    """Mock XNATClient for destination server."""
    client = MagicMock()
    client.base_url = "https://dst.example.org"
    return client


@pytest.fixture
def checker(mock_dest_client: MagicMock) -> ConflictChecker:
    """ConflictChecker wired to mock client."""
    return ConflictChecker(mock_dest_client)


class TestConflictResult:
    def test_no_conflict_defaults(self) -> None:
        result = ConflictResult(has_conflict=False, reason="", remote_id=None)
        assert result.has_conflict is False
        assert result.reason == ""
        assert result.remote_id is None

    def test_conflict_with_details(self) -> None:
        result = ConflictResult(has_conflict=True, reason="label mismatch", remote_id="XNAT_S001")
        assert result.has_conflict is True
        assert result.reason == "label mismatch"
        assert result.remote_id == "XNAT_S001"


class TestCheckSubject:
    def test_no_conflict_when_not_found(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response({"ResultSet": {"Result": []}})
        result = checker.check_subject("XNAT_S999", "SUB001", "DST")
        assert result.has_conflict is False

    def test_no_conflict_when_matches(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "XNAT_S999", "label": "SUB001", "project": "DST"}]}}
        )
        result = checker.check_subject("XNAT_S999", "SUB001", "DST")
        assert result.has_conflict is False

    def test_conflict_on_label_mismatch(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "XNAT_S999", "label": "DIFFERENT", "project": "DST"}]}}
        )
        result = checker.check_subject("XNAT_S999", "SUB001", "DST")
        assert result.has_conflict is True
        assert "label" in result.reason.lower()

    def test_conflict_on_project_mismatch(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "XNAT_S999", "label": "SUB001", "project": "OTHER"}]}}
        )
        result = checker.check_subject("XNAT_S999", "SUB001", "DST")
        assert result.has_conflict is True
        assert "project" in result.reason.lower()


class TestCheckExperiment:
    def test_no_conflict_when_not_found(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response({"ResultSet": {"Result": []}})
        result = checker.check_experiment("XNAT_E001", "EXP_LABEL", "DST")
        assert result.has_conflict is False

    def test_no_conflict_when_matches(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [{"ID": "XNAT_E001", "label": "EXP_LABEL", "project": "DST"}]}}
        )
        result = checker.check_experiment("XNAT_E001", "EXP_LABEL", "DST")
        assert result.has_conflict is False

    def test_conflict_on_label_mismatch(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {
                "ResultSet": {
                    "Result": [{"ID": "XNAT_E001", "label": "WRONG_LABEL", "project": "DST"}]
                }
            }
        )
        result = checker.check_experiment("XNAT_E001", "EXP_LABEL", "DST")
        assert result.has_conflict is True
        assert "label" in result.reason.lower()

    def test_conflict_on_project_mismatch(
        self, checker: ConflictChecker, mock_dest_client: MagicMock
    ) -> None:
        mock_dest_client.get.return_value = _make_response(
            {
                "ResultSet": {
                    "Result": [{"ID": "XNAT_E001", "label": "EXP_LABEL", "project": "OTHER"}]
                }
            }
        )
        result = checker.check_experiment("XNAT_E001", "EXP_LABEL", "DST")
        assert result.has_conflict is True
        assert "project" in result.reason.lower()
