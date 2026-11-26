from __future__ import annotations

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.common import Context, global_options, handle_errors, require_auth
from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.output import OutputFormat

_PROBE_CAPTURE: dict[str, object] = {}


@cli.command("__probe_global__", hidden=True)
@global_options
@handle_errors
def _probe_global(ctx: Context) -> None:
    """Hidden probe used by inheritance tests to inspect Context state."""
    _PROBE_CAPTURE["profile_name"] = ctx.profile_name
    _PROBE_CAPTURE["output_format"] = ctx.output_format
    _PROBE_CAPTURE["quiet"] = ctx.quiet
    _PROBE_CAPTURE["verbose"] = ctx.verbose


@cli.group("__probe_group__", hidden=True, invoke_without_command=True)
@global_options
@handle_errors
@require_auth
def _probe_group(ctx: Context) -> None:
    """Hidden group that also runs on its own, like `session vars`."""


@_probe_group.command("sub")
@global_options
@handle_errors
def _probe_group_sub(ctx: Context) -> None:
    """Hidden subcommand of the probe group."""
    _PROBE_CAPTURE["profile_name"] = ctx.profile_name


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


class TestRootHelpAndVersion:
    def test_root_help_lists_new_global_options(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "--version" in result.stdout
        assert "--verbose" in result.stdout
        assert "--profile" in result.stdout
        assert "--output" in result.stdout
        assert "--quiet" in result.stdout

    def test_version_not_shadowed_by_v(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "xnatctl" in result.stdout

    def test_root_verbose_does_not_error_before_subcommand(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--verbose", "api", "get", "--help"])
        assert result.exit_code == 0
        assert "GET request" in result.stdout

    def test_root_short_v_accepted_before_subcommand(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["-v", "api", "get", "--help"])
        assert result.exit_code == 0


class TestInheritance:
    def setup_method(self) -> None:
        _PROBE_CAPTURE.clear()

    def test_root_verbose_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["--verbose", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["verbose"] is True

    def test_subcommand_verbose_wins_when_root_unset(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["__probe_global__", "-v"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["verbose"] is True

    def test_subcommand_explicit_output_wins_over_root(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["-o", "json", "__probe_global__", "-o", "table"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["output_format"] is OutputFormat.TABLE

    def test_root_output_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["-o", "json", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["output_format"] is OutputFormat.JSON

    def test_root_profile_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["--profile", "staging", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["profile_name"] == "staging"

    @pytest.mark.parametrize(
        ("args", "env", "expected"),
        [
            (["--profile", "staging", "__probe_global__"], {"XNAT_PROFILE": "prod"}, "staging"),
            (["__probe_global__"], {"XNAT_PROFILE": "prod"}, "prod"),
            (["-p", "prod", "__probe_global__", "-p", "staging"], {}, "staging"),
            (["__probe_group__", "-p", "staging", "sub"], {}, "staging"),
            (["__probe_group__", "-p", "staging", "sub"], {"XNAT_PROFILE": "prod"}, "staging"),
            (["-p", "prod", "__probe_group__", "sub", "-p", "staging"], {}, "staging"),
        ],
        ids=[
            "root-beats-environment",
            "environment-alone",
            "subcommand-beats-root",
            "group-reaches-subcommand",
            "group-beats-environment",
            "subcommand-beats-root-through-group",
        ],
    )
    def test_the_nearest_typed_profile_wins(
        self, runner: CliRunner, args: list[str], env: dict[str, str], expected: str
    ) -> None:
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, args, env=env)

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["profile_name"] == expected


def _run_cli_in_subprocess(tmp_path, *args: str, env: dict[str, str] | None = None):
    import os
    import subprocess
    import sys

    config_dir = tmp_path / "home" / ".config" / "xnatctl"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "version: 1\ndefault_profile: default\nbogus_key: 1\n"
        "profiles:\n  default:\n    url: http://127.0.0.1:9\n"
    )
    config_file.chmod(0o600)
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("XNAT")}
    home = str(tmp_path / "home")
    # Path.home() reads USERPROFILE on Windows, HOME elsewhere.
    child_env.update(HOME=home, USERPROFILE=home, XNAT_NO_UPDATE_CHECK="1", **(env or {}))
    return subprocess.run(
        [sys.executable, "-m", "xnatctl", *args],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


def _write_dicom_with_an_unknown_charset(path) -> None:
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.SpecificCharacterSet = "ISO_IR 999"
    ds.PatientName = "Test^Name"
    ds.Modality = "CT"
    ds.save_as(path, enforce_file_format=True)


class TestCommandsWithoutGlobalOptions:
    def test_their_log_keeps_warnings_and_drops_info(
        self, clean_root: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        import click

        monkeypatch.delenv("XNATCTL_DEBUG", raising=False)

        messages: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        @click.command("probe-no-globals")
        def probe() -> None:
            log = logging.getLogger("xnatctl.test.no_globals")
            log.info("info line")
            log.warning("warning line")

        # Python's own default is WARNING; start lower so a missing setup shows.
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().addHandler(Capture())
        cli.add_command(probe)
        try:
            result = CliRunner().invoke(cli, ["probe-no-globals"])
        finally:
            cli.commands.pop("probe-no-globals")

        assert result.exit_code == 0, result.output
        assert messages == ["warning line"]

    @pytest.mark.parametrize(
        "root_args", [[], ["--log-file", "diag.jsonl"]], ids=["plain", "log-file"]
    )
    def test_config_warnings_reach_stderr(self, tmp_path, root_args: list[str]) -> None:
        result = _run_cli_in_subprocess(tmp_path, *root_args, "config", "current-context")

        assert result.returncode == 0, result.stderr
        assert "has unrecognized config keys" in result.stderr

    @pytest.mark.parametrize(
        ("root_args", "env"),
        [([], {}), (["-v"], {}), ([], {"XNATCTL_DEBUG": "1"})],
        ids=["default", "verbose", "wire-trace"],
    )
    def test_pydicom_log_lines_are_never_shown(
        self, tmp_path, root_args: list[str], env: dict[str, str]
    ) -> None:
        dicom_file = tmp_path / "charset.dcm"
        _write_dicom_with_an_unknown_charset(dicom_file)

        result = _run_cli_in_subprocess(
            tmp_path, *root_args, "dicom", "inspect", str(dicom_file), env=env
        )

        assert result.returncode == 0, result.stderr
        assert "Unknown encoding 'ISO_IR 999'" in result.stderr
        assert "pydicom:" not in result.stderr

    def test_a_failure_prints_no_diagnostics_only_record(self, tmp_path) -> None:
        result = _run_cli_in_subprocess(tmp_path, "config", "remove-profile", "nope", "--dry-run")

        assert result.returncode == 1
        assert "Profile 'nope' not found" in result.stderr
        assert "Command exited with code" not in result.stderr
