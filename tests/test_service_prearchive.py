"""Unit tests for PrearchiveService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from conftest import make_response

from xnatctl.core.exceptions import (
    OperationError,
    ResourceExistsError,
    ResourceNotFoundError,
)
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
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        result = service.list()

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/prearchive"

    def test_list_by_project(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """List by project uses project-scoped path."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/prearchive/projects/PROJ01" in call_path


class TestPrearchiveGet:
    """Tests for PrearchiveService.get."""

    def test_get(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Get returns prearchive session dict."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": [SAMPLE_PREARCHIVE]}})

        result = service.get("PROJ01", "20240115_120000", "session_01")

        assert result["status"] == "READY"

    def test_get_not_found(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on empty results."""
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

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
        """Archive uses the archive service with a prearchive src path."""
        mock_client.post.return_value = make_response(
            "/data/experiments/E001", content_type="text/plain"
        )

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        assert result["project"] == "PROJ01"
        assert mock_client.post.call_args[0][0] == "/data/services/archive"
        post_data = mock_client.post.call_args[1]["data"]
        assert post_data["src"] == "/prearchive/projects/PROJ01/20240115_120000/session_01"
        assert "dest" not in post_data

    def test_archive_with_options(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Archive passes subject, label, overwrite via archive-service form data."""
        mock_client.post.return_value = make_response("", content_type="text/plain")

        service.archive(
            "PROJ01",
            "20240115_120000",
            "session_01",
            subject="SUB01",
            experiment_label="MR001",
            overwrite=True,
        )

        post_data = mock_client.post.call_args[1]["data"]
        assert post_data["dest"] == "/archive/projects/PROJ01/subjects/SUB01/experiments/MR001"
        assert post_data["overwrite"] == "delete"

    def test_archive_label_without_subject_uses_prearchive_subject(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """Archive can infer the subject from prearchive metadata when relabeling."""
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [{"subject": "SUB01", **SAMPLE_PREARCHIVE}]}}
        )
        mock_client.post.return_value = make_response("", content_type="text/plain")

        service.archive(
            "PROJ01",
            "20240115_120000",
            "session_01",
            experiment_label="MR001",
        )

        post_data = mock_client.post.call_args[1]["data"]
        assert post_data["dest"] == "/archive/projects/PROJ01/subjects/SUB01/experiments/MR001"

    def test_archive_encodes_archive_service_segments(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """Archive service request should encode dot and slash path segments."""
        mock_client.post.return_value = make_response("", content_type="text/plain")

        service.archive(
            "..",
            "2024.01.15",
            "session/name",
            subject="SUB/01",
            experiment_label="MR.001",
        )

        post_data = mock_client.post.call_args[1]["data"]
        assert post_data["src"] == "/prearchive/projects/%2E%2E/2024%2E01%2E15/session%2Fname"
        assert (
            post_data["dest"] == "/archive/projects/%2E%2E/subjects/SUB%2F01/experiments/MR%2E001"
        )

    def test_archive_not_in_prearchive_raises_actionable_error(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """A 404 from the archive service becomes an idempotency-aware error (issue #20).

        When the prearchive path is gone (typically because the session was
        already archived), the bare ``resource not found: /data/services/archive``
        is misleading. The archive must raise an actionable OperationError that
        names the session and points at how to verify it landed.
        """
        mock_client.post.side_effect = ResourceNotFoundError("resource", "/data/services/archive")

        with pytest.raises(OperationError) as excinfo:
            service.archive("COG01_ROM", "20260717_110001982", "COG01_ROM_00005034_01_SE01_MR")

        message = str(excinfo.value)
        assert "COG01_ROM_00005034_01_SE01_MR" in message
        assert "already be archived" in message
        assert "session show -P COG01_ROM -E COG01_ROM_00005034_01_SE01_MR" in message


class TestPrearchiveRedirectHandling:
    """Regression tests: XNAT answers move/archive with a 301 to the new location.

    The HTTP client follows redirects (``follow_redirects=True``), so these
    operations must be reported as success rather than surfacing an httpx
    'Redirect response 301' error (see issue #19). Uses a real XNATClient over
    a MockTransport so the redirect is actually exercised end to end.
    """

    def _client(self, handler: object) -> PrearchiveService:
        from xnatctl.core.client import XNATClient

        client = XNATClient(base_url="https://xnat.example.org", username="u", password="p")
        client.session_token = "tok"
        client._client = httpx.Client(
            base_url=client.base_url,
            verify=False,
            follow_redirects=True,
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        )
        return PrearchiveService(client)

    def test_move_301_redirect_is_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and "action=move" in str(request.url):
                return httpx.Response(
                    301,
                    headers={"Location": "/data/prearchive/projects/PROJ02/ts/session_01"},
                )
            return httpx.Response(
                200,
                json={"ResultSet": {"Result": [{"project": "PROJ02"}]}},
                headers={"content-type": "application/json"},
            )

        service = self._client(handler)
        result = service.move("PROJ01", "20240115_120000", "session_01", "PROJ02")

        assert result["success"] is True
        assert result["target_project"] == "PROJ02"


class TestPrearchiveDelete:
    """Tests for PrearchiveService.delete."""

    def test_delete(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Delete returns True."""
        mock_client.delete.return_value = make_response("")

        assert service.delete("PROJ01", "20240115_120000", "session_01") is True


class TestPrearchiveRebuild:
    """Tests for PrearchiveService.rebuild."""

    def test_rebuild(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Rebuild issues POST with rebuild action."""
        mock_client.post.return_value = make_response("", content_type="text/plain")

        result = service.rebuild("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "rebuild"


class TestPrearchiveMove:
    """Tests for PrearchiveService.move."""

    def test_move(self, service: PrearchiveService, mock_client: MagicMock) -> None:
        """Move issues POST with move action and target project."""
        mock_client.post.return_value = make_response("", content_type="text/plain")

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
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.get_scans("PROJ01", "20240115_120000", "session_01")

        assert len(result) == 1
        assert result[0]["type"] == "T1w"
        call_path = mock_client.get.call_args[0][0]
        assert "/scans" in call_path


# =============================================================================
# Response inspection (ROB-10)
#
# XNAT's prearchive services answer HTTP 200 with an error-shaped body instead
# of a 4xx, so these verbs used to return {"success": True} regardless.
# =============================================================================


class TestArchiveResponseInspection:
    def test_experiment_uri_is_surfaced(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(
            "/data/experiments/XNAT_E00042", content_type="text/plain"
        )

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        assert result["experiment_uri"] == "/data/experiments/XNAT_E00042"

    def test_archive_prefixed_uri_is_recognised(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(
            "/data/archive/experiments/XNAT_E00042", content_type="text/plain"
        )

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["experiment_uri"] == "/data/archive/experiments/XNAT_E00042"

    @pytest.mark.parametrize(
        "body",
        [
            "Conflict: session already exists in the archive",
            "The experiment already exists",
        ],
    )
    def test_conflict_body_raises_resource_exists(
        self, body: str, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(body, content_type="text/plain")

        with pytest.raises(ResourceExistsError) as exc:
            service.archive("PROJ01", "20240115_120000", "session_01")

        assert body[:40] in str(exc.value)

    def test_error_body_raises_operation_error_with_snippet(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(
            "java.lang.NullPointerException: archive failed", content_type="text/plain"
        )

        with pytest.raises(OperationError) as exc:
            service.archive("PROJ01", "20240115_120000", "session_01")

        assert "NullPointerException" in str(exc.value)
        assert exc.value.details["project"] == "PROJ01"

    def test_empty_body_is_still_success(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """Some deployments answer with a bare 200; that must not become a failure."""
        mock_client.post.return_value = make_response("", content_type="text/plain")

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        assert result["experiment_uri"] is None

    def test_uri_body_wins_over_incidental_error_word(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """A success report naming an experiment is not an error, whatever the prose."""
        mock_client.post.return_value = make_response(
            "Archived with 0 errors: /data/experiments/XNAT_E00042",
            content_type="text/plain",
        )

        result = service.archive("PROJ01", "20240115_120000", "session_01")

        assert result["success"] is True
        assert result["experiment_uri"] == "/data/experiments/XNAT_E00042"


class TestRebuildMoveResponseInspection:
    def test_rebuild_error_body_raises(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(
            "Rebuild failed: session locked", content_type="text/plain"
        )

        with pytest.raises(OperationError):
            service.rebuild("PROJ01", "20240115_120000", "session_01")

    def test_move_error_body_raises_with_target_in_details(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response(
            "Error: destination project not found", content_type="text/plain"
        )

        with pytest.raises(OperationError) as exc:
            service.move("PROJ01", "20240115_120000", "session_01", "PROJ02")

        assert exc.value.details["target_project"] == "PROJ02"

    def test_rebuild_clean_body_still_succeeds(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.post.return_value = make_response("OK", content_type="text/plain")

        assert service.rebuild("PROJ01", "20240115_120000", "session_01")["success"] is True


class TestGetNotFoundScoping:
    def test_typed_404_is_rescoped_to_the_prearchive_session(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        mock_client.get.side_effect = ResourceNotFoundError("resource", "/data/prearchive/...")

        with pytest.raises(ResourceNotFoundError) as exc:
            service.get("PROJ01", "20240115_120000", "session_01")

        assert "PROJ01/20240115_120000/session_01" in str(exc.value)

    def test_unrelated_error_containing_404_propagates_unchanged(
        self, service: PrearchiveService, mock_client: MagicMock
    ) -> None:
        """Regression: `if "404" in str(e)` also fired on a session labelled SUB404."""
        boom = OperationError("get", "upstream exploded for subject SUB404")
        mock_client.get.side_effect = boom

        with pytest.raises(OperationError) as exc:
            service.get("PROJ01", "20240115_120000", "SUB404")

        assert exc.value is boom, "must propagate unchanged, not become a 404"
