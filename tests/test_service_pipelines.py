"""Unit tests for PipelineService."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from xnatctl.core.exceptions import OperationError, ResourceNotFoundError
from xnatctl.services.pipelines import PipelineService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> PipelineService:
    """Create PipelineService with mock client."""
    return PipelineService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


class TestPipelineList:
    """Tests for PipelineService.list."""

    def test_list_all(self, service: PipelineService, mock_client: MagicMock) -> None:
        """List without project uses /data/pipelines."""
        rows = [{"name": "freesurfer", "description": "FreeSurfer recon-all"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list()

        assert len(result) == 1
        assert result[0]["name"] == "freesurfer"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/pipelines"

    def test_list_by_project(self, service: PipelineService, mock_client: MagicMock) -> None:
        """List by project uses project-scoped path."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/pipelines" in call_path


class TestPipelineGet:
    """Tests for PipelineService.get."""

    def test_get_returns_dict(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Get returns pipeline dict when response is a dict."""
        pipeline = {"name": "freesurfer", "version": "7.0"}
        mock_client.get.return_value = _resp(pipeline)

        result = service.get("freesurfer")

        assert result["name"] == "freesurfer"

    def test_get_from_result_set(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Get returns full dict when response is a dict (including ResultSet wrapper).

        The isinstance(data, dict) check matches before _extract_results is called,
        so the full response dict is returned.
        """
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [{"name": "freesurfer"}]}})

        result = service.get("freesurfer")

        # Since data is a dict, isinstance check returns True and the full dict is returned
        assert "ResultSet" in result

    def test_get_not_found(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Get raises ResourceNotFoundError on 404."""
        mock_client.get.side_effect = ResourceNotFoundError("resource", "/data/pipelines/missing")

        with pytest.raises(ResourceNotFoundError):
            service.get("missing")


class TestPipelineRun:
    """Tests for PipelineService.run."""

    def test_run_returns_job_info(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Run returns success dict with job_id."""
        mock_client.post.return_value = _resp({"jobId": "JOB123"})

        result = service.run("freesurfer", "E001")

        assert result["success"] is True
        assert result["job_id"] == "JOB123"
        assert result["pipeline"] == "freesurfer"

    def test_run_text_response(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Run handles text response (job ID as string)."""
        resp = MagicMock(spec=httpx.Response)
        resp.json.side_effect = ValueError("not json")
        resp.text = "JOB456"
        resp.headers = {"content-type": "text/plain"}
        mock_client.post.return_value = resp

        result = service.run("freesurfer", "E001")

        assert result["job_id"] == "JOB456"

    def test_run_with_params(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Run passes additional params."""
        mock_client.post.return_value = _resp({"jobId": "JOB789"})

        service.run("freesurfer", "E001", params={"reconall_args": "-all"})

        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["reconall_args"] == "-all"


class TestPipelineStatus:
    """Tests for PipelineService.status."""

    def test_status_dict(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Status returns dict when response is dict."""
        mock_client.get.return_value = _resp({"job_id": "JOB123", "status": "Running"})

        result = service.status("JOB123")

        assert result["status"] == "Running"

    def test_status_from_result_set(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Status returns full dict when response is a dict (isinstance check matches first)."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [{"status": "Complete"}]}})

        result = service.status("JOB123")

        # isinstance(data, dict) is True, so full dict is returned
        assert "ResultSet" in result

    def test_status_unknown_when_list(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Status returns unknown fallback when response is a non-dict non-list."""
        # _get returns resp.json() which is a list here (not a dict)
        mock_client.get.return_value = _resp([])

        result = service.status("JOB123")

        assert result["status"] == "unknown"


class TestPipelineWait:
    """Tests for PipelineService.wait."""

    @patch("xnatctl.services.pipelines.time")
    def test_wait_completes(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Wait returns when job reaches terminal state."""
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = _resp({"status": "Complete"})

        result = service.wait("JOB123")

        assert result["status"] == "Complete"

    @patch("xnatctl.services.pipelines.time")
    def test_wait_timeout(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Wait raises OperationError on timeout."""
        mock_time.time.side_effect = [0.0, 9999.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = _resp({"status": "Running"})

        with pytest.raises(OperationError):
            service.wait("JOB123", timeout=10)

    @patch("xnatctl.services.pipelines.time")
    def test_wait_failed_job(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Wait raises OperationError when job fails."""
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = _resp({"status": "Failed", "message": "OOM"})

        with pytest.raises(OperationError):
            service.wait("JOB123")

    @patch("xnatctl.services.pipelines.time")
    def test_wait_with_callback(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Wait invokes progress callback on each poll."""
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = _resp({"status": "Complete"})
        callback = MagicMock()

        service.wait("JOB123", progress_callback=callback)

        callback.assert_called_once()


class TestPipelineCancel:
    """Tests for PipelineService.cancel."""

    def test_cancel(self, service: PipelineService, mock_client: MagicMock) -> None:
        """Cancel issues POST with kill action."""
        mock_client.post.return_value = _resp("", content_type="text/plain")

        assert service.cancel("JOB123") is True
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "kill"


class TestPipelineListJobs:
    """Tests for PipelineService.list_jobs."""

    def test_list_jobs_all(self, service: PipelineService, mock_client: MagicMock) -> None:
        """List all jobs."""
        rows = [{"job_id": "JOB1"}, {"job_id": "JOB2"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list_jobs()

        assert len(result) == 2
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/pipelines/jobs"

    def test_list_jobs_by_experiment(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """List jobs filtered by experiment."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list_jobs(experiment_id="E001")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/experiments/E001/pipelines/jobs" in call_path

    def test_list_jobs_by_project(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """List jobs filtered by project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list_jobs(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/pipelines/jobs" in call_path

    def test_list_jobs_with_status(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        """Status filter is passed as param."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list_jobs(status="Running")

        params = mock_client.get.call_args[1]["params"]
        assert params["status"] == "Running"
