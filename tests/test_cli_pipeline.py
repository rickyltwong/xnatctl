from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_config() -> Config:
    return Config(
        default_profile="default",
        profiles={
            "default": Profile(
                url="https://xnat.example.org",
                username="testuser",
                password="testpass",
                verify_ssl=False,
            )
        },
    )


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}
    return client


class TestPipelineList:
    def test_pipeline_list(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {
                "name": "dcm2niix",
                "description": "DICOM to NIfTI conversion",
                "version": "1.0",
                "path": "/pipelines/dcm2niix",
            },
            {
                "name": "freesurfer",
                "description": "FreeSurfer recon-all",
                "version": "7.3",
                "path": "/pipelines/freesurfer",
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "list"])

        assert result.exit_code == 0

    def test_pipeline_list_with_project(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "list", "--project", "PROJ1"])

        assert result.exit_code == 0
        mock_service.list.assert_called_once_with(project="PROJ1")

    def test_pipeline_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {"name": "dcm2niix", "Name": "dcm2niix"},
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "list", "--quiet"])

        assert result.exit_code == 0
        assert "dcm2niix" in result.output


class TestPipelineRun:
    def test_pipeline_run(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {
            "success": True,
            "pipeline": "dcm2niix",
            "experiment": "XNAT_E001",
            "job_id": "JOB123",
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "run",
                                "dcm2niix",
                                "-E",
                                "XNAT_E001",
                            ],
                        )

        assert result.exit_code == 0
        assert "JOB123" in result.output

    def test_pipeline_run_with_params(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {
            "success": True,
            "job_id": "JOB456",
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "run",
                                "myproc",
                                "-E",
                                "XNAT_E001",
                                "--param",
                                "param1=val1",
                                "--param",
                                "param2=val2",
                            ],
                        )

        assert result.exit_code == 0
        call_kwargs = mock_service.run.call_args[1]
        assert call_kwargs["params"] == {"param1": "val1", "param2": "val2"}

    def test_pipeline_run_json_output(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {
            "success": True,
            "pipeline": "dcm2niix",
            "experiment": "XNAT_E001",
            "job_id": "JOB789",
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "run",
                                "dcm2niix",
                                "-E",
                                "XNAT_E001",
                                "--output",
                                "json",
                            ],
                        )

        assert result.exit_code == 0
        assert "JOB789" in result.output


class TestPipelineStatus:
    def test_pipeline_status(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.status.return_value = {
            "status": "Running",
            "message": "Processing scan 3/5",
            "start_time": "2024-01-15T10:00:00",
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "status", "JOB123"])

        assert result.exit_code == 0
        assert "Running" in result.output

    def test_pipeline_status_json(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.status.return_value = {
            "status": "Complete",
            "start_time": "2024-01-15T10:00:00",
            "end_time": "2024-01-15T10:30:00",
        }

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            ["pipeline", "status", "JOB123", "--output", "json"],
                        )

        assert result.exit_code == 0
        assert "Complete" in result.output


class TestPipelineCancel:
    def test_cancel_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "cancel", "JOB123", "--yes"])

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_service.cancel.assert_called_once_with("JOB123")

    def test_cancel_aborted(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "cancel", "JOB123"], input="n\n")

        assert result.exit_code != 0
        mock_service.cancel.assert_not_called()


class TestPipelineJobs:
    def test_pipeline_jobs(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = [
            {
                "id": "JOB1",
                "pipeline": "dcm2niix",
                "experiment": "XNAT_E001",
                "status": "Complete",
                "start_time": "2024-01-15T10:00:00",
                "end_time": "2024-01-15T10:05:00",
            },
        ]

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(cli, ["pipeline", "jobs"])

        assert result.exit_code == 0

    def test_pipeline_jobs_with_filters(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = []

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "jobs",
                                "--experiment",
                                "XNAT_E001",
                                "--status",
                                "Running",
                                "--limit",
                                "10",
                            ],
                        )

        assert result.exit_code == 0
        mock_service.list_jobs.assert_called_once_with(
            experiment_id="XNAT_E001",
            project=None,
            status="Running",
            limit=10,
        )

    @staticmethod
    def _job(id_: str, status: str) -> dict[str, str]:
        return {
            "id": id_,
            "pipeline": "p",
            "experiment": "e",
            "status": status,
            "start_time": "",
            "end_time": "",
        }

    def test_pipeline_jobs_filter_sort_limit_sees_beyond_the_server_window(
        self, runner: CliRunner
    ) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        def fake_list_jobs(
            experiment_id=None, project=None, status=None, limit=None
        ) -> list[dict[str, str]]:
            if limit is None:
                return [
                    self._job("C3", "Complete"),
                    *(self._job(f"A{i:03d}", "Running") for i in range(30)),
                    self._job("C1", "Complete"),
                    *(self._job(f"A{i:03d}", "Running") for i in range(30, 60)),
                    self._job("C5", "Complete"),
                    *(self._job(f"A{i:03d}", "Running") for i in range(60, 90)),
                    self._job("C2", "Complete"),
                    *(self._job(f"A{i:03d}", "Running") for i in range(90, 120)),
                    self._job("C4", "Complete"),
                    *(self._job(f"A{i:03d}", "Running") for i in range(120, 150)),
                ]
            return [self._job(f"A{i:03d}", "Running") for i in range(limit)]

        mock_service.list_jobs.side_effect = fake_list_jobs

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "jobs",
                                "--filter",
                                "status:Complete",
                                "--sort-by",
                                "id",
                                "--limit",
                                "3",
                                "-o",
                                "json",
                            ],
                        )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert [r["id"] for r in rows] == ["C1", "C2", "C3"]

    def test_pipeline_jobs_sort_only_still_fetches_unbounded(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        def fake_list_jobs(
            experiment_id=None, project=None, status=None, limit=None
        ) -> list[dict[str, str]]:
            if limit is None:
                return [self._job(f"{i:03d}", "Complete") for i in range(150, 0, -1)]
            return [self._job(f"X{i}", "Complete") for i in range(limit)]

        mock_service.list_jobs.side_effect = fake_list_jobs

        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli,
                            [
                                "pipeline",
                                "jobs",
                                "--sort-by",
                                "id",
                                "--limit",
                                "3",
                                "-o",
                                "json",
                            ],
                        )

        assert result.exit_code == 0
        rows = json.loads(result.output[result.output.index("[") :])
        assert [r["id"] for r in rows] == ["001", "002", "003"]
