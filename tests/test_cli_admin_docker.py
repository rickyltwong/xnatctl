"""Tests for xnatctl CLI `admin docker` commands.

Kept in its own file rather than appended to `tests/test_cli_admin.py`
(1173 lines, already at the file-size cap) -- and written against the
documented `authenticated_cli` fixture per `docs/adding-a-command.rst`,
rather than that file's older hand-rolled patch pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from conftest import AuthenticatedCLI

from xnatctl.core.exceptions import RetryExhaustedError, ServerError

#: Verified live: the plain-text body of a 500 from /xapi/docker/images or
#: /xapi/docker/server when no Docker daemon is reachable (XNAT 1.9.2.1 +
#: Container Service 3.7.2).
DAEMON_UNREACHABLE_BODY = (
    "There was an error in the request : java.io.IOException: "
    "com.sun.jna.LastErrorException: [2] No such file or directory"
)


class TestAdminDockerImages:
    """Tests for `admin docker images`."""

    def test_images_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [
            {"image-id": "sha256:abc", "tags": ["xnat/dcm2niix:v1.2"]}
        ]

        result = authenticated_cli.invoke(["admin", "docker", "images"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/images")

    def test_images_json_output(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [
            {"image-id": "sha256:abc", "tags": ["xnat/dcm2niix:v1.2"]}
        ]

        result = authenticated_cli.invoke(["admin", "docker", "images", "-o", "json"])

        assert result.exit_code == 0
        assert "sha256:abc" in result.output

    def test_images_no_daemon_renders_actionable_message_not_java_text(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY
        )

        result = authenticated_cli.invoke(["admin", "docker", "images"])

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output
        assert "No results" not in result.output

    def test_images_no_daemon_after_retries_exhausted_still_renders_actionable_message(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Confirmed live: GET retries a persistent 500 (idempotent), so the real
        failure mode is RetryExhaustedError wrapping the ServerError, not a bare
        ServerError -- see services/docker_admin.py's module docstring.
        """
        underlying = ServerError(500, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY)
        authenticated_cli.client.get_json.side_effect = RetryExhaustedError(
            "request", 4, underlying
        )

        result = authenticated_cli.invoke(["admin", "docker", "images"])

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output


class TestAdminDockerHubs:
    """Tests for `admin docker hubs`."""

    def test_hubs_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = [
            {
                "id": 1,
                "name": "Docker Hub",
                "url": "https://index.docker.io/v1/",
                "username": None,
                "email": None,
                "status": {"ping": False, "response": "Error", "message": "..."},
                "default": True,
            }
        ]

        result = authenticated_cli.invoke(["admin", "docker", "hubs"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/hubs")
        assert "Docker Hub" in result.output


class TestAdminDockerServer:
    """Tests for `admin docker server`."""

    def test_server_requests_expected_path(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///var/run/docker.sock",
            "api-version": "1.41",
        }

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/server")
        assert "unix:///var/run/docker.sock" in result.output

    def test_server_quiet_shows_host(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///var/run/docker.sock",
        }

        result = authenticated_cli.invoke(["admin", "docker", "server", "-q"])

        assert result.exit_code == 0
        assert result.output.strip() == "unix:///var/run/docker.sock"

    def test_server_surfaces_ping_when_present(self, authenticated_cli: AuthenticatedCLI) -> None:
        """DockerServerWithPing's `ping` field must render, not be filtered out by
        a fixed column list -- it is the one field that says whether the daemon
        is actually reachable.
        """
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///var/run/docker.sock",
            "name": "Local",
            "ping": True,
        }

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code == 0
        assert "ping" in result.output.lower()
        # The table renderer spells booleans Yes/No, as it does everywhere
        # else -- assert on what the reader actually sees, not on repr(True).
        assert "Yes" in result.output

    def test_server_no_daemon_renders_actionable_message_not_java_text(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output

    def test_server_plain_read_never_prompts(self, authenticated_cli: AuthenticatedCLI) -> None:
        """Without --set-host this is a plain GET -- no --yes needed, no audit prompt."""
        authenticated_cli.client.get_json.return_value = {"host": "unix:///var/run/docker.sock"}

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/server")


class TestAdminDockerServerSetHost:
    """Tests for `admin docker server --set-host` -- happy path, declined confirmation, dry-run."""

    def test_set_host_merges_with_current_config(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///old.sock",
            "name": "Local",
        }
        authenticated_cli.client.post.return_value = MagicMock(
            json=MagicMock(return_value={"host": "tcp://new:2376", "name": "Local", "ping": True})
        )

        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/docker/server", json={"host": "tcp://new:2376", "name": "Local"}
        )

    def test_set_host_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_set_host_dry_run_reads_current_config_no_post(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """Dry-run must run the same read-and-merge preflight execution does.

        A dry-run that returns before the GET would report "would set"
        even when the current-config read execution performs would have
        failed (unreadable or malformed configuration).
        """
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///old.sock",
            "name": "Local",
        }

        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/server")
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_set_host_dry_run_malformed_current_config_refused(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        """A malformed current configuration must refuse dry-run, not report success."""
        authenticated_cli.client.get_json.return_value = ["not", "an", "object"]

        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376", "--dry-run"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()
        # The generic "[DRY-RUN] Preview mode" banner still prints (it's
        # part of the confirmation flow, ahead of the command body); what
        # must NOT appear is the per-command success line reporting "would
        # set".
        assert "Would set" not in result.output

    def test_set_host_no_daemon_renders_actionable_message(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )
        authenticated_cli.client.post.side_effect = ServerError(
            500, "POST", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )

        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376", "--yes"]
        )

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output


class TestAdminDockerPull:
    """Tests for `admin docker pull` -- happy path, declined confirmation, dry-run."""

    def test_pull_default_save_commands(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["admin", "docker", "pull", "xnat/dcm2niix:v1.2", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/docker/pull",
            params={"image": "xnat/dcm2niix:v1.2", "save-commands": "true"},
        )

    def test_pull_no_save_commands(self, authenticated_cli: AuthenticatedCLI) -> None:
        authenticated_cli.client.post.return_value = MagicMock(status_code=200)

        result = authenticated_cli.invoke(
            ["admin", "docker", "pull", "xnat/dcm2niix:v1.2", "--no-save-commands", "--yes"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_called_once_with(
            "/xapi/docker/pull",
            params={"image": "xnat/dcm2niix:v1.2", "save-commands": "false"},
        )

    def test_pull_prompt_abort_no_mutation(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["admin", "docker", "pull", "xnat/dcm2niix:v1.2"], input="n\n"
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()

    def test_pull_dry_run_no_http_call(self, authenticated_cli: AuthenticatedCLI) -> None:
        result = authenticated_cli.invoke(
            ["admin", "docker", "pull", "xnat/dcm2niix:v1.2", "--dry-run"]
        )

        assert result.exit_code == 0
        authenticated_cli.client.post.assert_not_called()
        assert "DRY-RUN" in result.output

    def test_pull_no_daemon_renders_actionable_message_not_java_text(
        self, authenticated_cli: AuthenticatedCLI
    ) -> None:
        authenticated_cli.client.post.side_effect = ServerError(
            500, "POST", "/xapi/docker/pull", DAEMON_UNREACHABLE_BODY
        )

        result = authenticated_cli.invoke(
            ["admin", "docker", "pull", "xnat/dcm2niix:v1.2", "--yes"]
        )

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output
