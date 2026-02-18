"""Tests for xnatctl CLI auth commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from xnatctl.cli.main import cli
from xnatctl.core.config import Config, Profile
from xnatctl.core.exceptions import AuthenticationError


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
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = ("testuser", "testpass")

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "login"])

        assert result.exit_code == 0
        assert "Logged in" in result.output

    def test_login_json_output(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.return_value = "fake-session-token"
        mock_client.whoami.return_value = {"username": "testuser"}
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = ("testuser", "testpass")

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
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

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "login"])

        assert result.exit_code != 0

    def test_login_prompts_for_missing_credentials(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.authenticate.return_value = "token123"
        mock_client.whoami.return_value = {"username": "prompted_user"}
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_session = MagicMock()
        mock_session.expires_at = None
        mock_auth_mgr.save_session.return_value = mock_session
        mock_auth_mgr.get_credentials.return_value = (None, None)

        cfg = Config(
            default_profile="default",
            profiles={
                "default": Profile(url="https://xnat.example.org", verify_ssl=False)
            },
        )

        with patch("xnatctl.cli.auth.Config.load", return_value=cfg):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
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

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_logout_no_session(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.load_session.return_value = None
        mock_auth_mgr.clear_session.return_value = False

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
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

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "status"])

        assert result.exit_code == 0

    def test_status_json_output(self, runner: CliRunner) -> None:
        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_info.return_value = None
        mock_auth_mgr.get_credentials.return_value = (None, None)
        mock_auth_mgr.get_token_from_env.return_value = None

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "status", "-o", "json"])

        assert result.exit_code == 0
        assert "url" in result.output


class TestAuthTest:
    """Tests for auth test command."""

    def test_auth_test_with_session(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.whoami.return_value = {
            "username": "testuser",
            "firstname": "Test",
            "lastname": "User",
            "email": "test@example.org",
        }
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = "cached-token"
        mock_auth_mgr.get_credentials.return_value = (None, None)

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
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
            profiles={
                "default": Profile(url="https://xnat.example.org", verify_ssl=False)
            },
        )

        with patch("xnatctl.cli.auth.Config.load", return_value=cfg):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                result = runner.invoke(cli, ["auth", "test"])

        assert result.exit_code != 0

    def test_auth_test_json_output(self, runner: CliRunner) -> None:
        mock_client = MagicMock()
        mock_client.whoami.return_value = {
            "username": "testuser",
            "firstname": "Test",
            "lastname": "User",
        }
        mock_client.close.return_value = None

        mock_auth_mgr = MagicMock()
        mock_auth_mgr.get_session_token.return_value = "cached-token"
        mock_auth_mgr.get_credentials.return_value = (None, None)

        with patch("xnatctl.cli.auth.Config.load", return_value=_mock_config()):
            with patch("xnatctl.cli.auth.AuthManager", return_value=mock_auth_mgr):
                with patch("xnatctl.cli.auth.XNATClient", return_value=mock_client):
                    result = runner.invoke(cli, ["auth", "test", "-o", "json"])

        assert result.exit_code == 0
        assert "authenticated" in result.output
