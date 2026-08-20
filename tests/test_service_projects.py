"""Unit tests for ProjectService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import make_response

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.project import Project
from xnatctl.services.projects import ProjectService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> ProjectService:
    """Create ProjectService with mock client."""
    return ProjectService(mock_client)


SAMPLE_PROJECT_ROW = {
    "ID": "PROJ01",
    "label": "PROJ01",
    "name": "Test Project",
    "secondary_ID": "SEC01",
    "pi_firstname": "Jane",
    "pi_lastname": "Doe",
    "accessibility": "private",
    "URI": "/data/projects/PROJ01",
}


class TestProjectList:
    """Tests for ProjectService.list."""

    def test_list_top_level_array(self, service: ProjectService, mock_client: MagicMock) -> None:
        """List tolerates top-level JSON arrays."""
        mock_client.get.return_value = make_response([SAMPLE_PROJECT_ROW])

        result = service.list()

        assert len(result) == 1
        assert result[0].id == "PROJ01"

    def test_list_returns_projects(self, service: ProjectService, mock_client: MagicMock) -> None:
        """List returns Project objects parsed from ResultSet."""
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}
        )

        result = service.list()

        assert len(result) == 1
        assert isinstance(result[0], Project)
        assert result[0].id == "PROJ01"
        assert result[0].name == "Test Project"
        mock_client.get.assert_called_once()

    def test_list_with_limit(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Limit truncates results."""
        rows = [
            {**SAMPLE_PROJECT_ROW, "ID": f"PROJ{i:02d}", "label": f"PROJ{i:02d}"} for i in range(5)
        ]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.list(limit=2)

        assert len(result) == 2

    def test_list_accessible_param(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Accessible flag is passed as query param."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(accessible=True)

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["accessible"] == "true"

    def test_list_empty(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Empty ResultSet returns empty list."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        result = service.list()

        assert result == []


class TestProjectGet:
    """Tests for ProjectService.get."""

    def test_get_items_response(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Get handles `items[]` detail responses."""
        mock_client.get.return_value = make_response(
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "PROJ01",
                            "name": "Test Project",
                            "secondary_ID": "SEC01",
                        }
                    }
                ]
            }
        )

        result = service.get("PROJ01")

        assert isinstance(result, Project)
        assert result.id == "PROJ01"
        assert result.secondary_id == "SEC01"

    def test_get_returns_project(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Get returns a single Project."""
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}
        )

        result = service.get("PROJ01")

        assert isinstance(result, Project)
        assert result.id == "PROJ01"
        assert result.pi_firstname == "Jane"

    def test_get_not_found_empty_results(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Get raises ResourceNotFoundError when results are empty."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("MISSING")

    def test_get_not_found_404(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on 404."""
        mock_client.get.side_effect = ResourceNotFoundError("resource", "/data/projects/GONE")

        with pytest.raises(ResourceNotFoundError):
            service.get("GONE")


class TestProjectCreate:
    """Tests for ProjectService.create."""

    def test_create_calls_put_then_get(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Create issues PUT then fetches the project."""
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}
        )

        result = service.create("PROJ01", name="Test Project", accessibility="private")

        assert isinstance(result, Project)
        mock_client.put.assert_called_once()
        put_kwargs = mock_client.put.call_args
        assert put_kwargs[1]["params"]["name"] == "Test Project"
        assert put_kwargs[1]["params"]["accessibility"] == "private"

    def test_create_optional_params(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Optional params are only sent when provided."""
        mock_client.put.return_value = make_response("", content_type="text/plain")
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}
        )

        service.create("PROJ01")

        put_kwargs = mock_client.put.call_args
        assert "description" not in put_kwargs[1]["params"]
        assert "keywords" not in put_kwargs[1]["params"]


class TestProjectDelete:
    """Tests for ProjectService.delete."""

    def test_delete_returns_true(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Delete returns True on success."""
        mock_client.delete.return_value = make_response("")

        assert service.delete("PROJ01") is True
        mock_client.delete.assert_called_once()

    def test_delete_with_remove_files(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Delete passes removeFiles param."""
        mock_client.delete.return_value = make_response("")

        service.delete("PROJ01", remove_files=True)

        call_kwargs = mock_client.delete.call_args
        assert call_kwargs[1]["params"]["removeFiles"] == "true"


class TestProjectGetSubjects:
    """Tests for ProjectService.get_subjects."""

    def test_get_subjects(self, service: ProjectService, mock_client: MagicMock) -> None:
        """get_subjects returns raw dicts."""
        rows = [{"ID": "SUBJ01", "label": "Subject 1"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_subjects("PROJ01")

        assert len(result) == 1
        assert result[0]["ID"] == "SUBJ01"

    def test_get_subjects_with_limit(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Limit truncates subject results."""
        rows = [{"ID": f"SUBJ{i:02d}"} for i in range(5)]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_subjects("PROJ01", limit=2)

        assert len(result) == 2


class TestProjectGetSessions:
    """Tests for ProjectService.get_sessions."""

    def test_get_sessions(self, service: ProjectService, mock_client: MagicMock) -> None:
        """get_sessions returns raw dicts."""
        rows = [{"ID": "EXP01", "label": "Session 1"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_sessions("PROJ01")

        assert len(result) == 1
        assert result[0]["ID"] == "EXP01"


class TestProjectRawAccessors:
    """Tests for the raw-row / raw-response accessors the CLI routes through."""

    def test_list_rows_returns_raw_rows(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """list_rows returns untyped rows and requests the given columns."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}

        rows = service.list_rows("ID,name,pi_lastname,description")

        assert rows == [SAMPLE_PROJECT_ROW]
        mock_client.get_json.assert_called_once_with(
            "/data/projects", params={"columns": "ID,name,pi_lastname,description"}
        )

    def test_list_rows_tolerates_top_level_array(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """A bare JSON array is still extracted to rows."""
        mock_client.get_json.return_value = [SAMPLE_PROJECT_ROW]

        assert service.list_rows("ID") == [SAMPLE_PROJECT_ROW]

    def test_get_detail_items_response(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """get_detail unwraps an items[] detail document to data_fields."""
        mock_client.get_json.return_value = {
            "items": [{"data_fields": {"ID": "PROJ01", "secondary_ID": "SEC01"}}]
        }

        detail = service.get_detail("PROJ01")

        assert detail == {"ID": "PROJ01", "secondary_ID": "SEC01"}
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ01")

    def test_get_detail_resultset_response(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """get_detail returns the first ResultSet row."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}

        assert service.get_detail("PROJ01") == SAMPLE_PROJECT_ROW

    def test_get_detail_missing_returns_none(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """get_detail returns None when nothing matches."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": []}}

        assert service.get_detail("GONE") is None

    def test_subject_rows(self, service: ProjectService, mock_client: MagicMock) -> None:
        """subject_rows returns raw subject rows from the subjects collection."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "SUBJ01"}]}}

        rows = service.subject_rows("PROJ01")

        assert rows == [{"ID": "SUBJ01"}]
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ01/subjects")

    def test_experiment_rows(self, service: ProjectService, mock_client: MagicMock) -> None:
        """experiment_rows returns raw experiment rows from the experiments collection."""
        mock_client.get_json.return_value = {"ResultSet": {"Result": [{"ID": "EXP01"}]}}

        rows = service.experiment_rows("PROJ01")

        assert rows == [{"ID": "EXP01"}]
        mock_client.get_json.assert_called_once_with("/data/projects/PROJ01/experiments")

    def test_create_via_post_success(self, service: ProjectService, mock_client: MagicMock) -> None:
        """create_via_post POSTs the exact XML wire body and returns the raw response.

        The body is pinned byte-for-byte: whitespace, element order, and the
        declaration are all part of the wire contract this method preserves.
        """
        mock_client.post.return_value = make_response("", status_code=201)

        resp = service.create_via_post(
            "NEWPROJ",
            name="New Project",
            description="A test",
            pi_lastname="Smith",
            accessibility="protected",
        )

        assert resp.status_code == 201
        call = mock_client.post.call_args
        assert call[0][0] == "/data/projects/NEWPROJ"
        assert call[1]["params"] == {"accessibility": "protected"}
        assert call[1]["headers"] == {"Content-Type": "text/xml"}
        assert call[1]["data"] == (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<xnat:Project ID="NEWPROJ" xmlns:xnat="http://nrg.wustl.edu/xnat">\n'
            "    <xnat:name>New Project</xnat:name>\n"
            "    <xnat:description>A test</xnat:description>\n"
            "    <xnat:PI><xnat:lastname>Smith</xnat:lastname></xnat:PI>\n"
            "</xnat:Project>"
        )

    def test_create_via_post_defaults_name_and_omits_optional_xml(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Name defaults to the ID; optional elements are absent, not empty."""
        mock_client.post.return_value = make_response("", status_code=200)

        service.create_via_post("NEWPROJ")

        assert mock_client.post.call_args[1]["data"] == (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<xnat:Project ID="NEWPROJ" xmlns:xnat="http://nrg.wustl.edu/xnat">\n'
            "    <xnat:name>NEWPROJ</xnat:name>\n"
            "</xnat:Project>"
        )


class TestProjectSetAccessibility:
    """Tests for ProjectService.set_accessibility."""

    def test_set_accessibility(self, service: ProjectService, mock_client: MagicMock) -> None:
        """set_accessibility calls PUT on correct path."""
        mock_client.put.return_value = make_response("", content_type="text/plain")

        result = service.set_accessibility("PROJ01", "public")

        assert result is True
        call_args = mock_client.put.call_args
        assert "/data/projects/PROJ01/accessibility/public" in call_args[0][0]
