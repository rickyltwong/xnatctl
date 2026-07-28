"""Tests for root-group global options and inheritance (issue #14)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.common import Context, global_options, handle_errors
from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.output import OutputFormat

# Module-level capture dict shared by the probe subcommand. Tests reset it
# inside each test so concurrent capture from prior calls cannot leak.
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
    """Create a CLI test runner that splits stdout and stderr."""
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


class TestRootHelpAndVersion:
    """The root group still exposes --help, --version, and the four new flags."""

    def test_root_help_lists_new_global_options(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # --version + the four new global flags must appear in root help.
        assert "--version" in result.stdout
        assert "--verbose" in result.stdout
        assert "--profile" in result.stdout
        assert "--output" in result.stdout
        assert "--quiet" in result.stdout

    def test_version_not_shadowed_by_v(self, runner: CliRunner) -> None:
        """--version must still work; -v is verbose, not a version alias."""
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        # The version flag short-circuits and prints the version line.
        assert "xnatctl" in result.stdout

    def test_root_verbose_does_not_error_before_subcommand(self, runner: CliRunner) -> None:
        """`xnatctl --verbose api get --help` must exit 0 (regression #14)."""
        result = runner.invoke(cli, ["--verbose", "api", "get", "--help"])
        assert result.exit_code == 0
        assert "GET request" in result.stdout

    def test_root_short_v_accepted_before_subcommand(self, runner: CliRunner) -> None:
        """The short alias -v works on the root group."""
        result = runner.invoke(cli, ["-v", "api", "get", "--help"])
        assert result.exit_code == 0


class TestInheritance:
    """Root-group values flow into the per-subcommand decorator."""

    def setup_method(self) -> None:
        """Reset the probe capture before each test."""
        _PROBE_CAPTURE.clear()

    def test_root_verbose_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        """Root --verbose populates ctx.verbose when subcommand left it default."""
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["--verbose", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["verbose"] is True

    def test_subcommand_verbose_wins_when_root_unset(self, runner: CliRunner) -> None:
        """Existing `xnatctl <sub> -v` continues to work."""
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["__probe_global__", "-v"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["verbose"] is True

    def test_subcommand_explicit_output_wins_over_root(self, runner: CliRunner) -> None:
        """Subcommand `-o table` beats root `-o json` (explicit > inherited)."""
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["-o", "json", "__probe_global__", "-o", "table"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["output_format"] is OutputFormat.TABLE

    def test_root_output_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        """Root `-o json` flows into the subcommand when subcommand omitted -o."""
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["-o", "json", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["output_format"] is OutputFormat.JSON

    def test_root_profile_inherited_when_subcommand_default(self, runner: CliRunner) -> None:
        """`xnatctl --profile staging <sub>` populates ctx.profile_name."""
        with core_config_seam(_mock_config()):
            with config_seam(_mock_config()):
                result = runner.invoke(cli, ["--profile", "staging", "__probe_global__"])

        assert result.exit_code == 0, result.stderr
        assert _PROBE_CAPTURE["profile_name"] == "staging"
