"""Unit tests for DockerAdminService.

hubs() fixture is the real shape observed live against XNAT 1.9.2.1 +
Container Service 3.7.2. The images()/get_server() daemon-unreachable tests
use the real plain-text body XNAT returns for that 500 (see
services/docker_admin.py's module docstring).
"""

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

#: Verified live: the plain-text body of a 500 from /xapi/docker/images or
#: /xapi/docker/server when no Docker daemon is reachable.
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
    """Tests for DockerAdminService.images."""

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
        """A missing Docker daemon must not surface as the raw Java exception text."""
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
        # The raw body survives in details for --verbose, it just isn't the message.
        assert exc_info.value.details.get("body") == DAEMON_UNREACHABLE_BODY

    def test_images_retry_exhausted_also_renders_actionable_message(self, fake_client) -> None:
        """A persistent 500 is retried (GET is idempotent) and surfaces as
        RetryExhaustedError wrapping the ServerError -- verified live, see
        services/docker_admin.py's module docstring.
        """
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
        """A RetryExhaustedError NOT wrapping a ServerError (e.g. a connect-phase
        timeout) must propagate unchanged, not be misreported as a daemon issue.
        """
        fake_client.get_json.side_effect = RetryExhaustedError("request", 4, TimeoutError())
        service = DockerAdminService(fake_client)

        with pytest.raises(RetryExhaustedError):
            service.images()

    def test_images_unrelated_500_body_propagates_unchanged(self, fake_client) -> None:
        """A 500 that is NOT the daemon-unreachable signature must not be
        rewritten into a Docker-connectivity message -- it might be an
        unrelated plugin-side failure.
        """
        unrelated = ServerError(500, "GET", "/xapi/docker/images", "NullPointerException: boom")
        fake_client.get_json.side_effect = unrelated
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.images()

        assert exc_info.value is unrelated

    def test_images_non_500_server_error_propagates_unchanged(self, fake_client) -> None:
        """A 502/503/504 from a proxy is not the same failure as the daemon
        being unreachable -- even if its body happened to look similar.
        """
        proxy_error = ServerError(502, "GET", "/xapi/docker/images", DAEMON_UNREACHABLE_BODY)
        fake_client.get_json.side_effect = proxy_error
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError) as exc_info:
            service.images()

        assert exc_info.value is proxy_error


class TestHubs:
    """Tests for DockerAdminService.hubs."""

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
    """Tests for DockerAdminService.get_server."""

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
    """Tests for DockerAdminService.pull_image.

    Verified live: ``POST /xapi/docker/pull?image=...&save-commands=...``
    -- the query parameter names the ground-truth notes guessed were
    right. With no daemon reachable it answers the same 500 signature as
    images()/get_server(), this time from a POST (not idempotent, so never
    wrapped in RetryExhaustedError in practice).
    """

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
        # The message names the real HTTP method, not a hardcoded "GET" --
        # this is a POST call.
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
    """Tests for DockerAdminService.set_server.

    ``POST /xapi/docker/server`` could not be round-tripped live (no
    daemon), so this exercises the merge-with-current-config behavior
    that's the whole point of the method: preserving fields the caller
    didn't ask to change.
    """

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
        """``ping`` is response-only (DockerServerWithPing) -- not part of the
        writable DockerServer schema, so it must not be echoed back in the body.
        """
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
        """No daemon reachable -- get_server() raises -- falls back to a
        host-only body rather than propagating the read failure.
        """
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
        """A 500 that isn't the daemon-unreachable signature must abort, not fall back.

        Falling back to a host-only body here would risk silently wiping
        every other Docker setting on a server that does a full replace.
        """
        fake_client.get_json.side_effect = ServerError(
            500, "GET", "/xapi/docker/server", "some unrelated plugin-side failure"
        )
        service = DockerAdminService(fake_client)

        with pytest.raises(ServerError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()

    def test_set_server_permission_error_on_read_aborts_without_posting(self, fake_client) -> None:
        """A permission failure reading the current config must abort, not fall back."""
        fake_client.get_json.side_effect = PermissionDeniedError("docker server config")
        service = DockerAdminService(fake_client)

        with pytest.raises(PermissionDeniedError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()

    def test_set_server_unreadable_shape_on_read_aborts_without_posting(self, fake_client) -> None:
        """An unexpected 2xx shape reading the current config must abort, not fall back."""
        fake_client.get_json.return_value = ["not", "a", "dict"]
        service = DockerAdminService(fake_client)

        with pytest.raises(XNATCtlError):
            service.set_server("tcp://new:2376")

        fake_client.post.assert_not_called()
