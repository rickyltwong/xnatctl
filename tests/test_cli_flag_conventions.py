"""Tests for the flag renames that reconcile short-flag conventions.

Each rename below is Breaking to a Stable surface (see ``docs/stability.rst``)
and goes through the ``DEPRECATED_FLAGS`` machinery in
``xnatctl/cli/common/deprecation.py``: the new spelling works, the old
spelling still works, and using the old spelling prints a dated warning to
stderr. Three properties, tested per flag below.

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


# =============================================================================
# -e -> -E (--experiment)
# =============================================================================


class TestExperimentShortFlag:
    """``-e`` is deprecated in favor of ``-E`` on pipeline/admin commands."""

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
                        "--no-parallel",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "Refreshed" in result.output
        assert deprecation_message("-e") in result.stderr


# =============================================================================
# resource download --file -> --output-file
# =============================================================================


class TestResourceOutputFile:
    """``--file`` is deprecated in favor of ``--output-file`` (``-f`` unchanged)."""

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
        """``-f`` is not deprecated here -- it is the short form of the new spelling."""
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


# =============================================================================
# -f retires on `api post`/`api put` (meant --file) and `container logs`
# (meant --follow) -- the same letter, two different meanings, so both lose it.
# =============================================================================


class TestApiFileShortFlag:
    """``-f`` is deprecated on ``api post``/``api put`` in favor of ``--file``."""

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
    """``-f`` is deprecated on ``container logs`` in favor of ``--follow``."""

    def test_follow_new_spelling(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER  # status: Complete
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(
            ["container", "logs", "501", "--follow", "--interval", "0.01"]
        )

        assert result.exit_code == 0
        assert "deprecated" not in result.stderr
        assert result.stdout == "hello"

    def test_short_flag_f_still_works_and_warns(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = SAMPLE_CONTAINER  # status: Complete
        authenticated_cli.client.stream.return_value = _stream_body_ctx(b"hello")

        result = authenticated_cli.invoke(["container", "logs", "501", "-f", "--interval", "0.01"])

        assert result.exit_code == 0
        assert result.stdout == "hello"  # the log body itself -- the warning is stderr-only
        assert deprecation_message("-f (container logs)") in result.stderr


# =============================================================================
# -s retires on `scan delete`/`scan download` (collided with -S/--subject on
# the same command line); --scans also becomes repeatable (additive).
# =============================================================================


class TestScanShortFlag:
    """``-s`` is deprecated in favor of ``--scans`` (long form only)."""

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
        """``--scans 1 --scans 2`` (repeatable) is equivalent to ``--scans 1,2``."""
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


class TestForwardingAliasSatisfiesRequiredOption:
    """A forwarding alias must actually satisfy the option it forwards into.

    Regression test for a bug that shipped in 0.4.0: `--session` is a
    deprecated alias for `--experiment`, documented as working until 0.5.0,
    but `session upload --session SESS` died with "Missing option
    '--experiment' / '-E'". Click enforces an option's own `required=True`
    independently of whether another option's callback already forwarded a
    value into `ctx.params`, so the alias fired, forwarded, warned -- and
    then Click rejected the invocation anyway. Exactly the pre-deprecation
    invocation the alias exists to keep working was the one that failed.
    """

    @pytest.mark.parametrize("command", ["upload", "upload-exam"])
    def test_deprecated_alias_alone_satisfies_the_primary(
        self, command: str, tmp_path: Path
    ) -> None:
        source = tmp_path / "dicom"
        source.mkdir()
        (source / "a.dcm").write_bytes(b"x")
        harness = make_authenticated_cli()

        result = harness.invoke(
            ["session", command, str(source), "-P", "P", "-S", "S", "--session", "SESS"]
        )

        assert "Missing option" not in result.output
        assert "deprecat" in result.output.lower()

    @pytest.mark.parametrize("command", ["upload", "upload-exam"])
    def test_omitting_both_spellings_still_errors(self, command: str, tmp_path: Path) -> None:
        """Relaxing Click's `required=True` must not make the option optional."""
        source = tmp_path / "dicom"
        source.mkdir()
        (source / "a.dcm").write_bytes(b"x")
        harness = make_authenticated_cli()

        result = harness.invoke(["session", command, str(source), "-P", "P", "-S", "S"])

        assert result.exit_code == 2
        assert "Missing option" in result.output


class TestBothSpellingsOnOneCommandLine:
    """Mixing a deprecated alias with its modern spelling must not lose values.

    The forwarding alias writes straight into ``ctx.params``, which bypasses
    Click's ``handle_parse_result``. That leaves the target's slot holding a
    value with no recorded parameter source, and when the target option is
    processed it finds an occupied slot it cannot out-rank -- so it discards
    its own value. ``admin refresh-catalogs -E NEW1 -E NEW2 -e OLD1`` reached
    the command as ``('OLD1',)``: both ``-E`` values silently dropped, exit 0,
    and those experiments simply never refreshed.

    Every option a forwarding alias targets is now ``is_eager=True`` so the
    target is processed first and the alias callback can merge into it.
    """

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
        """Order must not decide which spelling survives -- both do."""
        assert self._result(self._multi_cmd(), args) == "RESULT=('NEW1', 'OLD1')"

    def test_repeatable_option_keeps_every_value(self) -> None:
        """The exact `admin refresh-catalogs` case: nothing silently dropped."""
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
        """Only one value can survive, so the alias keeps overwriting -- but
        it must do so consistently, not depending on typing order.

        Making these targets eager too would let the modern spelling win, but
        eagerness reorders processing against `--help` and other eager
        callbacks, which costs more than it buys where there is nothing to
        merge. See _make_forwarding_alias_cb.
        """
        assert self._result(self._scalar_cmd(), args) == "RESULT='OLD1'"

    def test_alias_alone_still_forwards(self) -> None:
        """The whole point of the alias -- a pre-rename script -- keeps working."""
        assert self._result(self._multi_cmd(), ["-e", "OLD1"]) == "RESULT=('OLD1',)"
        assert self._result(self._scalar_cmd(), ["-e", "OLD1"]) == "RESULT='OLD1'"

    def test_every_repeatable_forwarding_alias_target_is_eager(self) -> None:
        """The merge above only works if the target is processed first.

        A new repeatable forwarding alias whose target is not eager would
        silently reintroduce the value-dropping bug, so assert the invariant
        across the real CLI rather than trusting each call site.
        """

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
        # Guard against the walk silently finding nothing and passing vacuously.
        assert checked >= 3
