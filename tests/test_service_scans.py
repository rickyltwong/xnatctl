"""Unit tests for ScanService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.scan import Scan
from xnatctl.services.scans import ScanService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> ScanService:
    """Create ScanService with mock client."""
    return ScanService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


SAMPLE_SCAN = {
    "ID": "1",
    "label": "1",
    "type": "T1w",
    "series_description": "T1 MPRAGE",
    "quality": "usable",
    "URI": "/data/experiments/E001/scans/1",
}


class TestScanList:
    """Tests for ScanService.list."""

    def test_list_scans(self, service: ScanService, mock_client: MagicMock) -> None:
        """List returns Scan objects with session_id injected."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SCAN]}})

        result = service.list("XNAT_E00001")

        assert len(result) == 1
        assert isinstance(result[0], Scan)
        assert result[0].id == "1"
        assert result[0].session_id == "XNAT_E00001"

    def test_list_with_project(self, service: ScanService, mock_client: MagicMock) -> None:
        """List with project uses project-scoped path and injects project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SCAN]}})

        result = service.list("XNAT_E00001", project="PROJ01")

        assert result[0].project == "PROJ01"
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/XNAT_E00001/scans" in call_path

    def test_list_with_columns(self, service: ScanService, mock_client: MagicMock) -> None:
        """Columns param is passed."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list("E001", columns=["ID", "type"])

        params = mock_client.get.call_args[1]["params"]
        assert params["columns"] == "ID,type"


class TestScanGet:
    """Tests for ScanService.get."""

    def test_get_scan(self, service: ScanService, mock_client: MagicMock) -> None:
        """Get returns Scan with session_id."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SCAN]}})

        result = service.get("XNAT_E00001", "1")

        assert isinstance(result, Scan)
        assert result.type == "T1w"
        assert result.session_id == "XNAT_E00001"

    def test_get_with_project(self, service: ScanService, mock_client: MagicMock) -> None:
        """Get with project injects project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SCAN]}})

        result = service.get("E001", "1", project="PROJ01")

        assert result.project == "PROJ01"

    def test_get_not_found(self, service: ScanService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("E001", "999")


class TestScanDelete:
    """Tests for ScanService.delete."""

    def test_delete(self, service: ScanService, mock_client: MagicMock) -> None:
        """Delete returns True."""
        mock_client.delete.return_value = _resp("")

        assert service.delete("E001", "1") is True

    def test_delete_with_remove_files(self, service: ScanService, mock_client: MagicMock) -> None:
        """Delete passes removeFiles."""
        mock_client.delete.return_value = _resp("")

        service.delete("E001", "1", remove_files=True)

        params = mock_client.delete.call_args[1]["params"]
        assert params["removeFiles"] == "true"


class TestScanDeleteMultiple:
    """Tests for ScanService.delete_multiple."""

    def test_delete_multiple_sequential(
        self, service: ScanService, mock_client: MagicMock
    ) -> None:
        """Sequential deletion of multiple scans."""
        mock_client.delete.return_value = _resp("")

        result = service.delete_multiple("E001", ["1", "2", "3"], parallel=False)

        assert len(result["deleted"]) == 3
        assert result["total"] == 3
        assert len(result["failed"]) == 0

    def test_delete_multiple_with_failure(
        self, service: ScanService, mock_client: MagicMock
    ) -> None:
        """Failed deletions are tracked."""
        def delete_side_effect(path: str, **kwargs: object) -> MagicMock:
            if "scans/2" in path:
                raise RuntimeError("server error")
            return _resp("")

        mock_client.delete.side_effect = delete_side_effect

        result = service.delete_multiple("E001", ["1", "2", "3"], parallel=False)

        assert "1" in result["deleted"]
        assert "3" in result["deleted"]
        assert "2" in result["failed"]
        assert len(result["errors"]) == 1

    def test_delete_multiple_wildcard(
        self, service: ScanService, mock_client: MagicMock
    ) -> None:
        """Wildcard '*' fetches all scan IDs first."""
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [
                {**SAMPLE_SCAN, "ID": "1"},
                {**SAMPLE_SCAN, "ID": "2"},
            ]}}
        )
        mock_client.delete.return_value = _resp("")

        result = service.delete_multiple("E001", ["*"], parallel=False)

        assert result["total"] == 2

    def test_delete_multiple_progress_callback(
        self, service: ScanService, mock_client: MagicMock
    ) -> None:
        """Progress callback is invoked for each scan."""
        mock_client.delete.return_value = _resp("")
        callback = MagicMock()

        service.delete_multiple("E001", ["1", "2"], parallel=False, progress_callback=callback)

        assert callback.call_count == 2


class TestScanGetResources:
    """Tests for ScanService.get_resources."""

    def test_get_resources(self, service: ScanService, mock_client: MagicMock) -> None:
        """get_resources returns raw dicts."""
        rows = [{"label": "DICOM", "file_count": 100}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.get_resources("E001", "1")

        assert len(result) == 1
        assert result[0]["label"] == "DICOM"


class TestScanSetQuality:
    """Tests for ScanService.set_quality."""

    def test_set_quality(self, service: ScanService, mock_client: MagicMock) -> None:
        """set_quality issues PUT with quality param."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        assert service.set_quality("E001", "1", "usable") is True
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xnat:imageScanData/quality"] == "usable"


class TestScanSetNote:
    """Tests for ScanService.set_note."""

    def test_set_note(self, service: ScanService, mock_client: MagicMock) -> None:
        """set_note issues PUT with note param."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        assert service.set_note("E001", "1", "test note") is True
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xnat:imageScanData/note"] == "test note"
