"""Unit tests for SessionService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import make_response

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
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.list()

        assert len(result) == 1
        assert isinstance(result[0], Session)
        assert result[0].id == "XNAT_E00001"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/experiments"

    def test_list_by_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """List by project uses project-scoped path."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments" in call_path

    def test_list_by_project_and_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """List by project and subject uses nested path."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(project="PROJ01", subject="SUB01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB01/experiments" in call_path

    def test_list_with_modality(self, service: SessionService, mock_client: MagicMock) -> None:
        """Modality filter sets xsiType param."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(modality="MR")

        params = mock_client.get.call_args[1]["params"]
        assert params["xsiType"] == "xnat:mrSessionData"

    def test_list_with_limit(self, service: SessionService, mock_client: MagicMock) -> None:
        """Limit truncates results."""
        rows = [{**SAMPLE_SESSION, "ID": f"E{i:05d}"} for i in range(10)]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.list(limit=3)

        assert len(result) == 3

    def test_list_with_columns(self, service: SessionService, mock_client: MagicMock) -> None:
        """Columns param is joined and passed."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(columns=["ID", "label", "date"])

        params = mock_client.get.call_args[1]["params"]
        assert params["columns"] == "ID,label,date"


class TestSessionGet:
    """Tests for SessionService.get."""

    def test_get_items_response(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get session handles `items[]` detail responses."""
        mock_client.get.return_value = make_response(
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_E00001",
                            "label": "MR001",
                            "project": "PROJ01",
                            "subject_ID": "XNAT_S00001",
                        },
                        "meta": {"xsi:type": "xnat:mrSessionData"},
                    }
                ]
            }
        )

        result = service.get("XNAT_E00001")

        assert isinstance(result, Session)
        assert result.label == "MR001"
        assert result.xsi_type == "xnat:mrSessionData"

    def test_get_by_id(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get session by ID."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.get("XNAT_E00001")

        assert isinstance(result, Session)
        assert result.label == "MR001"

    def test_get_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get session scoped to project."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.get("MR001", project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_get_not_found(self, service: SessionService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("MISSING")

    def test_get_not_found_dispatches_on_type_not_message_text(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """A typed 404 is classified by its class, not by sniffing the message.

        The client can raise ``ResourceNotFoundError`` with any message; a
        session labelled e.g. "SUB404" must not defeat classification the way
        a substring check on "404" would.
        """
        mock_client.get.side_effect = ResourceNotFoundError("resource", "no such thing here")

        with pytest.raises(ResourceNotFoundError) as excinfo:
            service.get("MISSING")

        assert excinfo.value.details.get("resource_type") == "session"
        assert excinfo.value.details.get("resource_id") == "MISSING"


class TestSessionCreate:
    """Tests for SessionService.create."""

    def test_create_default_xsi_type(self, service: SessionService, mock_client: MagicMock) -> None:
        """Create uses default xnat:mrSessionData."""
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        result = service.create("PROJ01", "SUB01", "MR001")

        assert isinstance(result, Session)
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:mrSessionData"

    def test_create_with_modality_override(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Modality overrides xsi_type."""
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "PET001", modality="PET")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["xsiType"] == "xnat:petSessionData"

    def test_create_with_optional_params(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Optional params are passed when provided."""
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_SESSION]}})

        service.create("PROJ01", "SUB01", "MR001", date="2024-01-15", visit_id="V1")

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["date"] == "2024-01-15"
        assert put_params["visit_id"] == "V1"


class TestSessionDelete:
    """Tests for SessionService.delete."""

    def test_delete_with_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Delete uses project-scoped path."""
        mock_client.delete.return_value = make_response("")

        assert service.delete("MR001", project="PROJ01") is True
        call_path = mock_client.delete.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/MR001" in call_path

    def test_delete_without_project(self, service: SessionService, mock_client: MagicMock) -> None:
        """Delete without project uses global path."""
        mock_client.delete.return_value = make_response("")

        service.delete("XNAT_E00001")

        call_path = mock_client.delete.call_args[0][0]
        assert "/data/experiments/XNAT_E00001" in call_path


class TestSessionGetScans:
    """Tests for SessionService.get_scans."""

    def test_get_scans(self, service: SessionService, mock_client: MagicMock) -> None:
        """get_scans returns raw dicts."""
        rows = [{"ID": "1", "type": "T1w"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_scans("XNAT_E00001")

        assert len(result) == 1
        assert result[0]["type"] == "T1w"


class TestSessionGetResources:
    """Tests for SessionService.get_resources."""

    def test_get_resources(self, service: SessionService, mock_client: MagicMock) -> None:
        """get_resources returns raw dicts."""
        rows = [{"label": "DICOM", "file_count": 200}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_resources("XNAT_E00001", project="PROJ01")

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/experiments/XNAT_E00001/resources" in call_path


class TestSessionSetField:
    """Tests for SessionService.set_field."""

    def test_set_field(self, service: SessionService, mock_client: MagicMock) -> None:
        """set_field issues PUT with field param."""
        mock_client.put.return_value = make_response("", content_type="text/plain")

        assert service.set_field("XNAT_E00001", "note", "test note") is True
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["note"] == "test note"


class TestSessionShare:
    """Tests for SessionService.share."""

    def test_share(self, service: SessionService, mock_client: MagicMock) -> None:
        """Share issues PUT to target project path."""
        mock_client.put.return_value = make_response("", content_type="text/plain")

        assert service.share("XNAT_E00001", "PROJ02", label="MR_SHARED") is True
        call_path = mock_client.put.call_args[0][0]
        assert "/data/experiments/XNAT_E00001/projects/PROJ02" in call_path
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["label"] == "MR_SHARED"

    def test_share_primary(self, service: SessionService, mock_client: MagicMock) -> None:
        """Share with primary flag."""
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.share("XNAT_E00001", "PROJ02", primary=True)

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["primary"] == "true"


class TestSessionRawAccessors:
    """Tests for the raw-row accessors the CLI screens/engine route through."""

    def test_list_project_experiment_rows_no_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """The session-list rows carry the fixed column set and no subject filter."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "EXP01"}]}}

        rows = service.list_project_experiment_rows("PROJ01")

        assert rows == [{"ID": "EXP01"}]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType"},
        )

    def test_list_project_experiment_rows_with_subject(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """A subject adds a subject_label filter to the same request."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        service.list_project_experiment_rows("PROJ01", subject="SUBJ01")

        params = mock_client.get_json.call_args[1]["params"]
        assert params["subject_label"] == "SUBJ01"

    def test_list_project_experiment_rows_missing_resultset(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """A response without a ResultSet yields an empty list."""
        mock_client.get_json.return_value = {}

        assert service.list_project_experiment_rows("PROJ01") == []

    def test_scan_rows_uses_flat_experiment_url(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Without a project the scans URL is the routable flat form."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "1"}]}}

        rows = service.scan_rows("EXP01")

        assert rows == [{"ID": "1"}]
        mock_client.get_json.assert_called_once_with("/data/experiments/EXP01/scans")

    def test_experiment_resource_rows_subject_scoped(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        """Session-level resources are read from the subject-scoped experiment URL."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"label": "MISC"}]}}

        rows = service.experiment_resource_rows("EXP01", project="PROJ01", subject="SUBJ01")

        assert rows == [{"label": "MISC"}]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/subjects/SUBJ01/experiments/EXP01/resources"
        )


class TestListSessions:
    """Tests for SessionService.list_sessions (classification + modality filter)."""

    ROWS = {
        "ResultSet": {
            "Result": [
                {
                    "ID": "XNAT_E1",
                    "label": "MR001",
                    "subject_label": "SUB1",
                    "date": "2026-01-01",
                    "xsiType": "xnat:mrSessionData",
                },
                {
                    "ID": "XNAT_E2",
                    "label": "PET001",
                    "subject_label": "SUB2",
                    "date": "2026-01-02",
                    "xsiType": "xnat:petSessionData",
                },
                {
                    "ID": "XNAT_E3",
                    "label": "OTHER",
                    "subject_label": "SUB3",
                    "date": "",
                    "xsiType": "xnat:otherData",
                },
            ]
        }
    }

    def test_classifies_each_row_and_maps_fields(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = self.ROWS

        rows = service.list_sessions("PROJ01")

        assert rows == [
            {
                "id": "XNAT_E1",
                "label": "MR001",
                "subject": "SUB1",
                "date": "2026-01-01",
                "modality": "MR",
            },
            {
                "id": "XNAT_E2",
                "label": "PET001",
                "subject": "SUB2",
                "date": "2026-01-02",
                "modality": "PET",
            },
            {
                "id": "XNAT_E3",
                "label": "OTHER",
                "subject": "SUB3",
                "date": "",
                "modality": "?",
            },
        ]
        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType"},
        )

    def test_modality_filter_drops_non_matching_rows(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = self.ROWS

        rows = service.list_sessions("PROJ01", modality="MR")

        assert [r["id"] for r in rows] == ["XNAT_E1"]

    def test_subject_filter_is_forwarded_as_subject_label(
        self, service: SessionService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        service.list_sessions("PROJ01", subject="SUB1")

        mock_client.get_json.assert_called_once_with(
            "/data/projects/PROJ01/experiments",
            params={"columns": "ID,label,subject_label,date,xsiType", "subject_label": "SUB1"},
        )
