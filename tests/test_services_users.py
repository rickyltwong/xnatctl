"""Unit tests for UserService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import make_response

from xnatctl.services.users import UserService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> UserService:
    """Create UserService with mock client."""
    return UserService(mock_client)


class TestList:
    """Tests for UserService.list.

    The default listing hits ``/xapi/users/profiles``, never the bare
    ``/xapi/users`` -- confirmed against xnat-web's ``UsersApi``:
    ``/xapi/users`` returns ``List<String>`` (bare usernames only), which
    would crash every column this listing renders (email/enabled/verified
    live only on the ``profiles`` response).
    """

    def test_list_default_hits_profiles_endpoint_not_bare_users(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = [
            {"username": "jsmith", "email": "j@example.org", "enabled": True, "verified": True}
        ]

        result = service.list()

        assert result == [
            {"username": "jsmith", "email": "j@example.org", "enabled": True, "verified": True}
        ]
        mock_client.get_json.assert_called_once_with("/xapi/users/profiles")

    def test_list_guards_bare_string_elements(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        """Regression for the raw /xapi/users shape (List[str]) reaching this method:
        a non-dict element must be wrapped, never surfaced raw (which would
        crash any caller doing ``row.get(...)``).
        """
        mock_client.get_json.return_value = ["jsmith", "adoe"]

        result = service.list()

        assert result == [{"username": "jsmith"}, {"username": "adoe"}]

    def test_list_active_hits_active_endpoint(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        mock_client.get_json.return_value = [{"username": "jsmith"}]

        result = service.list(active_only=True)

        assert result == [{"username": "jsmith"}]
        mock_client.get_json.assert_called_once_with("/xapi/users/active")

    def test_list_normalizes_dict_response(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        """/xapi/users/active is a Map<String, Map<String, Object>> keyed by username."""
        mock_client.get_json.return_value = {"jsmith": {"email": "j@example.org"}}

        result = service.list(active_only=True)

        assert result == [{"email": "j@example.org", "username": "jsmith"}]


class TestGet:
    def test_get_returns_dict(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.get_json.return_value = {"username": "jsmith", "email": "j@example.org"}

        result = service.get("jsmith")

        assert result["email"] == "j@example.org"
        mock_client.get_json.assert_called_once_with("/xapi/users/jsmith")


class TestSetEnabled:
    def test_disable_puts_false(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.set_enabled("jsmith", False)

        mock_client.put.assert_called_once_with("/xapi/users/jsmith/enabled/false")

    def test_enable_puts_true(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.set_enabled("jsmith", True)

        mock_client.put.assert_called_once_with("/xapi/users/jsmith/enabled/true")


class TestRoles:
    def test_list_roles(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.get_json.return_value = ["Administrator"]

        result = service.list_roles("jsmith")

        assert result == ["Administrator"]
        mock_client.get_json.assert_called_once_with("/xapi/users/jsmith/roles")

    def test_grant_role_puts_role_path(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.put.return_value = make_response("", content_type="text/plain")

        service.grant_role("jsmith", "Administrator")

        mock_client.put.assert_called_once_with("/xapi/users/jsmith/roles/Administrator")

    def test_revoke_role_deletes_role_path(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        mock_client.delete.return_value = make_response("", content_type="text/plain")

        service.revoke_role("jsmith", "Administrator")

        mock_client.delete.assert_called_once_with("/xapi/users/jsmith/roles/Administrator")


class TestGroups:
    def test_groups_returns_list(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.get_json.return_value = [{"ID": "PROJ_member"}]

        result = service.groups("jsmith")

        assert result == [{"ID": "PROJ_member"}]

    def test_groups_wraps_bare_strings(self, service: UserService, mock_client: MagicMock) -> None:
        mock_client.get_json.return_value = ["PROJ_member"]

        result = service.groups("jsmith")

        assert result == [{"group": "PROJ_member"}]


class TestKillSessions:
    def test_kill_sessions_deletes_active_path(
        self, service: UserService, mock_client: MagicMock
    ) -> None:
        mock_client.delete.return_value = make_response("", content_type="text/plain")

        service.kill_sessions("jsmith")

        mock_client.delete.assert_called_once_with("/xapi/users/active/jsmith")
