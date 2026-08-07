"""Error-path tests: expected failures must never surface a Python traceback.

Covers the no-raw-traceback contract: bad profile, first-run/no-config, unreachable
server, and the last-resort ``main()`` guard.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli, main
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import ConfigurationError, NetworkError


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config_with(profiles: dict[str, Profile], default: str = "default") -> Config:
    cfg = Config()
    cfg.profiles = profiles
    cfg.default_profile = default
    return cfg


def test_unknown_profile_no_traceback(runner: CliRunner) -> None:
    """`--profile nonexistent` renders one friendly line, no traceback."""
    cfg = _config_with({"default": Profile(url="https://xnat.example.org")})

    with patch.object(Config, "load", return_value=cfg):
        result = runner.invoke(cli, ["--profile", "nonexistent", "project", "list"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Profile" in result.output
    assert "config init" in result.output or "config show" in result.output


def test_no_config_first_run_no_traceback(runner: CliRunner) -> None:
    """First run with an empty config prints a friendly hint, not a traceback."""
    with patch.object(Config, "load", return_value=_config_with({})):
        result = runner.invoke(cli, ["project", "list"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "config init" in result.output or "config show" in result.output


def test_unreachable_server_no_traceback(runner: CliRunner) -> None:
    """A network failure during the pre-flight check is rendered, not dumped."""
    cfg = _config_with({"default": Profile(url="https://xnat.example.org")})

    class FakeClient:
        base_url = "https://xnat.example.org"
        session_token = "cached"

        @property
        def is_authenticated(self) -> bool:
            return True

        def whoami(self) -> dict[str, str]:
            raise NetworkError("https://xnat.example.org", "connection refused")

    with (
        patch.object(Config, "load", return_value=cfg),
        patch("xnatctl.cli.common.Context.get_client", return_value=cast(Any, FakeClient())),
    ):
        result = runner.invoke(cli, ["project", "list"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Network error" in result.output


def test_main_guard_catches_xnatctl_error() -> None:
    """The top-level ``main()`` guard turns a stray XNATCtlError into exit 1."""
    with (
        patch.object(Config, "load", side_effect=ConfigurationError("Failed to load config: boom")),
        patch("sys.argv", ["xnatctl", "project", "list"]),
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    assert excinfo.value.code == 1
