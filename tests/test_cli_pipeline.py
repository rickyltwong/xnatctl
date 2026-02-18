"""Tests for xnatctl CLI pipeline commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


def _mock_config() -> Config:
    """Build a mock Config with a default profile."""
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
    """Build a mock XNATClient."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}
    return client


class TestPipelineList:
    """Tests for pipeline list command."""

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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["pipeline", "list", "--project", "PROJ1"]
                        )

        assert result.exit_code == 0
        mock_service.list.assert_called_once_with(project="PROJ1")

    def test_pipeline_list_quiet(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list.return_value = [
            {"name": "dcm2niix", "Name": "dcm2niix"},
        ]

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["pipeline", "list", "--quiet"]
                        )

        assert result.exit_code == 0
        assert "dcm2niix" in result.output


class TestPipelineRun:
    """Tests for pipeline run command."""

    def test_pipeline_run(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {
            "success": True,
            "pipeline": "dcm2niix",
            "experiment": "XNAT_E001",
            "job_id": "JOB123",
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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
                                "-e",
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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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
                                "-e",
                                "XNAT_E001",
                                "-P",
                                "param1=val1",
                                "-P",
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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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
                                "-e",
                                "XNAT_E001",
                                "--output",
                                "json",
                            ],
                        )

        assert result.exit_code == 0
        assert "JOB789" in result.output


class TestPipelineStatus:
    """Tests for pipeline status command."""

    def test_pipeline_status(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.status.return_value = {
            "status": "Running",
            "message": "Processing scan 3/5",
            "start_time": "2024-01-15T10:00:00",
        }

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["pipeline", "status", "JOB123"]
                        )

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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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
    """Tests for pipeline cancel command."""

    def test_cancel_with_yes(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["pipeline", "cancel", "JOB123", "--yes"]
                        )

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_service.cancel.assert_called_once_with("JOB123")

    def test_cancel_aborted(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
                with patch("xnatctl.cli.common.XNATClient", return_value=client):
                    with patch(
                        "xnatctl.cli.pipeline.PipelineService",
                        return_value=mock_service,
                    ):
                        result = runner.invoke(
                            cli, ["pipeline", "cancel", "JOB123"], input="n\n"
                        )

        assert result.exit_code != 0
        mock_service.cancel.assert_not_called()


class TestPipelineJobs:
    """Tests for pipeline jobs command."""

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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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

        with patch("xnatctl.core.config.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
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
