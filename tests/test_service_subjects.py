"""Unit tests for SubjectService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.core.exceptions import ResourceNotFoundError, ValidationError
from xnatctl.models.subject import Subject
from xnatctl.services.subjects import SubjectService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> SubjectService:
    """Create SubjectService with mock client."""
    return SubjectService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


SAMPLE_SUBJECT = {
    "ID": "XNAT_S00001",
    "label": "SUB001",
    "project": "PROJ01",
    "URI": "/data/subjects/XNAT_S00001",
}


class TestSubjectList:
    """Tests for SubjectService.list."""

    def test_list_all(self, service: SubjectService, mock_client: MagicMock) -> None:
        """List without project uses /data/subjects."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        result = service.list()

        assert len(result) == 1
        assert isinstance(result[0], Subject)
        assert result[0].id == "XNAT_S00001"
        call_args = mock_client.get.call_args[0][0]
        assert call_args == "/data/subjects"

    def test_list_by_project(self, service: SubjectService, mock_client: MagicMock) -> None:
        """List with project filters by project path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        service.list(project="PROJ01")

        call_args = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects" in call_args

    def test_list_with_limit(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Limit truncates results."""
        rows = [{**SAMPLE_SUBJECT, "ID": f"S{i:05d}", "label": f"SUB{i:03d}"} for i in range(5)]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list(limit=3)

        assert len(result) == 3

    def test_list_with_columns(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Columns param is joined and passed."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(columns=["ID", "label"])

        params = mock_client.get.call_args[1]["params"]
        assert params["columns"] == "ID,label"


class TestSubjectGet:
    """Tests for SubjectService.get."""

    def test_get_by_id(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Get subject by ID."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        result = service.get("XNAT_S00001")

        assert isinstance(result, Subject)
        assert result.label == "SUB001"

    def test_get_with_project(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Get subject scoped to project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        service.get("SUB001", project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB001" in call_path

    def test_get_not_found(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        with pytest.raises(ResourceNotFoundError):
            service.get("MISSING")


class TestSubjectCreate:
    """Tests for SubjectService.create."""

    def test_create(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Create issues PUT then fetches subject."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        result = service.create("PROJ01", "SUB001", gender="male", yob=1990)

        assert isinstance(result, Subject)
        mock_client.put.assert_called_once()
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["gender"] == "male"
        assert put_params["yob"] == "1990"


class TestSubjectDelete:
    """Tests for SubjectService.delete."""

    def test_delete_with_project(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Delete uses project-scoped path."""
        mock_client.delete.return_value = _resp("")

        assert service.delete("SUB001", project="PROJ01") is True
        call_path = mock_client.delete.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB001" in call_path

    def test_delete_without_project(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Delete without project uses global path."""
        mock_client.delete.return_value = _resp("")

        service.delete("XNAT_S00001")

        call_path = mock_client.delete.call_args[0][0]
        assert "/data/subjects/XNAT_S00001" in call_path

    def test_delete_with_remove_files(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Delete passes removeFiles param."""
        mock_client.delete.return_value = _resp("")

        service.delete("SUB001", project="PROJ01", remove_files=True)

        params = mock_client.delete.call_args[1]["params"]
        assert params["removeFiles"] == "true"


class TestSubjectRename:
    """Tests for SubjectService.rename."""

    def test_rename(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Rename issues PUT with new label, then fetches."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        new_subject = {**SAMPLE_SUBJECT, "label": "SUB002"}
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [new_subject]}})

        result = service.rename("SUB001", "SUB002", project="PROJ01")

        assert result.label == "SUB002"
        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["label"] == "SUB002"


class TestSubjectRenameBatch:
    """Tests for SubjectService.rename_batch."""

    def test_rename_batch_dry_run(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Dry run verifies subjects exist but does not rename."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        result = service.rename_batch("PROJ01", {"SUB001": "SUB_NEW"}, dry_run=True)

        assert len(result["renamed"]) == 1
        assert result["dry_run"] is True
        mock_client.put.assert_not_called()

    def test_rename_batch_not_found_skips(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Missing subjects are skipped."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        result = service.rename_batch("PROJ01", {"MISSING": "NEW"}, dry_run=True)

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "not found"


class TestSubjectRenamePattern:
    """Tests for SubjectService.rename_pattern."""

    def test_invalid_regex(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Invalid regex raises ValidationError."""
        with pytest.raises(ValidationError):
            service.rename_pattern("PROJ01", "[invalid", "{1}")

    def test_pattern_no_change_skipped(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Subjects where old == new label are skipped."""
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [SAMPLE_SUBJECT]}}
        )

        result = service.rename_pattern("PROJ01", r"^(SUB001)$", "{1}", dry_run=True)

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "no change"

    def test_pattern_rename_dry_run(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Pattern rename in dry-run mode."""
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [SAMPLE_SUBJECT]}}
        )

        result = service.rename_pattern("PROJ01", r"^SUB(\d+)$", "SUBJ_{1}", dry_run=True)

        assert len(result["renamed"]) == 1
        assert result["renamed"][0]["to"] == "SUBJ_001"

    def test_pattern_merge_skipped_without_flag(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Multiple subjects mapping to same target are skipped without merge flag."""
        subjects = [
            {**SAMPLE_SUBJECT, "ID": "S1", "label": "SUB_A1"},
            {**SAMPLE_SUBJECT, "ID": "S2", "label": "SUB_A2"},
        ]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": subjects}})

        result = service.rename_pattern("PROJ01", r"^SUB_A\d$", "SUB_A", dry_run=True)

        assert len(result["skipped"]) == 2


class TestSubjectGetSessions:
    """Tests for SubjectService.get_sessions."""

    def test_get_sessions_with_project(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """get_sessions with project uses project-scoped path."""
        rows = [{"ID": "EXP01"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.get_sessions("SUB001", project="PROJ01")

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB001/experiments" in call_path


class TestSubjectMerge:
    """Tests for SubjectService.merge_subjects."""

    def test_merge_dry_run(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Dry-run merge reports experiments without modifying."""
        source = {**SAMPLE_SUBJECT, "ID": "S1", "label": "SRC"}
        target = {**SAMPLE_SUBJECT, "ID": "S2", "label": "TGT"}

        def get_side_effect(path: str, **kwargs: object) -> MagicMock:
            if "SRC" in path and "experiments" in path:
                return _resp({"ResultSet": {"Result": [{"ID": "EXP01"}]}})
            if "SRC" in path:
                return _resp({"ResultSet": {"Result": [source]}})
            if "TGT" in path:
                return _resp({"ResultSet": {"Result": [target]}})
            return _resp({"ResultSet": {"Result": []}})

        mock_client.get.side_effect = get_side_effect

        result = service.merge_subjects("PROJ01", "SRC", "TGT", dry_run=True)

        assert result["experiments_moved"] == 1
        assert result["source_deleted"] is True
        assert result["dry_run"] is True
        mock_client.put.assert_not_called()
        mock_client.delete.assert_not_called()
