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

    def test_get_items_response(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Get subject handles `items[]` detail responses."""
        mock_client.get.return_value = _resp(
            {
                "items": [
                    {
                        "data_fields": {
                            "ID": "XNAT_S00001",
                            "label": "SUB001",
                            "project": "PROJ01",
                        }
                    }
                ]
            }
        )

        result = service.get("XNAT_S00001")

        assert isinstance(result, Subject)
        assert result.label == "SUB001"

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
        """Delete uses project-scoped path (when subject has no experiments)."""
        # Empty experiments list so the safety guard allows the delete.
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})
        mock_client.delete.return_value = _resp("")

        assert service.delete("SUB001", project="PROJ01") is True
        call_path = mock_client.delete.call_args[0][0]
        assert "/data/projects/PROJ01/subjects/SUB001" in call_path

    def test_delete_without_project(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Delete without project uses global path (safety guard skipped)."""
        # Safety check is skipped when no project is supplied because
        # get_sessions requires a project scope.
        mock_client.delete.return_value = _resp("")

        service.delete("XNAT_S00001")

        call_path = mock_client.delete.call_args[0][0]
        assert "/data/subjects/XNAT_S00001" in call_path

    def test_delete_with_remove_files(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Delete passes removeFiles param."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})
        mock_client.delete.return_value = _resp("")

        service.delete("SUB001", project="PROJ01", remove_files=True)

        params = mock_client.delete.call_args[1]["params"]
        assert params["removeFiles"] == "true"

    def test_delete_refuses_when_subject_has_experiments(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Refuse to delete a subject that still has attached experiments.

        XNAT would cascade-delete the experiments — the exact failure mode
        of the OXD01_CMH PET-session deletion incident.
        """
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [{"ID": "EXP01"}, {"ID": "EXP02"}]}}
        )

        with pytest.raises(RuntimeError, match="still attached"):
            service.delete("SUB001", project="PROJ01")

        mock_client.delete.assert_not_called()

    def test_delete_force_overrides_guard(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """force=True skips the experiment-attached safety check."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [{"ID": "EXP01"}]}})
        mock_client.delete.return_value = _resp("")

        assert service.delete("SUB001", project="PROJ01", force=True) is True
        mock_client.delete.assert_called_once()


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
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

        result = service.rename_pattern("PROJ01", r"^(SUB001)$", "{1}", dry_run=True)

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "no change"

    def test_pattern_rename_dry_run(self, service: SubjectService, mock_client: MagicMock) -> None:
        """Pattern rename in dry-run mode."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [SAMPLE_SUBJECT]}})

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

    def test_merge_uses_scoped_put_to_target_subject(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Regression test for OXD01_CMH PET-session deletion incident.

        The reassignment MUST use the project+target-subject-scoped PUT
        (same URI shape as the XNAT web UI), with the target subject's
        internal XNAT ID in the URL path. An earlier implementation used
        PUT /data/experiments/{id}?xnat:experimentData/subject_ID=... which
        was silently destructive on the live XNAT — the experiment was
        neither left on source nor moved to target, and the source DELETE
        completed with nothing to cascade. Do not reintroduce that pattern.
        """
        source = {**SAMPLE_SUBJECT, "ID": "XNAT_S00001", "label": "SRC"}
        target = {**SAMPLE_SUBJECT, "ID": "XNAT_S99999", "label": "TGT"}

        get_calls: list[str] = []

        def get_side_effect(path: str, **kwargs: object) -> MagicMock:
            get_calls.append(path)
            if "SRC" in path and "experiments" in path:
                # First call returns the source experiment; post-reassign
                # fail-safe call returns empty (all moved successfully).
                prior_src_exps = sum(1 for p in get_calls if "SRC" in p and "experiments" in p)
                if prior_src_exps == 1:
                    return _resp({"ResultSet": {"Result": [{"ID": "EXP01"}]}})
                return _resp({"ResultSet": {"Result": []}})
            if "/data/experiments/EXP01" in path:
                # Verification GET after the reassignment PUT.
                return _resp(
                    {
                        "items": [
                            {
                                "data_fields": {
                                    "ID": "EXP01",
                                    "subject_ID": "XNAT_S99999",
                                    "project": "PROJ01",
                                },
                                "meta": {},
                            }
                        ]
                    }
                )
            if "SRC" in path:
                return _resp({"ResultSet": {"Result": [source]}})
            if "TGT" in path:
                return _resp({"ResultSet": {"Result": [target]}})
            return _resp({"ResultSet": {"Result": []}})

        mock_client.get.side_effect = get_side_effect
        mock_client.put.return_value = _resp("")
        mock_client.delete.return_value = _resp("")

        service.merge_subjects("PROJ01", "SRC", "TGT")

        reassign_calls = [
            c
            for c in mock_client.put.call_args_list
            if "/subjects/XNAT_S99999/experiments/EXP01" in c.args[0]
        ]
        assert len(reassign_calls) == 1, (
            f"Expected one scoped reassignment PUT to target subject; "
            f"got {len(reassign_calls)}. All PUTs: "
            f"{[c.args[0] for c in mock_client.put.call_args_list]}"
        )

        put_path = reassign_calls[0].args[0]
        assert put_path == "/data/projects/PROJ01/subjects/XNAT_S99999/experiments/EXP01", (
            f"PUT must be scoped to project+target-subject+experiment. Got: {put_path}"
        )

        # Must NOT use the old (destructive) global endpoint with the
        # xnat:experimentData/subject_ID XML-path-shortcut querystring.
        global_calls = [
            c for c in mock_client.put.call_args_list if c.args[0] == "/data/experiments/EXP01"
        ]
        assert not global_calls, (
            "Must not PUT to /data/experiments/{id} — that shape was silently "
            "destructive on the live XNAT and caused the OXD01_CMH incident."
        )
        for call in mock_client.put.call_args_list:
            params = call.kwargs.get("params", {})
            assert "xnat:experimentData/subject_ID" not in params, (
                "Must not pass xnat:experimentData/subject_ID — target is "
                "encoded in the URL path, not an XML-path-shortcut param."
            )

        # Audit metadata matching the web UI.
        params = reassign_calls[0].kwargs.get("params", {})
        assert params.get("event_type") == "WEB_FORM"
        assert params.get("event_action") == "Modified subject"

    def test_merge_aborts_if_verification_detects_wrong_subject_id(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """If the post-PUT verify GET shows the experiment's subject_ID did
        not change to the target, abort before deleting the source.

        This is the guard that the original OXD01_CMH "use internal ID" fix
        was missing. Verified absence is not the same as verified presence.
        """
        source = {**SAMPLE_SUBJECT, "ID": "XNAT_S00001", "label": "SRC"}
        target = {**SAMPLE_SUBJECT, "ID": "XNAT_S99999", "label": "TGT"}

        def get_side_effect(path: str, **kwargs: object) -> MagicMock:
            if "SRC" in path and "experiments" in path:
                return _resp({"ResultSet": {"Result": [{"ID": "EXP01"}]}})
            if "/data/experiments/EXP01" in path:
                # Simulate the destructive PUT: subject_ID unchanged (or
                # pointing elsewhere). The verify step must catch this.
                return _resp(
                    {
                        "items": [
                            {
                                "data_fields": {
                                    "ID": "EXP01",
                                    "subject_ID": "XNAT_S00001",
                                    "project": "PROJ01",
                                },
                                "meta": {},
                            }
                        ]
                    }
                )
            if "SRC" in path:
                return _resp({"ResultSet": {"Result": [source]}})
            if "TGT" in path:
                return _resp({"ResultSet": {"Result": [target]}})
            return _resp({"ResultSet": {"Result": []}})

        mock_client.get.side_effect = get_side_effect
        mock_client.put.return_value = _resp("")

        with pytest.raises(RuntimeError, match="did not take effect"):
            service.merge_subjects("PROJ01", "SRC", "TGT")

        mock_client.delete.assert_not_called()

    def test_merge_aborts_if_source_still_has_experiments_after_loop(
        self, service: SubjectService, mock_client: MagicMock
    ) -> None:
        """Defence in depth: even if every per-experiment verify passes,
        re-list source experiments before deleting. If any remain, abort.
        """
        source = {**SAMPLE_SUBJECT, "ID": "XNAT_S00001", "label": "SRC"}
        target = {**SAMPLE_SUBJECT, "ID": "XNAT_S99999", "label": "TGT"}

        def get_side_effect(path: str, **kwargs: object) -> MagicMock:
            if "SRC" in path and "experiments" in path:
                # Both the initial list AND the post-loop recheck return the
                # same experiment. Per-experiment verify passes (below), so
                # this tests the outer defence-in-depth guard only.
                return _resp({"ResultSet": {"Result": [{"ID": "EXP01"}]}})
            if "/data/experiments/EXP01" in path:
                return _resp(
                    {
                        "items": [
                            {
                                "data_fields": {
                                    "ID": "EXP01",
                                    "subject_ID": "XNAT_S99999",
                                    "project": "PROJ01",
                                },
                                "meta": {},
                            }
                        ]
                    }
                )
            if "SRC" in path:
                return _resp({"ResultSet": {"Result": [source]}})
            if "TGT" in path:
                return _resp({"ResultSet": {"Result": [target]}})
            return _resp({"ResultSet": {"Result": []}})

        mock_client.get.side_effect = get_side_effect
        mock_client.put.return_value = _resp("")

        with pytest.raises(RuntimeError, match="still attached to source"):
            service.merge_subjects("PROJ01", "SRC", "TGT")

        mock_client.delete.assert_not_called()
