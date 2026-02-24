"""Unit tests for ResourceService."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.resource import Resource, ResourceFile
from xnatctl.services.resources import ResourceService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> ResourceService:
    """Create ResourceService with mock client."""
    return ResourceService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


SAMPLE_RESOURCE = {
    "ID": "RES001",
    "label": "DICOM",
    "format": "DICOM",
    "file_count": 200,
    "file_size": 104857600,
    "URI": "/data/experiments/E001/resources/DICOM",
}


class TestResourceList:
    """Tests for ResourceService.list."""

    def test_list_session_resources(self, service: ResourceService, mock_client: MagicMock) -> None:
        """List session-level resources."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.list("E001")

        assert len(result) == 1
        assert isinstance(result[0], Resource)
        assert result[0].label == "DICOM"
        assert result[0].session_id == "E001"
        call_path = mock_client.get.call_args[0][0]
        assert "/data/experiments/E001/resources" in call_path

    def test_list_scan_resources(self, service: ResourceService, mock_client: MagicMock) -> None:
        """List scan-level resources."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.list("E001", scan_id="1")

        assert result[0].scan_id == "1"
        call_path = mock_client.get.call_args[0][0]
        assert "/scans/1/resources" in call_path

    def test_list_with_project(self, service: ResourceService, mock_client: MagicMock) -> None:
        """List with project uses project-scoped path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.list("E001", project="PROJ01")

        assert result[0].project == "PROJ01"
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/" in call_path

    def test_list_tolerates_missing_id_and_unparseable_counts(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """list() tolerates real-ish rows that violate strict typing."""

        rows = [
            {
                "label": "Physio",
                "format": "TXT",
                "file_count": "",
                "file_size": "",
                "URI": "/data/experiments/E001/resources/Physio",
            },
            {
                "label": "Other",
                "format": "TXT",
                "file_count": "not-a-number",
                "file_size": "123",
                "xnat_abstractresource_id": "RES_ALT_1",
                "URI": "/data/experiments/E001/resources/Other",
            },
        ]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list("E001")

        assert [r.label for r in result] == ["Physio", "Other"]
        assert result[0].file_count is None
        assert result[0].file_size is None
        assert result[0].id
        assert result[1].id == "RES_ALT_1"

    def test_list_treats_bool_file_count_as_missing(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """Bool file_count should not coerce to 1/0."""

        rows = [
            {
                "label": "Physio",
                "format": "TXT",
                "file_count": True,
                "file_size": "",
                "URI": "/data/experiments/E001/resources/Physio",
            }
        ]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list("E001")

        assert result[0].file_count is None


class TestResourceGet:
    """Tests for ResourceService.get."""

    def test_get_resource(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Get resource by label."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.get("E001", "DICOM")

        assert isinstance(result, Resource)
        assert result.label == "DICOM"

    def test_get_not_found(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError when label not matched."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        with pytest.raises(ResourceNotFoundError):
            service.get("E001", "NIFTI")


class TestResourceListFiles:
    """Tests for ResourceService.list_files."""

    def test_list_files(self, service: ResourceService, mock_client: MagicMock) -> None:
        """list_files returns ResourceFile objects."""
        file_row = {"Name": "scan001.dcm", "Size": 524288, "URI": "/files/scan001.dcm"}
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [file_row]}})

        result = service.list_files("E001", "DICOM")

        assert len(result) == 1
        assert isinstance(result[0], ResourceFile)
        assert result[0].name == "scan001.dcm"
        assert result[0].size == 524288

    def test_list_files_scan_level(self, service: ResourceService, mock_client: MagicMock) -> None:
        """list_files at scan level uses correct path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list_files("E001", "DICOM", scan_id="1", project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/scans/1/resources/DICOM/files" in call_path


class TestResourceCreate:
    """Tests for ResourceService.create."""

    def test_create_resource(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Create issues PUT then fetches the resource."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.create("E001", "DICOM", format="DICOM", content="raw")

        assert isinstance(result, Resource)
        mock_client.put.assert_called_once()
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["format"] == "DICOM"
        assert put_params["content"] == "raw"

    def test_create_scan_level(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Create at scan level uses correct path."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        service.create("E001", "DICOM", scan_id="1")

        call_path = mock_client.put.call_args[0][0]
        assert "/scans/1/resources/DICOM" in call_path


    def test_create_existing_resource_409(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """Create returns existing resource on 409 Conflict."""
        resp_409 = httpx.Response(status_code=409, request=httpx.Request("PUT", "http://x"))
        mock_client.put.side_effect = httpx.HTTPStatusError(
            "409 Conflict", request=resp_409.request, response=resp_409
        )
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_RESOURCE]}})

        result = service.create("E001", "DICOM")

        assert isinstance(result, Resource)
        assert result.label == "DICOM"

    def test_create_non_409_error_raises(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """Create raises on non-409 HTTP errors."""
        resp_500 = httpx.Response(status_code=500, request=httpx.Request("PUT", "http://x"))
        mock_client.put.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error", request=resp_500.request, response=resp_500
        )

        with pytest.raises(httpx.HTTPStatusError):
            service.create("E001", "DICOM")


class TestResourceDelete:
    """Tests for ResourceService.delete."""

    def test_delete(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Delete returns True."""
        mock_client.delete.return_value = _resp("")

        assert service.delete("E001", "DICOM") is True

    def test_delete_with_remove_files(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """Delete passes removeFiles=true by default."""
        mock_client.delete.return_value = _resp("")

        service.delete("E001", "DICOM")

        params = mock_client.delete.call_args[1]["params"]
        assert params["removeFiles"] == "true"

    def test_delete_without_remove_files(
        self, service: ResourceService, mock_client: MagicMock
    ) -> None:
        """Delete without remove_files omits param."""
        mock_client.delete.return_value = _resp("")

        service.delete("E001", "DICOM", remove_files=False)

        params = mock_client.delete.call_args[1]["params"]
        assert "removeFiles" not in params


class TestResourceUploadFile:
    """Tests for ResourceService.upload_file."""

    def test_upload_file_not_found(self, service: ResourceService, mock_client: MagicMock) -> None:
        """Upload raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            service.upload_file("E001", "DICOM", Path("/nonexistent/file.dcm"))

    def test_upload_file(
        self, service: ResourceService, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """Upload reads file and calls client.put."""
        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}')
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.upload_file("E001", "DATA", test_file)

        assert result["success"] is True
        assert result["file"] == "test.json"
        assert result["size"] == test_file.stat().st_size
        mock_client.put.assert_called_once()

    def test_upload_file_zip_content_type(
        self, service: ResourceService, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """ZIP files get application/zip content type."""
        test_file = tmp_path / "archive.zip"
        test_file.write_bytes(b"PK\x03\x04fake")
        mock_client.put.return_value = _resp("", content_type="text/plain")

        service.upload_file("E001", "DATA", test_file)

        headers = mock_client.put.call_args[1]["headers"]
        assert headers["Content-Type"] == "application/zip"

    def test_upload_file_extract_flag(
        self, service: ResourceService, mock_client: MagicMock, tmp_path: Path
    ) -> None:
        """Extract flag is passed as param."""
        test_file = tmp_path / "data.zip"
        test_file.write_bytes(b"fake")
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.upload_file("E001", "DATA", test_file, extract=True)

        assert result["extracted"] is True
        params = mock_client.put.call_args[1]["params"]
        assert params["extract"] == "true"
