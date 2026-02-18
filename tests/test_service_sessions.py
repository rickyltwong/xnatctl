"""Unit tests for SessionService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.session import Session
from xnatctl.services.sessions import SessionService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> SessionService:
    """Create SessionService with mock client."""
    return SessionService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


SAMPLE_SESSION = {
    "ID": "XNAT_E00001",
    "label": "MR001",
    "project": "PROJ01",
    "subject_ID": "XNAT_S00001",
    "URI": "/data/experiments/XNAT_E00001",
}


class TestSessionList:
    """Tests for SessionService.list."""

    def test_list_all(self, service: SessionService, mock_client: MagicMock) -> None:
        """List without filters uses /data/experiments."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.list()

        assert len(result) == 1
        assert isinstance(result[0], Session)
        assert result[0].id == "XNAT_E00001"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/experiments"

    def test_list_by_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """List by project uses project-scoped path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments" in call_path

    def test_list_by_project_and_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """List by project and subject uses nested path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(project="PROJ01", subject="SUB01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB01/experiments" in call_path

    def test_list_with_modality(self, service: SessionService, mock_client: MagicMock) -> None:
        """Modality filter sets xsiType param."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(modality="MR")

        params = mock_client.get.call_args[1]["params"]
        assert params["xsiType"] == "xnat:mrSessionData"

    def test_list_with_limit(self, service: SessionService, mock_client: MagicMock) -> None:
        """Limit truncates results."""
        rows = [{**SAMPLE_SESSION, "ID": f"E{i:05d}"} for i in range(10)]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list(limit=3)

        assert len(result) == 3

    def test_list_with_columns(self, service: SessionService, mock_client: MagicMock) -> None:
        """Columns param is joined and passed."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(columns=["ID", "label", "date"])

        params = mock_client.get.call_args[1]["params"]
        assert params["columns"] == "ID,label,date"


class TestSessionGet:
    """Tests for SessionService.get."""

    def test_get_by_id(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get session by ID."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.get("XNAT_E00001")

        assert isinstance(result, Session)
        assert result.label == "MR001"

    def test_get_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get session scoped to project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.get("MR001", project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_get_not_found(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("MISSING")


class TestSessionCreate:
    """Tests for SessionService.create."""

    def test_create_default_xsi_type(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Create uses default xnat:mrSessionData."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.create("PROJ01", "SUB01", "MR001")

        assert isinstance(result, Session)
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:mrSessionData"

    def test_create_with_modality_override(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Modality overrides xsi_type."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "PET001", modality="PET")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:petSessionData"

    def test_create_with_optional_params(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Optional params are passed when provided."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "MR001", date="2024-01-15", visit_id="V1")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["date"] == "2024-01-15"
        assert put_params["visit_id"] == "V1"


class TestSessionDelete:
    """Tests for SessionService.delete."""

    def test_delete_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Delete uses project-scoped path."""
        mock_client.delete.return_value = _resp("")

        assert service.delete("MR001", project="PROJ01") is True
        call_path = mock_client.delete.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_delete_without_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Delete without project uses global path."""
        mock_client.delete.return_value = _resp("")

        service.delete("XNAT_E00001")

        call_path = mock_client.delete.call_args[0][0]
        assert "/data/experiments/XNAT_E00001" in call_path


class TestSessionGetScans:
    """Tests for SessionService.get_scans."""

    def test_get_scans(self, service: SessionService, mock_client: MagicMock) -> None:
        """get_scans returns raw dicts."""
        rows = [{"ID": "1", "type": "T1w"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.get_scans("XNAT_E00001")

        assert len(result) == 1
        assert result[0]["type"] == "T1w"


class TestSessionGetResources:
    """Tests for SessionService.get_resources."""

    def test_get_resources(self, service: SessionService, mock_client: MagicMock) -> None:
        """get_resources returns raw dicts."""
        rows = [{"label": "DICOM", "file_count": 200}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.get_resources("XNAT_E00001", project="PROJ01")

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/XNAT_E00001/resources" in call_path


class TestSessionSetField:
    """Tests for SessionService.set_field."""

    def test_set_field(self, service: SessionService, mock_client: MagicMock) -> None:
        """set_field issues PUT with field param."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        assert service.set_field("XNAT_E00001", "note", "test note") is True
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["note"] == "test note"


class TestSessionShare:
    """Tests for SessionService.share."""

    def test_share(self, service: SessionService, mock_client: MagicMock) -> None:
        """share issues PUT to target project path."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        assert service.share("XNAT_E00001", "PROJ02", label="MR_SHARED") is True
        call_path = mock_client.put.call_args[0][0]
        assert "/data/experiments/XNAT_E00001/projects/PROJ02" in call_path
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["label"] == "MR_SHARED"

    def test_share_primary(self, service: SessionService, mock_client: MagicMock) -> None:
        """share with primary flag."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        service.share("XNAT_E00001", "PROJ02", primary=True)

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["primary"] == "true"
