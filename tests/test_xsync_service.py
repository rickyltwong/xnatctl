"""Tests for XsyncService request shapes and secret handling (issue #15)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from xnatctl.services.xsync import XsyncService

# A throwaway password used to assert that nothing under test echoes it.
SECRET: str = "hunter2-supersecret-NEVER-LOG"


def _mock_client() -> MagicMock:
    """Build a MagicMock that mimics XNATClient's get/post surface."""
    client = MagicMock()
    return client


def _mock_response(
    *, json_payload: Any = None, text: str = "", content_type: str = ""
) -> MagicMock:
    """Build a MagicMock httpx.Response stand-in."""
    resp = MagicMock()
    resp.json.return_value = json_payload
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


class TestReadEndpoints:
    """GET endpoints hit the right URL and surface the parsed JSON."""

    def test_list_projects(self) -> None:
        client = _mock_client()
        client.get.return_value = _mock_response(
            json_payload=[{"id": "PROJ_A"}, {"id": "PROJ_B"}],
            content_type="application/json",
        )
        service = XsyncService(client)

        result = service.list_projects()

        client.get.assert_called_once_with("/xapi/xsync/projects")
        assert result == [{"id": "PROJ_A"}, {"id": "PROJ_B"}]

    def test_get_setup(self) -> None:
        client = _mock_client()
        client.get.return_value = _mock_response(json_payload={"k": "v"})
        service = XsyncService(client)

        service.get_setup("PROJ")

        client.get.assert_called_once_with("/xapi/xsync/setup/projects/PROJ")

    def test_get_status(self) -> None:
        client = _mock_client()
        client.get.return_value = _mock_response(json_payload={"k": "v"})
        XsyncService(client).get_status("PROJ")

        client.get.assert_called_once_with("/xapi/xsync/status/projects/PROJ")

    def test_get_history(self) -> None:
        client = _mock_client()
        client.get.return_value = _mock_response(json_payload=[{"id": 1}])
        XsyncService(client).get_history("PROJ")

        client.get.assert_called_once_with("/xapi/xsync/history/projects/PROJ")

    def test_get_progress_returns_text(self) -> None:
        client = _mock_client()
        client.get.return_value = _mock_response(
            text="streaming log line\n", content_type="text/plain"
        )

        result = XsyncService(client).get_progress("PROJ")

        client.get.assert_called_once_with("/xapi/xsync/progress/projects/PROJ")
        assert result == "streaming log line\n"


class TestWriteEndpoints:
    """POST endpoints send the right payload + content type."""

    def test_sync(self) -> None:
        client = _mock_client()
        client.post.return_value = _mock_response(
            json_payload={"ok": True}, content_type="application/json"
        )

        XsyncService(client).sync("PROJ")

        client.post.assert_called_once_with("/xapi/xsync/projects/PROJ")

    def test_sync_subject(self) -> None:
        client = _mock_client()
        client.post.return_value = _mock_response(
            json_payload={"ok": True}, content_type="application/json"
        )

        XsyncService(client).sync_subject("XNAT_E00001")

        client.post.assert_called_once_with("/xapi/xsync/syncsubject/XNAT_E00001")

    def test_remote_rest_uses_json_body(self) -> None:
        """remoteREST is sent as application/json with the full credential payload."""
        client = _mock_client()
        client.post.return_value = _mock_response(
            json_payload={"alias": "alias-1", "secret": "secret-1", "xdatUserId": 42},
            content_type="application/json",
        )

        result = XsyncService(client).remote_rest(
            url="https://remote.example.org",
            method="POST",
            username="alice",
            password=SECRET,
        )

        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args[0] == ("/xapi/xsync/remoteREST",)
        assert call_args[1]["json"] == {
            "url": "https://remote.example.org",
            "method": "POST",
            "username": "alice",
            "password": SECRET,
        }
        # The service does NOT call data= or headers= on remoteREST; httpx
        # sets application/json from the json= kwarg automatically.
        assert "data" not in call_args[1]
        assert "headers" not in call_args[1]
        assert result["alias"] == "alias-1"

    def test_save_credentials_uses_text_plain(self) -> None:
        """credentials/save body is text/plain even though it is JSON-shaped."""
        client = _mock_client()
        client.post.return_value = _mock_response(text="saved", content_type="text/plain")

        payload = {"host": "h", "username": "alias-1", "password": "secret-1"}
        XsyncService(client).save_credentials("PROJ", payload)

        client.post.assert_called_once()
        args, kwargs = client.post.call_args
        assert args == ("/xapi/xsync/credentials/save/projects/PROJ",)
        assert kwargs["headers"] == {"Content-Type": "text/plain"}
        assert kwargs["data"] == json.dumps(payload)
        # Must NOT be sent via json= (would force application/json).
        assert "json" not in kwargs or kwargs.get("json") is None

    def test_check_credentials_uses_text_plain(self) -> None:
        """credentials/check body is text/plain even though it is JSON-shaped."""
        client = _mock_client()
        client.post.return_value = _mock_response(
            text="JSESSIONID-token", content_type="text/plain"
        )

        payload = {"host": "h", "localProject": "PROJ"}
        XsyncService(client).check_credentials("PROJ", payload)

        args, kwargs = client.post.call_args
        assert args == ("/xapi/xsync/credentials/check/projects/PROJ",)
        assert kwargs["headers"] == {"Content-Type": "text/plain"}
        assert kwargs["data"] == json.dumps(payload)


class TestRefreshCredentialsOrchestration:
    """refresh_credentials composes the three-step flow in order."""

    @pytest.fixture
    def primed_client(self) -> MagicMock:
        """Return a client whose POSTs return the canonical XSync response shapes."""
        client = _mock_client()

        def _post_side_effect(path: str, **kwargs: Any) -> MagicMock:
            if path == "/xapi/xsync/remoteREST":
                return _mock_response(
                    json_payload={
                        "alias": "ephemeral-alias",
                        "secret": "ephemeral-secret",
                        "xdatUserId": 1234,
                        "estimatedExpirationTime": 999999,
                    },
                    content_type="application/json",
                )
            return _mock_response(text="ok", content_type="text/plain")

        client.post.side_effect = _post_side_effect
        return client

    def test_issues_three_posts_in_order(self, primed_client: MagicMock) -> None:
        """remoteREST -> credentials/save -> credentials/check, in that order."""
        XsyncService(primed_client).refresh_credentials(
            project_id="PROJ",
            remote_url="https://remote.example.org",
            remote_username="alice",
            remote_password=SECRET,
            local_project="PROJ",
            remote_project="PROJ_REMOTE",
            sync_new_only=True,
        )

        calls = primed_client.post.call_args_list
        assert len(calls) == 3
        assert calls[0].args == ("/xapi/xsync/remoteREST",)
        assert calls[1].args == ("/xapi/xsync/credentials/save/projects/PROJ",)
        assert calls[2].args == ("/xapi/xsync/credentials/check/projects/PROJ",)

    def test_save_payload_contains_remote_credentials_under_text_plain(
        self, primed_client: MagicMock
    ) -> None:
        """The save POST carries the alias/secret as a JSON-shaped text/plain body."""
        XsyncService(primed_client).refresh_credentials(
            project_id="PROJ",
            remote_url="https://remote.example.org",
            remote_username="alice",
            remote_password=SECRET,
            local_project="PROJ_LOCAL",
            remote_project="PROJ_REMOTE",
            sync_new_only=False,
        )

        save_call = primed_client.post.call_args_list[1]
        assert save_call.kwargs["headers"] == {"Content-Type": "text/plain"}
        body = json.loads(save_call.kwargs["data"])
        assert body["host"] == "https://remote.example.org"
        assert body["username"] == "ephemeral-alias"
        assert body["password"] == "ephemeral-secret"
        assert body["xdatUserId"] == 1234
        assert body["localProject"] == "PROJ_LOCAL"
        assert body["remoteProject"] == "PROJ_REMOTE"
        assert body["syncNew"] is False

    def test_check_payload_text_plain(self, primed_client: MagicMock) -> None:
        """The check POST also uses text/plain and includes host + localProject."""
        XsyncService(primed_client).refresh_credentials(
            project_id="PROJ",
            remote_url="https://remote.example.org",
            remote_username="alice",
            remote_password=SECRET,
            local_project="PROJ_LOCAL",
            remote_project="PROJ_REMOTE",
        )

        check_call = primed_client.post.call_args_list[2]
        assert check_call.kwargs["headers"] == {"Content-Type": "text/plain"}
        body = json.loads(check_call.kwargs["data"])
        assert body == {"host": "https://remote.example.org", "localProject": "PROJ_LOCAL"}

    def test_return_value_omits_secret(self, primed_client: MagicMock) -> None:
        """The summary dict does not echo the remote password or the minted secret."""
        summary = XsyncService(primed_client).refresh_credentials(
            project_id="PROJ",
            remote_url="https://remote.example.org",
            remote_username="alice",
            remote_password=SECRET,
            local_project="PROJ",
            remote_project="PROJ",
        )

        flat = json.dumps(summary)
        assert SECRET not in flat
        assert "ephemeral-secret" not in flat
        assert summary["saved"] is True
        assert summary["checked"] is True

    def test_password_only_in_remote_rest_body(self, primed_client: MagicMock) -> None:
        """The remote password appears in exactly one request body (remoteREST)."""
        XsyncService(primed_client).refresh_credentials(
            project_id="PROJ",
            remote_url="https://remote.example.org",
            remote_username="alice",
            remote_password=SECRET,
            local_project="PROJ",
            remote_project="PROJ",
        )

        # Check every call's serialized body.
        bodies: list[str] = []
        for call in primed_client.post.call_args_list:
            kwargs = call.kwargs
            if kwargs.get("json") is not None:
                bodies.append(json.dumps(kwargs["json"]))
            if kwargs.get("data") is not None:
                bodies.append(str(kwargs["data"]))

        secret_hits = [body for body in bodies if SECRET in body]
        # Exactly one hit: the remoteREST JSON payload.
        assert len(secret_hits) == 1
