from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from conftest import make_response

from xnatctl.core.exceptions import OperationError, ResourceNotFoundError
from xnatctl.services.pipelines import PipelineService


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> PipelineService:
    return PipelineService(mock_client)


class TestPipelineList:
    def test_list_all(self, service: PipelineService, mock_client: MagicMock) -> None:
        rows = [{"name": "freesurfer", "description": "FreeSurfer recon-all"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.list()

        assert len(result) == 1
        assert result[0]["name"] == "freesurfer"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/pipelines"

    def test_list_by_project(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/pipelines" in call_path


class TestPipelineGet:
    def test_get_returns_dict(self, service: PipelineService, mock_client: MagicMock) -> None:
        pipeline = {"name": "freesurfer", "version": "7.0"}
        mock_client.get.return_value = make_response(pipeline)

        result = service.get("freesurfer")

        assert result["name"] == "freesurfer"

    def test_get_from_result_set(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [{"name": "freesurfer"}]}}
        )

        result = service.get("freesurfer")

        assert "ResultSet" in result

    def test_get_not_found(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.side_effect = ResourceNotFoundError("resource", "/data/pipelines/missing")

        with pytest.raises(ResourceNotFoundError):
            service.get("missing")


class TestPipelineRun:
    def test_run_returns_job_info(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.post.return_value = make_response({"jobId": "JOB123"})

        result = service.run("freesurfer", "E001")

        assert result["success"] is True
        assert result["job_id"] == "JOB123"
        assert result["pipeline"] == "freesurfer"

    def test_run_text_response(self, service: PipelineService, mock_client: MagicMock) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.json.side_effect = ValueError("not json")
        resp.text = "JOB456"
        resp.headers = {"content-type": "text/plain"}
        mock_client.post.return_value = resp

        result = service.run("freesurfer", "E001")

        assert result["job_id"] == "JOB456"

    def test_run_with_params(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.post.return_value = make_response({"jobId": "JOB789"})

        service.run("freesurfer", "E001", params={"reconall_args": "-all"})

        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["reconall_args"] == "-all"


class TestPipelineStatus:
    def test_status_dict(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"job_id": "JOB123", "status": "Running"})

        result = service.status("JOB123")

        assert result["status"] == "Running"

    def test_status_from_result_set(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response(
            {"ResultSet": {"Result": [{"status": "Complete"}]}}
        )

        result = service.status("JOB123")

        assert "ResultSet" in result

    def test_status_unknown_when_list(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = make_response([])

        result = service.status("JOB123")

        assert result["status"] == "unknown"


class TestPipelineWait:
    @patch("xnatctl.services.pipelines.time")
    def test_wait_completes(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = make_response({"status": "Complete"})

        result = service.wait("JOB123")

        assert result["status"] == "Complete"

    @patch("xnatctl.services.pipelines.time")
    def test_wait_timeout(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_time.time.side_effect = [0.0, 9999.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = make_response({"status": "Running"})

        with pytest.raises(OperationError):
            service.wait("JOB123", timeout=10)

    @patch("xnatctl.services.pipelines.time")
    def test_wait_failed_job(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = make_response({"status": "Failed", "message": "OOM"})

        with pytest.raises(OperationError):
            service.wait("JOB123")

    @patch("xnatctl.services.pipelines.time")
    def test_wait_with_callback(
        self, mock_time: MagicMock, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_time.time.side_effect = [0.0, 1.0]
        mock_time.sleep = MagicMock()
        mock_client.get.return_value = make_response({"status": "Complete"})
        callback = MagicMock()

        service.wait("JOB123", progress_callback=callback)

        callback.assert_called_once()


class TestPipelineCancel:
    def test_cancel(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.post.return_value = make_response("", content_type="text/plain")

        assert service.cancel("JOB123") is True
        post_params = mock_client.post.call_args[1]["params"]
        assert post_params["action"] == "kill"


class TestPipelineListJobs:
    def test_list_jobs_all(self, service: PipelineService, mock_client: MagicMock) -> None:
        rows = [{"job_id": "JOB1"}, {"job_id": "JOB2"}]
        mock_client.get.return_value = make_response({"ResultSet": {"Result": rows}})

        result = service.list_jobs()

        assert len(result) == 2
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/pipelines/jobs"

    def test_list_jobs_by_experiment(
        self, service: PipelineService, mock_client: MagicMock
    ) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list_jobs(experiment_id="E001")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/experiments/E001/pipelines/jobs" in call_path

    def test_list_jobs_by_project(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list_jobs(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/pipelines/jobs" in call_path

    def test_list_jobs_with_status(self, service: PipelineService, mock_client: MagicMock) -> None:
        mock_client.get.return_value = make_response({"ResultSet": {"Result": []}})

        service.list_jobs(status="Running")

        params = mock_client.get.call_args[1]["params"]
        assert params["status"] == "Running"
