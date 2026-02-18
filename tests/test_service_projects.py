"""Unit tests for ProjectService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

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


def _make_response(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


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

    def test_list_returns_projects(self, service: ProjectService, mock_client: MagicMock) -> None:
        """List returns Project objects parsed from ResultSet."""
        mock_client.get.return_value = _make_response(
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
            {**SAMPLE_PROJECT_ROW, "ID": f"PROJ{i:02d}", "label": f"PROJ{i:02d}"}
            for i in range(5)
        ]
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": rows}})

        result = service.list(limit=2)

        assert len(result) == 2

    def test_list_accessible_param(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Accessible flag is passed as query param."""
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": []}})

        service.list(accessible=True)

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["accessible"] == "true"

    def test_list_empty(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Empty ResultSet returns empty list."""
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": []}})

        result = service.list()

        assert result == []


class TestProjectGet:
    """Tests for ProjectService.get."""

    def test_get_returns_project(self, service: ProjectService, mock_client: MagicMock) -> None:
        """Get returns a single Project."""
        mock_client.get.return_value = _make_response(
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
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": []}})

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
        mock_client.put.return_value = _make_response("", content_type="text/plain")
        mock_client.get.return_value = _make_response(
            {"ResultSet": {"Result": [SAMPLE_PROJECT_ROW]}}
        )

        result = service.create("PROJ01", name="Test Project", accessibility="private")

        assert isinstance(result, Project)
        mock_client.put.assert_called_once()
        put_kwargs = mock_client.put.call_args
        assert put_kwargs[1]["params"]["name"] == "Test Project"
        assert put_kwargs[1]["params"]["accessibility"] == "private"

    def test_create_optional_params(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Optional params are only sent when provided."""
        mock_client.put.return_value = _make_response("", content_type="text/plain")
        mock_client.get.return_value = _make_response(
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
        mock_client.delete.return_value = _make_response("")

        assert service.delete("PROJ01") is True
        mock_client.delete.assert_called_once()

    def test_delete_with_remove_files(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Delete passes removeFiles param."""
        mock_client.delete.return_value = _make_response("")

        service.delete("PROJ01", remove_files=True)

        call_kwargs = mock_client.delete.call_args
        assert call_kwargs[1]["params"]["removeFiles"] == "true"


class TestProjectGetSubjects:
    """Tests for ProjectService.get_subjects."""

    def test_get_subjects(self, service: ProjectService, mock_client: MagicMock) -> None:
        """get_subjects returns raw dicts."""
        rows = [{"ID": "SUBJ01", "label": "Subject 1"}]
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": rows}})

        result = service.get_subjects("PROJ01")

        assert len(result) == 1
        assert result[0]["ID"] == "SUBJ01"

    def test_get_subjects_with_limit(
        self, service: ProjectService, mock_client: MagicMock
    ) -> None:
        """Limit truncates subject results."""
        rows = [{"ID": f"SUBJ{i:02d}"} for i in range(5)]
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": rows}})

        result = service.get_subjects("PROJ01", limit=2)

        assert len(result) == 2


class TestProjectGetSessions:
    """Tests for ProjectService.get_sessions."""

    def test_get_sessions(self, service: ProjectService, mock_client: MagicMock) -> None:
        """get_sessions returns raw dicts."""
        rows = [{"ID": "EXP01", "label": "Session 1"}]
        mock_client.get.return_value = _make_response({"ResultSet": {"Result": rows}})

        result = service.get_sessions("PROJ01")

        assert len(result) == 1
        assert result[0]["ID"] == "EXP01"


class TestProjectSetAccessibility:
    """Tests for ProjectService.set_accessibility."""

    def test_set_accessibility(self, service: ProjectService, mock_client: MagicMock) -> None:
        """set_accessibility calls PUT on correct path."""
        mock_client.put.return_value = _make_response("", content_type="text/plain")

        result = service.set_accessibility("PROJ01", "public")

        assert result is True
        call_args = mock_client.put.call_args
        assert "/data/projects/PROJ01/accessibility/public" in call_args[0][0]
