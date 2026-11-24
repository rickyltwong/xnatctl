from __future__ import annotations

from unittest.mock import MagicMock

from conftest import AuthenticatedCLI

from xnatctl.core.exceptions import RetryExhaustedError, ServerError

DAEMON_UNREACHABLE_BODY = (
    "There was an error in the request : java.io.IOException: "
    "com.sun.jna.LastErrorException: [2] No such file or directory"
)


class TestAdminDockerImages:
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
        underlying = ServerError(500, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY)
        authenticated_cli.client.get_json.side_effect = RetryExhaustedError(
            "request", 4, underlying
        )

        result = authenticated_cli.invoke(["admin", "docker", "images"])

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()
        assert "java.io.IOException" not in result.output


class TestAdminDockerHubs:
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
        authenticated_cli.client.get_json.return_value = {
            "host": "unix:///var/run/docker.sock",
            "name": "Local",
            "ping": True,
        }

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code == 0
        assert "ping" in result.output.lower()
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
        authenticated_cli.client.get_json.return_value = {"host": "unix:///var/run/docker.sock"}

        result = authenticated_cli.invoke(["admin", "docker", "server"])

        assert result.exit_code == 0
        authenticated_cli.client.get_json.assert_called_once_with("/xapi/docker/server")


class TestAdminDockerServerSetHost:
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
        authenticated_cli.client.get_json.return_value = ["not", "an", "object"]

        result = authenticated_cli.invoke(
            ["admin", "docker", "server", "--set-host", "tcp://new:2376", "--dry-run"]
        )

        assert result.exit_code != 0
        authenticated_cli.client.post.assert_not_called()
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
