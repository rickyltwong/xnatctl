"""Unit tests for PrearchiveService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.services.prearchive import PrearchiveService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> PrearchiveService:
    """Create PrearchiveService with mock client."""
    return PrearchiveService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


SAMPLE_PREARCHIVE = {
    "ID": "PRE001",
    "label": "session_01",
    "project": "PROJ01",
    "timestamp": "20240115_120000",
    "status": "READY",
    "URI": "/data/prearchive/projects/PROJ01/20240115_120000/session_01",
}


class TestPrearchiveList:
    """Tests for PrearchiveService.list."""

    def test_list_all(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """List without project uses /data/prearchive."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        result = service.list()

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/prearchive"

    def test_list_by_project(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """List by project uses project-scoped path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/prearchive/projects/PROJ01" in call_path


class TestPrearchiveGet:
    """Tests for PrearchiveService.get."""

    def test_get(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Get returns prearchive session dict."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        result = service.get("PROJ01", "20240115_120000", "session_01")

        assert result["status"] == "READY"

    def test_get_not_found(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("PROJ01", "20240115_120000", "missing")

    def test_get_404_error(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on 404."""
        mock_client.get.side_effect = ResourceNotFoundError("resource", "path")

        with pytest.raises(ResourceNotFoundError):
            service.get("PROJ01", "20240115_120000", "missing")


class TestPrearchiveArchive:
    """Tests for PrearchiveService.archive."""

    def test_archive(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Archive issues POST with commit action."""
        mock_client.post.return_value = _resp("/data/experiments/E001", content_type="text/plain")

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        assert result["project"] == "PROJ01"
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "commit"

    def test_archive_with_options(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """Archive passes subject, label, overwrite."""
        mock_client.post.return_value = _resp("", content_type="text/plain")

        service.archive(
            "PROJ01", "20240115_120000", "session_01",
            subject="SUB01", experiment_label="MR001", overwrite=True,
        )

        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["subject"] == "SUB01"
        assert post_params["label"] == "MR001"
        assert post_params["overwrite"] == "delete"


class TestPrearchiveDelete:
    """Tests for PrearchiveService.delete."""

    def test_delete(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Delete returns True."""
        mock_client.delete.return_value = _resp("")

        assert service.delete("PROJ01", "20240115_120000", "session_01") is True


class TestPrearchiveRebuild:
    """Tests for PrearchiveService.rebuild."""

    def test_rebuild(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Rebuild issues POST with rebuild action."""
        mock_client.post.return_value = _resp("", content_type="text/plain")

        result = service.rebuild("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "rebuild"


class TestPrearchiveMove:
    """Tests for PrearchiveService.move."""

    def test_move(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Move issues POST with move action and target project."""
        mock_client.post.return_value = _resp("", content_type="text/plain")

        result = service.move("PROJ01", "20240115_120000", "session_01", "PROJ02")

        assert result["success"] is True
        assert result["target_project"] == "PROJ02"
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "move"
        assert post_params["newProject"] == "PROJ02"


class TestPrearchiveGetScans:
    """Tests for PrearchiveService.get_scans."""

    def test_get_scans(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """get_scans returns raw dicts."""
        rows = [{"ID": "1", "type": "T1w"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.get_scans("PROJ01", "20240115_120000", "session_01")

        assert len(result) == 1
        assert result[0]["type"] == "T1w"
        call_path = mock_client.get.call_args[0][0]
        assert "/scans" in call_path
