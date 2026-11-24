"""Tests for the flag renames that reconcile short-flag conventions.

Each rename was deprecated in 0.5.0 through the ``DEPRECATED_FLAGS``
machinery in ``xnatctl/cli/common/deprecation.py`` and the old spelling was
removed in 0.7.0, the release its warning named. Tested per flag below: the
new spelling works without a warning, and the old spelling is now rejected.

* ``-e`` -> ``-E`` for ``--experiment`` (pipeline run/jobs, admin
  refresh-catalogs) -- the outliers against the ``-E`` convention documented
  in AGENTS.md and used at 14 other sites.
* ``resource download --file`` -> ``--output-file`` -- same concept as
  ``project transfer-init --output-file``, previously spelled two ways.
  ``-f`` keeps meaning "the output-file short flag" on both commands.
* ``-f`` retires (long form only) on ``api post``/``api put`` (meant
  "read body from this file") and on ``container logs`` (meant "follow"),
  since the same letter cannot keep two different meanings.
* ``-s`` retires (long form only) on ``scan delete``/``scan download``:
  it collided with ``-S``/``--subject`` on the same command line. ``--scans``
  also becomes repeatable (``--scans 1 --scans 2``) alongside its existing
  comma-list syntax (``--scans 1,2,3``) -- purely additive, not a deprecation.
"""

from __future__ import annotations

import re
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

from xnatctl.cli.common import DEPRECATED_FLAGS, DeprecatedFlag
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
    """``-E`` on pipeline/admin commands; ``-e`` was removed in 0.7.0."""

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


class TestResourceOutputFile:
    """``--output-file`` (``-f`` unchanged); ``--file`` was removed in 0.7.0."""

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

    def test_missing_output_file_is_a_usage_error(self, runner: CliRunner) -> None:
        client = _mock_client()

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                result = runner.invoke(cli, ["resource", "download", "XNAT_E00001", "DICOM"])

        assert result.exit_code == 2
        assert "--output-file" in result.output


class TestApiFileShortFlag:
    """``--file`` on ``api post``/``api put``; ``-f`` was removed in 0.7.0."""

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


class TestContainerFollowShortFlag:
    """``--follow`` on ``container logs``; ``-f`` was removed in 0.7.0."""

    def test_follow_new_spelling(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "0.01"]
        )

        assert result.exit_code == 0
        assert "deprecated" not in result.stderr
        assert result.stdout == "hello"


class TestScanShortFlag:
    """``--scans`` (long form only); ``-s`` was removed in 0.7.0."""

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


class TestRemovedSpellings:
    """Each spelling deprecated in 0.5.0 is gone in 0.7.0, the release it named."""

    @pytest.mark.parametrize(
        ("args", "flag"),
        [
            (["pipeline", "run", "dcm2niix", "-e", "XNAT_E001"], "-e"),
            (["pipeline", "jobs", "-e", "XNAT_E001"], "-e"),
            (["admin", "refresh-catalogs", "TESTPROJ", "-e", "XNAT_E001"], "-e"),
            (["resource", "download", "XNAT_E00001", "DICOM", "--file", "out.zip"], "--file"),
            (["api", "post", "/data/foo", "-f", "body.json"], "-f"),
            (["api", "put", "/data/foo", "-f", "body.json"], "-f"),
            (["container", "logs", "501", "-f"], "-f"),
            (["scan", "delete", "-E", "XNAT_E00001", "-s", "1"], "-s"),
            (["scan", "download", "-E", "XNAT_E00001", "-s", "1"], "-s"),
        ],
        ids=[
            "pipeline-run-e",
            "pipeline-jobs-e",
            "refresh-catalogs-e",
            "resource-download-file",
            "api-post-f",
            "api-put-f",
            "container-logs-f",
            "scan-delete-s",
            "scan-download-s",
        ],
    )
    def test_the_old_spelling_is_rejected(
        self, runner: CliRunner, args: list[str], flag: str
    ) -> None:
        result = runner.invoke(cli, args)

        assert result.exit_code == 2, result.output
        # Click 8.1 prints "No such option: -e", later releases "No such option '-e'."
        assert re.search(rf"No such option(: | '){re.escape(flag)}\b", result.output), result.output


class TestRequiredValuesAreCheckedBeforeLogin:
    """The options the removed aliases used to feed are now required by Click.

    A missing or blank value is a usage error (exit 2) raised while parsing,
    so no client is built and no login happens for a command that cannot run.
    """

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["pipeline", "run", "dcm2niix"], "Missing option '--experiment'"),
            (["pipeline", "run", "dcm2niix", "-E", ""], "cannot be empty"),
            (["pipeline", "run", "dcm2niix", "-E", "   "], "cannot be empty"),
            (["scan", "download", "-E", "XNAT_E00001"], "Missing option '--scans'"),
            (["resource", "download", "XNAT_E00001", "DICOM"], "Missing option '--output-file'"),
            (["resource", "download", "XNAT_E00001", "DICOM", "-f", ""], "cannot be empty"),
        ],
        ids=[
            "pipeline-run-missing",
            "pipeline-run-blank",
            "pipeline-run-whitespace",
            "scan-download-missing",
            "resource-download-missing",
            "resource-download-blank",
        ],
    )
    def test_a_missing_or_blank_value_fails_before_login(
        self, runner: CliRunner, args: list[str], expected: str
    ) -> None:
        with patch("xnatctl.cli.common.XNATClient") as client_cls:
            result = runner.invoke(cli, args)

        assert result.exit_code == 2, result.output
        assert expected in result.output
        client_cls.assert_not_called()

    def test_a_whitespace_output_file_name_is_still_accepted(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name made of spaces is a valid POSIX filename, so it still downloads."""
        client = _mock_client()
        monkeypatch.chdir(tmp_path)

        with core_config_seam(_mock_config()), config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                with patch("xnatctl.cli.resource.stream_to_file") as mock_stream:
                    mock_stream.return_value = MagicMock(files=1, bytes=10)
                    result = runner.invoke(
                        cli, ["resource", "download", "XNAT_E00001", "DICOM", "-f", "  "]
                    )

        assert result.exit_code == 0, result.output
        assert mock_stream.call_args[0][2] == Path("  ")


@pytest.fixture
def registered_e_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a test-only ``-e`` entry so the harness below can build its alias.

    The real ``-e`` was removed in 0.7.0; these tests pin how the forwarding
    alias behaves, which the next deprecation will rely on.
    """
    monkeypatch.setitem(DEPRECATED_FLAGS, "-e", DeprecatedFlag("-E", "9.2.0", "9.0.0"))


@pytest.mark.usefixtures("registered_e_alias")
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
        """The merge above only works if the target is processed first.

        A new repeatable forwarding alias whose target is not eager would
        silently reintroduce the value-dropping bug, so assert the invariant
        across the real CLI rather than trusting each call site. No alias is
        wired today (the last ones were removed in 0.7.0), so this guards the
        next deprecation.
        """

        def walk(command: click.Command, path: str) -> list[tuple[str, click.Command]]:
            found = [(path, command)]
            for name, sub in getattr(command, "commands", {}).items():
                found.extend(walk(sub, f"{path} {name}"))
            return found

        offenders: list[str] = []
        for path, command in walk(cli, "xnatctl"):
            targets = {
                getattr(param.callback, "_forwarding_alias_target", None)
                for param in command.params
            }
            targets.discard(None)
            for param in command.params:
                if (
                    param.name in targets
                    and getattr(param, "multiple", False)
                    and not param.is_eager
                ):
                    offenders.append(f"{path}: --{param.name}")

        assert offenders == []
