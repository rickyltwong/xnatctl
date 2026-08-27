"""Tests for xnatctl CLI auth commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import AuthenticationError
from xnatctl.models.info import UserInfo


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


class TestAuthLogin:
    """Tests for auth login command."""

    def test_login_success(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.return_value = "fake-session-token"
        mock_client.whoami.return_value = UserInfo(username="testuser", enabled=True)
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = ("testuser", "testpass")

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "login"])

        assert result.exit_code == 0
        assert "Logged in" in result.output

    def test_login_json_output(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.return_value = "fake-session-token"
        mock_client.whoami.return_value = UserInfo(username="testuser", enabled=True)
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = ("testuser", "testpass")

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "login", "-o", "json"])

        assert result.exit_code == 0
        assert "authenticated" in result.output

    def test_login_auth_failure(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.side_effect = AuthenticationError(
            "https://xnat.example.org", "Invalid credentials"
        )
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_credentials.return_value = ("testuser", "badpass")

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "login"])

        assert result.exit_code != 0

    def test_login_prompts_for_missing_credentials(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.return_value = "token123"
        mock_client.whoami.return_value = UserInfo(username="prompted_user", enabled=True)
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = (None, None)

        cfg = Config(
            default_profile="default",
            profiles={"default": Profile(url="https://xnat.example.org", verify_ssl=False)},
        )

        with patch("xnatctl.cli.common.Config.load", return_value=cfg):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(
                        cli,
                        ["auth", "login"],
                        input="prompted_user\nsecretpass\n",
                    )

        assert result.exit_code == 0


class TestAuthLogout:
    """Tests for auth logout command."""

    def test_logout_with_session(self, runner: CliRunner) -> None:
        mock_session = MagicMock()
        mock_session.token = "old-token"

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.load_session.return_value = mock_session
        mock_auth_mgr.clear_session.return_value = True

        mock_client = MagicMock()
        mock_client.close.return_value = None

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_logout_no_session(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.load_session.return_value = None
        mock_auth_mgr.clear_session.return_value = False

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "No cached session" in result.output


class TestAuthStatus:
    """Tests for auth status command."""

    def test_status_with_session(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_info.return_value = {
            "username": "testuser",
            "created_at": "2024-01-15T10:00:00",
            "expires_at": "2024-01-15T10:15:00",
            "is_expired": False,
        }
        mock_auth_mgr.get_credentials.return_value = ("testuser", "testpass")
        mock_auth_mgr.get_token_from_env.return_value = None

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "status"])

        assert result.exit_code == 0

    def test_status_json_output(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_info.return_value = None
        mock_auth_mgr.get_credentials.return_value = (None, None)
        mock_auth_mgr.get_token_from_env.return_value = None

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "status", "-o", "json"])

        assert result.exit_code == 0
        assert "url" in result.output

    def test_status_tsv_output(self, runner: CliRunner) -> None:
        """`-o tsv` emits a tab-separated header+row, not the Rich key-value view."""
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_info.return_value = None
        mock_auth_mgr.get_credentials.return_value = (None, None)
        mock_auth_mgr.get_token_from_env.return_value = None

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "status", "-o", "tsv"])

        assert result.exit_code == 0
        assert "\x1b" not in result.output
        assert "Auth Status" not in result.output
        lines = result.output.splitlines()
        header, row = lines[0].split("\t"), lines[1].split("\t")
        assert header == [
            "url",
            "env_username",
            "env_password",
            "env_token",
            "session_cached",
        ]
        assert row[0] == "https://xnat.example.org"
        assert row[4] == "false"


class TestAuthTest:
    """Tests for auth test command."""

    def test_auth_test_with_session(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.whoami.return_value = UserInfo(
            username="testuser",
            firstname="Test",
            lastname="User",
            email="test@example.org",
        )
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = "cached-token"
        mock_auth_mgr.get_credentials.return_value = (None, None)

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "test"])

        assert result.exit_code == 0
        assert "successful" in result.output

    def test_auth_test_no_credentials(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = None
        mock_auth_mgr.get_credentials.return_value = (None, None)

        cfg = Config(
            default_profile="default",
            profiles={"default": Profile(url="https://xnat.example.org", verify_ssl=False)},
        )

        with patch("xnatctl.cli.common.Config.load", return_value=cfg):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "test"])

        assert result.exit_code != 0

    def test_auth_test_json_output(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.whoami.return_value = UserInfo(
            username="testuser",
            firstname="Test",
            lastname="User",
        )
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = "cached-token"
        mock_auth_mgr.get_credentials.return_value = (None, None)

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "test", "-o", "json"])

        assert result.exit_code == 0
        assert "authenticated" in result.output


class TestAuthGlobalOptions:
    """auth commands carry the standard decorator stack.

    Command-local -p/-o declarations and hand-rolled print_error +
    SystemExit would mean the exceptions' actionable hints never render on
    this path, and the root-group flags would go unseen.
    """

    def test_root_level_profile_flag_is_respected(self, runner: CliRunner) -> None:
        """`xnatctl -p other auth status` -- the root-group flag reaches
        auth commands.
        """
        cfg = Config(
            default_profile="default",
            profiles={
                "default": Profile(url="https://default.example.org"),
                "other": Profile(url="https://other.example.org"),
            },
        )
        auth_mgr = MagicMock()
        auth_mgr.get_session_info.return_value = None
        auth_mgr.get_credentials.return_value = (None, None)
        auth_mgr.get_token_from_env.return_value = None

        with (
            patch("xnatctl.cli.common.Config.load", return_value=cfg),
            patch("xnatctl.cli.common.AuthManager", return_value=auth_mgr),
        ):
            result = runner.invoke(cli, ["-p", "other", "auth", "status", "-o", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output)["url"] == "https://other.example.org"

    def test_json_output_is_parseable(self, runner: CliRunner) -> None:
        cfg = Config(
            default_profile="default",
            profiles={"default": Profile(url="https://xnat.example.org")},
        )
        auth_mgr = MagicMock()
        auth_mgr.get_session_info.return_value = None
        auth_mgr.get_credentials.return_value = (None, None)
        auth_mgr.get_token_from_env.return_value = None

        with (
            patch("xnatctl.cli.common.Config.load", return_value=cfg),
            patch("xnatctl.cli.common.AuthManager", return_value=auth_mgr),
        ):
            result = runner.invoke(cli, ["auth", "status", "-o", "json"])

        payload = json.loads(result.output)
        assert payload["session_cached"] is False

    @pytest.mark.parametrize("command", ["login", "logout", "status", "test"])
    def test_verbose_and_quiet_are_accepted(self, runner: CliRunner, command: str) -> None:
        """-q and -v did not exist on these commands at all before."""
        result = runner.invoke(cli, ["auth", command, "--help"])

        assert result.exit_code == 0
        assert "--verbose" in result.output
        assert "--quiet" in result.output


class TestFirstRun:
    """The no-config cliff."""

    def _empty(self) -> Config:
        return Config(profiles={})

    @pytest.mark.parametrize("command", ["login", "status", "test"])
    def test_no_config_points_at_config_init(self, runner: CliRunner, command: str) -> None:
        with patch("xnatctl.cli.common.Config.load", return_value=self._empty()):
            result = runner.invoke(cli, ["auth", command])

        assert result.exit_code == 1
        assert "config init" in result.output
        assert "Traceback" not in result.output

    def test_no_config_does_not_blame_a_missing_default_profile(self, runner: CliRunner) -> None:
        """Saying `Profile not found: default` would send the user looking
        for a typo that does not exist.
        """
        with patch("xnatctl.cli.common.Config.load", return_value=self._empty()):
            result = runner.invoke(cli, ["auth", "login"])

        assert "Profile not found" not in result.output
        assert "No profiles configured" in result.output


class TestAuthTestJsonShapePin:
    """The exact `auth test -o json` bytes, pinned across the UserInfo migration.

    The command spreads whoami's result into its JSON payload; converting the
    typed model back to a dict at the call site must keep this byte-identical.
    """

    def test_auth_test_json_shape_is_pinned(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.whoami.return_value = UserInfo(
            username="testuser",
            firstname="Test",
            lastname="User",
            email="test@example.org",
            enabled=True,
        )
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = "cached-token"
        mock_auth_mgr.get_credentials.return_value = (None, None)

        with patch("xnatctl.cli.common.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.common.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "test", "-o", "json"])

        assert result.exit_code == 0
        expected = {
            "status": "authenticated",
            "url": "https://xnat.example.org",
            "username": "testuser",
            "firstname": "Test",
            "lastname": "User",
            "email": "test@example.org",
            "enabled": True,
        }
        # CliRunner mixes the stderr progress line into output; the JSON on
        # stdout after it is the byte-exact payload.
        assert (
            result.output
            == "Testing with cached session...\n" + json.dumps(expected, indent=2) + "\n"
        )
