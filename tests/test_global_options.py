from __future__ import annotations

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.common import Context, global_options, handle_errors
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
