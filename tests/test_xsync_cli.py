"""Tests for the ``xnatctl xsync`` Click command group (issue #15)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from conftest import config_seam, core_config_seam

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile

SECRET: str = "hunter2-supersecret-NEVER-LOG"


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
                default_project="PROJ",
            )
        },
    )


def _mock_client_for_refresh() -> MagicMock:
    """Build a mock XNATClient whose POSTs satisfy the three-step flow."""
    client = MagicMock()
    client.is_authenticated = True
    client.base_url = "https://xnat.example.org"
    client.whoami.return_value = {"username": "testuser"}

    def _post_side_effect(path: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        if path == "/xapi/xsync/remoteREST":
            resp.json.return_value = {
                "alias": "ephemeral-alias",
                "secret": "ephemeral-secret",
                "xdatUserId": 1234,
            }
            resp.headers = {"content-type": "application/json"}
            resp.text = ""
        else:
            resp.json.return_value = None
            resp.headers = {"content-type": "text/plain"}
            resp.text = "ok"
        return resp

    client.post.side_effect = _post_side_effect

    def _get_side_effect(path: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        if path == "/xapi/xsync/projects":
            resp.json.return_value = [
                {"id": "PROJ_A", "host": "https://remote.example.org"},
                {"id": "PROJ_B", "host": "https://remote.example.org"},
                {"id": "PROJ_C", "host": "https://other.example.org"},
            ]
        else:
            resp.json.return_value = {}
        resp.headers = {"content-type": "application/json"}
        resp.text = "{}"
        return resp

    client.get.side_effect = _get_side_effect
    return client


def _patched_invoke(
    runner: CliRunner,
    client: MagicMock,
    args: list[str],
    *,
    input_: str | None = None,
    env: dict[str, str] | None = None,
):
    """Invoke the CLI with the mock client + config patched in."""
    with core_config_seam(_mock_config()):
        with config_seam(_mock_config()):
            with patch("xnatctl.cli.common.XNATClient", return_value=client):
                return runner.invoke(cli, args, input=input_, env=env)


class TestXsyncGroupHelp:
    """`xsync --help` shape contract."""

    def test_root_help_lists_xsync(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "xsync" in result.stdout

    def test_xsync_help_lists_eight_subcommands(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["xsync", "--help"])
        assert result.exit_code == 0
        for sub in (
            "list",
            "setup",
            "status",
            "history",
            "progress",
            "sync",
            "sync-subject",
            "refresh-credentials",
        ):
            assert sub in result.stdout, f"missing subcommand {sub!r} in xsync --help"

    def test_refresh_credentials_help_lists_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["xsync", "refresh-credentials", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--remote-url",
            "--remote-user",
            "--remote-pass-stdin",
            "--local-project",
            "--remote-project",
            "--sync-new-only",
            "--all",
            "-P",
            "--yes",
            "--dry-run",
        ):
            assert flag in result.stdout, f"missing flag {flag!r} in refresh-credentials --help"


class TestRefreshCredentialsSecretSourcing:
    """Secret-sourcing priority + argv rejection (decisions.md)."""

    def test_argv_password_is_rejected(
        self,
        runner: CliRunner,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--remote-pass X` is a parse-time UsageError before auth/config.

        Mirrors the Codex B01 reproduction recipe: clean ``HOME`` with no
        ``~/.config/xnatctl/config.yaml`` and no ``XNAT_*`` env vars. The
        argv-password guard MUST fire as a Click ``UsageError`` (exit 2)
        before any decorator, config load, or HTTP client touch.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in (
            "XNAT_URL",
            "XNAT_USER",
            "XNAT_PASS",
            "XNAT_TOKEN",
            "XNAT_PROFILE",
            "XNAT_XSYNC_REMOTE_PASS",
        ):
            monkeypatch.delenv(var, raising=False)

        client = _mock_client_for_refresh()
        with patch("xnatctl.cli.common.XNATClient", return_value=client):
            result = runner.invoke(
                cli,
                [
                    "xsync",
                    "refresh-credentials",
                    "--remote-url",
                    "https://x",
                    "--remote-user",
                    "alice",
                    "--remote-pass",
                    SECRET,
                    "--yes",
                ],
            )

        # Click ``UsageError`` exits 2 by convention.
        assert result.exit_code == 2, (
            f"expected UsageError exit 2, got {result.exit_code}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        # Wording comes from the shared reject_argv_password helper in
        # cli/common.py; assert the invariants, not the
        # exact sentence.
        assert "Refusing to read --remote-pass from argv" in combined
        assert "--remote-pass-stdin" in combined
        # Critical: the secret must not appear anywhere in the captured output.
        assert SECRET not in combined
        # And no HTTP call may have been issued.
        client.post.assert_not_called()
        client.get.assert_not_called()

    def test_stdin_password_accepted_and_secret_never_in_output(self, runner: CliRunner) -> None:
        """`--remote-pass-stdin` reads stdin, runs the flow, never echoes the secret."""
        client = _mock_client_for_refresh()
        result = _patched_invoke(
            runner,
            client,
            [
                "xsync",
                "refresh-credentials",
                "-P",
                "PROJ",
                "--remote-url",
                "https://remote.example.org",
                "--remote-user",
                "alice",
                "--remote-pass-stdin",
                "--local-project",
                "PROJ",
                "--remote-project",
                "PROJ_REMOTE",
                "--yes",
            ],
            input_=SECRET + "\n",
        )

        assert result.exit_code == 0, result.stderr
        combined = result.stdout + result.stderr
        assert SECRET not in combined

        # And the three POSTs went out in the expected order.
        paths = [call.args[0] for call in client.post.call_args_list]
        assert paths == [
            "/xapi/xsync/remoteREST",
            "/xapi/xsync/credentials/save/projects/PROJ",
            "/xapi/xsync/credentials/check/projects/PROJ",
        ]

    def test_env_var_password_accepted(self, runner: CliRunner) -> None:
        """`XNAT_XSYNC_REMOTE_PASS` is consumed when --remote-pass-stdin is absent."""
        client = _mock_client_for_refresh()
        result = _patched_invoke(
            runner,
            client,
            [
                "xsync",
                "refresh-credentials",
                "-P",
                "PROJ",
                "--remote-url",
                "https://remote.example.org",
                "--remote-user",
                "alice",
                "--local-project",
                "PROJ",
                "--remote-project",
                "PROJ",
                "--yes",
            ],
            env={"XNAT_XSYNC_REMOTE_PASS": SECRET},
        )

        assert result.exit_code == 0, result.stderr
        # Secret never echoed.
        combined = result.stdout + result.stderr
        assert SECRET not in combined
        # And ended up in the remoteREST request body.
        remote_rest_call = client.post.call_args_list[0]
        assert remote_rest_call.kwargs["json"]["password"] == SECRET

    def test_all_iterates_bound_projects(self, runner: CliRunner) -> None:
        """`--all` enumerates list-projects and refreshes each matching --remote-url."""
        client = _mock_client_for_refresh()
        result = _patched_invoke(
            runner,
            client,
            [
                "xsync",
                "refresh-credentials",
                "--remote-url",
                "https://remote.example.org",
                "--remote-user",
                "alice",
                "--remote-pass-stdin",
                "--all",
                "--yes",
            ],
            input_=SECRET + "\n",
        )

        assert result.exit_code == 0, result.stderr
        # Two bound projects matched the remote URL (PROJ_A, PROJ_B). Each
        # one triggers three POSTs -> six total, plus the initial GET to
        # list projects.
        post_paths = [call.args[0] for call in client.post.call_args_list]
        assert post_paths.count("/xapi/xsync/remoteREST") == 2
        assert "/xapi/xsync/credentials/save/projects/PROJ_A" in post_paths
        assert "/xapi/xsync/credentials/save/projects/PROJ_B" in post_paths
        assert "/xapi/xsync/credentials/check/projects/PROJ_A" in post_paths
        assert "/xapi/xsync/credentials/check/projects/PROJ_B" in post_paths
        # The non-matching project must not be touched.
        assert "/xapi/xsync/credentials/save/projects/PROJ_C" not in post_paths

        combined = result.stdout + result.stderr
        assert SECRET not in combined


class TestXsyncSubcommandsRoundTrip:
    """Quick end-to-end shape tests for read/write subcommands."""

    def test_list_quiet_prints_ids(self, runner: CliRunner) -> None:
        client = _mock_client_for_refresh()
        result = _patched_invoke(runner, client, ["xsync", "list", "-q"])

        assert result.exit_code == 0, result.stderr
        assert "PROJ_A" in result.stdout
        assert "PROJ_B" in result.stdout

    def test_progress_streams_text(self, runner: CliRunner) -> None:
        client = MagicMock()
        client.is_authenticated = True
        client.base_url = "https://xnat.example.org"
        client.whoami.return_value = {"username": "testuser"}
        progress_resp = MagicMock()
        progress_resp.text = "10% done"
        progress_resp.headers = {"content-type": "text/plain"}
        client.get.return_value = progress_resp

        result = _patched_invoke(runner, client, ["xsync", "progress", "-P", "PROJ"])

        assert result.exit_code == 0, result.stderr
        assert "10% done" in result.stdout
        client.get.assert_called_with("/xapi/xsync/progress/projects/PROJ")

    def test_sync_with_yes_triggers_post(self, runner: CliRunner) -> None:
        client = MagicMock()
        client.is_authenticated = True
        client.base_url = "https://xnat.example.org"
        client.whoami.return_value = {"username": "testuser"}
        post_resp = MagicMock()
        post_resp.json.return_value = {"ok": True}
        post_resp.headers = {"content-type": "application/json"}
        client.post.return_value = post_resp

        result = _patched_invoke(runner, client, ["xsync", "sync", "-P", "PROJ", "--yes"])

        assert result.exit_code == 0, result.stderr
        client.post.assert_called_once_with("/xapi/xsync/projects/PROJ")

    def test_setup_emits_json_with_output_json(self, runner: CliRunner) -> None:
        client = MagicMock()
        client.is_authenticated = True
        client.base_url = "https://xnat.example.org"
        client.whoami.return_value = {"username": "testuser"}
        get_resp = MagicMock()
        get_resp.json.return_value = {"id": "PROJ", "configured": True}
        get_resp.headers = {"content-type": "application/json"}
        client.get.return_value = get_resp

        result = _patched_invoke(runner, client, ["xsync", "setup", "-P", "PROJ", "-o", "json"])

        assert result.exit_code == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed == {"id": "PROJ", "configured": True}
