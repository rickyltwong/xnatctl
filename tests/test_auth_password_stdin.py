"""Argv-password rejection for auth login and --dest-pass (SEC-05).

A password on argv is visible in ``ps``, ``/proc/*/cmdline``, and shell
history. ``auth login --password <secret>`` used to accept one, and the hidden
``--dest-pass`` on the transfer commands did the same. Both now refuse at parse
time and offer a ``--*-stdin`` flag instead, following docker login's pattern.
The canonical rejection helper started life in ``cli/xsync.py``
(``--remote-pass``) and SEC-05 moved it to ``cli/common.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import make_authenticated_cli

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile

SECRET = "hunter2-argv-secret"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config() -> Config:
    return Config(
        default_profile="default",
        profiles={"default": Profile(url="https://xnat.example.org", name="default")},
    )


class TestAuthLoginPassword:
    def test_argv_password_is_rejected_before_anything_runs(self, runner: CliRunner) -> None:
        load = MagicMock()

        with patch("xnatctl.cli.common.Config.load", load):
            result = runner.invoke(cli, ["auth", "login", "--password", SECRET])

        assert result.exit_code == 2, "UsageError exits 2 by Click convention"
        assert "Refusing to read --password from argv" in result.output
        assert "--password-stdin" in result.output
        assert SECRET not in result.output
        # is_eager: the refusal fires at parse time, before config is touched.
        load.assert_not_called()

    def test_password_stdin_authenticates(self, runner: CliRunner) -> None:
        login = MagicMock(
            return_value={
                "status": "authenticated",
                "username": "admin",
                "requested_username": "admin",
                "url": "https://xnat.example.org",
                "profile": "default",
                "expires_at": None,
            }
        )

        with (
            patch("xnatctl.cli.common.Config.load", return_value=_config()),
            patch("xnatctl.cli.auth.do_login", login),
        ):
            result = runner.invoke(
                cli,
                ["auth", "login", "-u", "admin", "--password-stdin"],
                input=f"{SECRET}\n",
            )

        assert result.exit_code == 0
        assert login.call_args.kwargs["password"] == SECRET

    def test_password_stdin_with_empty_stdin_is_a_usage_error(self, runner: CliRunner) -> None:
        with patch("xnatctl.cli.common.Config.load", return_value=_config()):
            result = runner.invoke(
                cli, ["auth", "login", "-u", "admin", "--password-stdin"], input="\n"
            )

        assert result.exit_code == 2
        assert "stdin was empty" in result.output

    def test_env_and_prompt_fallbacks_survive(self, runner: CliRunner) -> None:
        """The rejection must not break the paths that were already safe."""
        login = MagicMock(
            return_value={
                "status": "authenticated",
                "username": "admin",
                "requested_username": "admin",
                "url": "https://xnat.example.org",
                "profile": "default",
                "expires_at": None,
            }
        )

        with (
            patch("xnatctl.cli.common.Config.load", return_value=_config()),
            patch("xnatctl.cli.auth.do_login", login),
        ):
            result = runner.invoke(cli, ["auth", "login", "-u", "admin"])

        assert result.exit_code == 0
        # No stdin flag: the command passes None and do_login's own
        # env/profile/prompt chain takes over, unchanged.
        assert login.call_args.kwargs["password"] is None


class TestDestPassOption:
    def test_argv_dest_pass_is_rejected(self, runner: CliRunner) -> None:
        harness = make_authenticated_cli(default_project="PROJ")

        result = harness.invoke(
            [
                "project",
                "transfer",
                "-P",
                "SRC",
                "--dest-project",
                "DST",
                "--dest-url",
                "https://dest.example.org",
                "--dest-pass",
                SECRET,
                "--yes",
            ]
        )

        assert result.exit_code == 2
        assert "Refusing to read --dest-pass from argv" in result.output
        assert "--dest-pass-stdin" in result.output
        assert SECRET not in result.output

    def test_dest_pass_stdin_reaches_the_dest_client(self, runner: CliRunner) -> None:
        harness = make_authenticated_cli(default_project="PROJ")
        dest_client = MagicMock()

        with patch("xnatctl.cli.project.create_dest_client", return_value=dest_client) as create:
            harness.invoke(
                [
                    "project",
                    "transfer",
                    "-P",
                    "SRC",
                    "--dest-project",
                    "DST",
                    "--dest-url",
                    "https://dest.example.org",
                    "--dest-pass-stdin",
                    "--yes",
                ],
                input=f"{SECRET}\n",
            )

        assert create.call_args.kwargs["dest_pass"] == SECRET
