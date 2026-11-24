from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner
from conftest import (
    AuthenticatedCLI,
    authenticated_seams,
    config_seam,
    core_config_seam,
    make_authenticated_cli,
    make_authenticated_context,
)

from xnatctl.cli.common import deprecation_message
from xnatctl.cli.common.deprecation import _make_forwarding_alias_cb
from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile

from .test_cli_container import SAMPLE_CONTAINER, _stream_body_ctx


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


class TestExperimentShortFlag:
    def test_pipeline_run_new_spelling(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {"success": True, "job_id": "JOB1"}

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.pipeline.PipelineService", return_value=mock_service):
                    result = runner.invoke(cli, ["pipeline", "run", "dcm2niix", "-E", "XNAT_E001"])

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr
        mock_service.run.assert_called_once()
        assert mock_service.run.call_args[1]["experiment_id"] == "XNAT_E001"

    def test_pipeline_run_old_spelling_still_works_and_warns(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.run.return_value = {"success": True, "job_id": "JOB1"}

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.pipeline.PipelineService", return_value=mock_service):
                    result = runner.invoke(cli, ["pipeline", "run", "dcm2niix", "-e", "XNAT_E001"])

        assert result.exit_code == 0, result.output
        assert mock_service.run.call_args[1]["experiment_id"] == "XNAT_E001"
        assert deprecation_message("-e") in result.stderr
        assert "deprecated" not in result.stdout

    def test_pipeline_jobs_new_spelling(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = []

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.pipeline.PipelineService", return_value=mock_service):
                    result = runner.invoke(cli, ["pipeline", "jobs", "-E", "XNAT_E001"])

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr
        assert mock_service.list_jobs.call_args[1]["experiment_id"] == "XNAT_E001"

    def test_pipeline_jobs_old_spelling_still_works_and_warns(self, runner: CliRunner) -> None:
        client = _mock_client()
        mock_service = MagicMock()
        mock_service.list_jobs.return_value = []

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.pipeline.PipelineService", return_value=mock_service):
                    result = runner.invoke(cli, ["pipeline", "jobs", "-e", "XNAT_E001"])

        assert result.exit_code == 0, result.output
        assert mock_service.list_jobs.call_args[1]["experiment_id"] == "XNAT_E001"
        assert deprecation_message("-e") in result.stderr

    def test_admin_refresh_catalogs_new_spelling(self, runner: CliRunner) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                    {"ID": "XNAT_E002", "subject_ID": "XNAT_S002", "label": "Sess2"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(
                    cli,
                    [
                        "admin",
                        "refresh-catalogs",
                        "TESTPROJ",
                        "-E",
                        "XNAT_E001",
                        "--workers",
                        "1",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr
        assert "Refreshed" in result.output

    def test_admin_refresh_catalogs_old_spelling_still_works_and_warns(
        self, runner: CliRunner
    ) -> None:
        client = _mock_client()
        client.get_json.return_value = {
            "ResultSet": {
                "Result": [
                    {"ID": "XNAT_E001", "subject_ID": "XNAT_S001", "label": "Sess1"},
                    {"ID": "XNAT_E002", "subject_ID": "XNAT_S002", "label": "Sess2"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client.post.return_value = mock_resp

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(
                    cli,
                    [
                        "admin",
                        "refresh-catalogs",
                        "TESTPROJ",
                        "-e",
                        "XNAT_E001",
                        "--workers",
                        "1",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "Refreshed" in result.output
        assert deprecation_message("-e") in result.stderr


class TestResourceOutputFile:
    def test_output_file_new_spelling(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        out = tmp_path / "out.zip"

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.resource.stream_to_file") as mock_stream:
                    mock_stream.return_value = MagicMock(files=1, bytes=10)
                    result = runner.invoke(
                        cli,
                        ["resource", "download", "XNAT_E00001", "DICOM", "--output-file", str(out)],
                    )

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr

    def test_short_flag_f_still_maps_to_output_file_without_warning(
        self, runner: CliRunner, tmp_path
    ) -> None:
        client = _mock_client()
        out = tmp_path / "out.zip"

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.resource.stream_to_file") as mock_stream:
                    mock_stream.return_value = MagicMock(files=1, bytes=10)
                    result = runner.invoke(
                        cli, ["resource", "download", "XNAT_E00001", "DICOM", "-f", str(out)]
                    )

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr

    def test_file_old_spelling_still_works_and_warns(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        out = tmp_path / "out.zip"

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.resource.stream_to_file") as mock_stream:
                    mock_stream.return_value = MagicMock(files=1, bytes=10)
                    result = runner.invoke(
                        cli,
                        ["resource", "download", "XNAT_E00001", "DICOM", "--file", str(out)],
                    )

        assert result.exit_code == 0, result.output
        assert deprecation_message("--file") in result.stderr

    def test_missing_output_file_is_a_usage_error(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(cli, ["resource", "download", "XNAT_E00001", "DICOM"])

        assert result.exit_code == 2
        assert "--output-file" in result.output


class TestApiFileShortFlag:
    def test_api_post_new_spelling(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp
        body = tmp_path / "body.json"
        body.write_text('{"a": 1}')

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(cli, ["api", "post", "/data/foo", "--file", str(body)])

        assert result.exit_code == 0, result.output
        assert result.stderr.count("deprecated") == 0

    def test_api_post_old_spelling_still_works_and_warns(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.post.return_value = mock_resp
        body = tmp_path / "body.json"
        body.write_text('{"a": 1}')

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(cli, ["api", "post", "/data/foo", "-f", str(body)])

        assert result.exit_code == 0, result.output
        assert deprecation_message("-f (api post/put)") in result.stderr
        assert "deprecated" not in result.stdout

    def test_api_put_old_spelling_still_works_and_warns(self, runner: CliRunner, tmp_path) -> None:
        client = _mock_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "ok"
        client.put.return_value = mock_resp
        body = tmp_path / "body.json"
        body.write_text('{"a": 1}')

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(cli, ["api", "put", "/data/foo", "-f", str(body)])

        assert result.exit_code == 0, result.output
        assert deprecation_message("-f (api post/put)") in result.stderr


class TestContainerFollowShortFlag:
    def test_follow_new_spelling(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "0.01"]
        )

        assert result.exit_code == 0
        assert "deprecated" not in result.stderr
        assert result.stdout == "hello"

    def test_short_flag_f_still_works_and_warns(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(["container", "logs", "501", "-f", "--interval", "0.01"])

        assert result.exit_code == 0
        assert result.stdout == "hello"
        assert deprecation_message("-f (container logs)") in result.stderr


class TestScanShortFlag:
    def test_scan_delete_new_spelling_dry_run(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                ["scan", "delete", "-E", "XNAT_E00001", "--scans", "1,2", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert "deprecated" not in result.stderr
        assert "2 scans" in result.stderr

    def test_scan_delete_old_spelling_still_works_and_warns(self, runner: CliRunner) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli, ["scan", "delete", "-E", "XNAT_E00001", "-s", "1,2", "--dry-run"]
            )

        assert result.exit_code == 0, result.output
        assert "2 scans" in result.stderr
        assert deprecation_message("-s") in result.stderr

    def test_scan_download_new_spelling_dry_run(self, runner: CliRunner, tmp_path) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1,2",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert result.stderr == "" or "deprecated" not in result.stderr
        assert "2 scans" in result.output

    def test_scan_download_repeatable_scans_matches_comma_list(
        self, runner: CliRunner, tmp_path
    ) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "--scans",
                    "1",
                    "--scans",
                    "2",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "2 scans" in result.output

    def test_scan_download_old_spelling_still_works_and_warns(
        self, runner: CliRunner, tmp_path
    ) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli,
                [
                    "scan",
                    "download",
                    "-E",
                    "XNAT_E00001",
                    "-s",
                    "1,2",
                    "--out",
                    str(tmp_path),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "2 scans" in result.output
        assert deprecation_message("-s") in result.stderr

    def test_missing_scans_on_download_is_a_usage_error(self, runner: CliRunner, tmp_path) -> None:
        ctx, mock_client = make_authenticated_context()

        with authenticated_seams(ctx, mock_client):
            result = runner.invoke(
                cli, ["scan", "download", "-E", "XNAT_E00001", "--out", str(tmp_path)]
            )

        assert result.exit_code == 2
        assert "--scans" in result.output


class TestExperimentIsRequiredOnUploads:
    @pytest.mark.parametrize("command", ["upload", "upload-exam"])
    def test_omitting_experiment_is_a_usage_error(self, command: str, tmp_path: Path) -> None:
        source = tmp_path / "dicom"
        source.mkdir()
        (source / "a.dcm").write_bytes(b"x")
        harness = make_authenticated_cli()

        result = harness.invoke(["session", command, str(source), "-P", "P", "-S", "S"])

        assert result.exit_code == 2
        assert "Missing option" in result.output


class TestBothSpellingsOnOneCommandLine:
    @staticmethod
    def _multi_cmd() -> click.Command:
        @click.command()
        @click.option("--experiment", "-E", multiple=True, is_eager=True)
        @click.option(
            "-e",
            "legacy",
            multiple=True,
            hidden=True,
            expose_value=False,
            callback=_make_forwarding_alias_cb("-e", "experiment"),
        )
        def cmd(experiment: tuple[str, ...]) -> None:
            click.echo(f"RESULT={experiment!r}")

        return cmd

    @staticmethod
    def _scalar_cmd() -> click.Command:
        @click.command()
        @click.option("--experiment", "-E", is_eager=True)
        @click.option(
            "-e",
            "legacy",
            hidden=True,
            expose_value=False,
            callback=_make_forwarding_alias_cb("-e", "experiment"),
        )
        def cmd(experiment: str | None) -> None:
            click.echo(f"RESULT={experiment!r}")

        return cmd

    @staticmethod
    def _result(cmd: click.Command, args: list[str]) -> str:
        output = CliRunner().invoke(cmd, args).output
        return next(line for line in output.splitlines() if line.startswith("RESULT="))

    @pytest.mark.parametrize(
        "args",
        [
            ["-E", "NEW1", "-e", "OLD1"],
            ["-e", "OLD1", "-E", "NEW1"],
        ],
        ids=["modern-first", "deprecated-first"],
    )
    def test_repeatable_option_merges_both_spellings(self, args: list[str]) -> None:
        assert self._result(self._multi_cmd(), args) == "RESULT=('NEW1', 'OLD1')"

    def test_repeatable_option_keeps_every_value(self) -> None:
        result = self._result(
            self._multi_cmd(), ["-E", "NEW1", "-E", "NEW2", "-e", "OLD1", "-e", "OLD2"]
        )

        assert result == "RESULT=('NEW1', 'NEW2', 'OLD1', 'OLD2')"

    def test_repeatable_option_does_not_duplicate_a_shared_value(self) -> None:
        assert self._result(self._multi_cmd(), ["-E", "DUP", "-e", "DUP"]) == "RESULT=('DUP',)"

    @pytest.mark.parametrize(
        "args",
        [["-E", "NEW1", "-e", "OLD1"], ["-e", "OLD1", "-E", "NEW1"]],
        ids=["modern-first", "deprecated-first"],
    )
    def test_single_valued_option_lets_the_alias_win_either_way(self, args: list[str]) -> None:
        assert self._result(self._scalar_cmd(), args) == "RESULT='OLD1'"

    def test_alias_alone_still_forwards(self) -> None:
        assert self._result(self._multi_cmd(), ["-e", "OLD1"]) == "RESULT=('OLD1',)"
        assert self._result(self._scalar_cmd(), ["-e", "OLD1"]) == "RESULT='OLD1'"

    def test_every_repeatable_forwarding_alias_target_is_eager(self) -> None:
        def walk(command: click.Command, path: str) -> list[tuple[str, click.Command]]:
            found = [(path, command)]
            for name, sub in getattr(command, "commands", {}).items():
                found.extend(walk(sub, f"{path} {name}"))
            return found

        offenders: list[str] = []
        checked = 0
        for path, command in walk(cli, "xnatctl"):
            targets = {
                getattr(param.callback, "_forwarding_alias_target", None)
                for param in command.params
            }
            targets.discard(None)
            for param in command.params:
                if param.name in targets and getattr(param, "multiple", False):
                    checked += 1
                    if not param.is_eager:
                        offenders.append(f"{path}: --{param.name}")

        assert offenders == []
        assert checked >= 3
