from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xnatctl.core.exceptions import (
    PermissionDeniedError,
    RetryExhaustedError,
    ServerError,
    XNATCtlError,
)
from xnatctl.services.docker_admin import DockerAdminService

DAEMON_UNREACHABLE_BODY = (
    "There was an error in the request : java.io.IOException: "
    "com.sun.jna.LastErrorException: [2] No such file or directory"
)

REAL_HUB = {
    "id": 1,
    "name": "Docker Hub",
    "url": "https://index.docker.io/v1/",
    "username": None,
    "email": None,
    "status": {
        "ping": False,
        "response": "Error",
        "message": "Hub status check created exception. Check Docker server status.",
    },
    "default": True,
}


class TestImages:
    def test_images(self, fake_client) -> None:
        rows = [{"image-id": "sha256:abc", "tags": ["xnat/dcm2niix:v1.2"]}]
        fake_client.get_json.return_value = rows
        service = DockerAdminService(fake_client)

        result = service.images()

        assert result == rows
        fake_client.get_json.assert_called_once_with("/xapi/docker/images")

    def test_images_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = {}
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.images()

    def test_images_daemon_unreachable_renders_actionable_message(self, fake_client) -> None:
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.images()

        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        assert "java.io.IOException" not in message
        assert exc_info.value.hint is not None
        assert exc_info.value.details.get("body") == DAEMON_UNREACHABLE_BODY

    def test_images_retry_exhausted_also_renders_actionable_message(self, fake_client) -> None:
        underlying = ServerError(500, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY)
        fake_client.get_json.side_effect = RetryExhaustedError("request", 4, underlying)
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.images()

        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        assert "java.io.IOException" not in message
        assert exc_info.value.details.get("body") == DAEMON_UNREACHABLE_BODY

    def test_images_unrelated_retry_exhausted_is_not_swallowed(self, fake_client) -> None:
        fake_client.get_json.side_effect = RetryExhaustedError("request", 4, TimeoutError())
        service = DockerAdminService(fake_client)

        with pytest.raises(RetryExhaustedError):
            service.images()

    def test_images_unrelated_500_body_propagates_unchanged(self, fake_client) -> None:
        unrelated = ServerError(500, "GET", "/xapi/docker/images", "NullPointerException: boom")
        fake_client.get_json.side_effect = unrelated
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.images()

        assert exc_info.value is unrelated

    def test_images_non_500_server_error_propagates_unchanged(self, fake_client) -> None:
        proxy_error = ServerError(502, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY)
        fake_client.get_json.side_effect = proxy_error
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.images()

        assert exc_info.value is proxy_error


class TestHubs:
    def test_hubs(self, fake_client) -> None:
        fake_client.get_json.return_value = [REAL_HUB]
        service = DockerAdminService(fake_client)

        result = service.hubs()

        assert result == [REAL_HUB]
        fake_client.get_json.assert_called_once_with("/xapi/docker/hubs")

    def test_hubs_non_list_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = None
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.hubs()


class TestGetServer:
    def test_get_server(self, fake_client) -> None:
        server = {"host": "unix:///var/run/docker.sock", "api-version": "1.41"}
        fake_client.get_json.return_value = server
        service = DockerAdminService(fake_client)

        result = service.get_server()

        assert result == server
        fake_client.get_json.assert_called_once_with("/xapi/docker/server")

    def test_get_server_non_dict_response_raises(self, fake_client) -> None:
        fake_client.get_json.return_value = []
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.get_server()

    def test_get_server_daemon_unreachable_renders_actionable_message(self, fake_client) -> None:
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.get_server()

        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        assert "java.io.IOException" not in message

    def test_get_server_unrelated_500_body_propagates_unchanged(self, fake_client) -> None:
        unrelated = ServerError(500, "GET", "/xapi/docker/server", "NullPointerException: boom")
        fake_client.get_json.side_effect = unrelated
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.get_server()

        assert exc_info.value is unrelated


class TestPullImage:
    def test_pull_image_default_save_commands(self, fake_client) -> None:
        fake_client.post.return_value = MagicMock()
        service = DockerAdminService(fake_client)

        service.pull_image("busybox:latest")

        fake_client.post.assert_called_once_with(
            "/xapi/docker/pull", params={"image": "busybox:latest", "save-commands": "true"}
        )

    def test_pull_image_save_commands_false(self, fake_client) -> None:
        fake_client.post.return_value = MagicMock()
        service = DockerAdminService(fake_client)

        service.pull_image("busybox:latest", save_commands=False)

        fake_client.post.assert_called_once_with(
            "/xapi/docker/pull", params={"image": "busybox:latest", "save-commands": "false"}
        )

    def test_pull_image_daemon_unreachable_renders_actionable_message(self, fake_client) -> None:
        fake_client.post.side_effect = ServerError(
            500, "POST", "/xapi/docker/pull", DAEMON_UNREACHABLE_BODY
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.pull_image("busybox:latest")

        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        assert "POST /xapi/docker/pull" in message
        assert "java.io.IOException" not in message

    def test_pull_image_unrelated_500_body_propagates_unchanged(self, fake_client) -> None:
        unrelated = ServerError(500, "POST", "/xapi/docker/pull", "NullPointerException: boom")
        fake_client.post.side_effect = unrelated
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.pull_image("busybox:latest")

        assert exc_info.value is unrelated


class TestSetServer:
    def test_set_server_merges_host_into_current_config(
        self, fake_client, response_factory
    ) -> None:
        current = {"host": "unix:///old.sock", "name": "Local", "auto-cleanup": True}
        fake_client.get_json.return_value = current
        fake_client.post.return_value = response_factory(
            {"host": "tcp://new:2376", "name": "Local", "auto-cleanup": True, "ping": True}
        )
        service = DockerAdminService(fake_client)

        result = service.set_server("tcp://new:2376")

        fake_client.post.assert_called_once_with(
            "/xapi/docker/server",
            json={"host": "tcp://new:2376", "name": "Local", "auto-cleanup": True},
        )
        assert result["ping"] is True

    def test_set_server_strips_ping_before_posting(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = {"host": "unix:///old.sock", "ping": True}
        fake_client.post.return_value = response_factory({"host": "tcp://new:2376"})
        service = DockerAdminService(fake_client)

        service.set_server("tcp://new:2376")

        fake_client.post.assert_called_once_with(
            "/xapi/docker/server", json={"host": "tcp://new:2376"}
        )

    def test_set_server_falls_back_to_host_only_when_current_unreadable(
        self, fake_client, response_factory
    ) -> None:
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )
        fake_client.post.return_value = response_factory({"host": "tcp://new:2376"})
        service = DockerAdminService(fake_client)

        service.set_server("tcp://new:2376")

        fake_client.post.assert_called_once_with(
            "/xapi/docker/server", json={"host": "tcp://new:2376"}
        )

    def test_set_server_daemon_unreachable_on_post_renders_actionable_message(
        self, fake_client
    ) -> None:
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )
        fake_client.post.side_effect = ServerError(
            500, "POST", "/xapi/docker/server", DAEMON_UNREACHABLE_BODY
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError) as exc_info:
            service.set_server("tcp://new:2376")

        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        assert "POST /xapi/docker/server" in message

    def test_set_server_non_dict_response_raises(self, fake_client, response_factory) -> None:
        fake_client.get_json.return_value = {"host": "unix:///old.sock"}
        fake_client.post.return_value = response_factory([])
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.set_server("tcp://new:2376")

    def test_set_server_unrelated_read_error_aborts_without_posting(self, fake_client) -> None:
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", "some unrelated plugin-side failure"
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()

    def test_set_server_permission_error_on_read_aborts_without_posting(self, fake_client) -> None:
        fake_client.get_json.side_effect = PermissionDeniedError("docker server config")
        service = DockerAdminService(fake_client)

        with pytest.raises(PermissionDeniedError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()

    def test_set_server_unreadable_shape_on_read_aborts_without_posting(self, fake_client) -> None:
        fake_client.get_json.return_value = ["not", "a", "dict"]
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()
