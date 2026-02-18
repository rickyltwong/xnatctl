"""Unit tests for AdminService."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from xnatctl.services.admin import AdminService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock XNATClient."""
    client = MagicMock()
    client.base_url = "https://xnat.example.org"
    return client


@pytest.fixture
def service(mock_client: MagicMock) -> AdminService:
    """Create AdminService with mock client."""
    return AdminService(mock_client)


def _resp(json_data: dict | list | str, content_type: str = "application/json") -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.headers = {"content-type": content_type}
    return resp


class TestRefreshCatalogs:
    """Tests for AdminService.refresh_catalogs."""

    def test_refresh_specific_experiments(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Refresh specified experiments."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.refresh_catalogs("PROJ01", experiments=["E001", "E002"], parallel=False)

        assert len(result["refreshed"]) == 2
        assert result["total"] == 2
        assert mock_client.put.call_count == 2

    def test_refresh_fetches_experiments_when_none(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """When no experiments given, fetches them from the project."""
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [{"ID": "E001"}, {"ID": "E002"}]}}
        )
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.refresh_catalogs("PROJ01", parallel=False)

        assert result["total"] == 2
        mock_client.get.assert_called_once()

    def test_refresh_with_limit(self, service: AdminService, mock_client: MagicMock) -> None:
        """Limit truncates experiment list."""
        mock_client.get.return_value = _resp(
            {"ResultSet": {"Result": [{"ID": f"E{i:03d}"} for i in range(10)]}}
        )
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.refresh_catalogs("PROJ01", limit=3, parallel=False)

        assert result["total"] == 3

    def test_refresh_with_options(self, service: AdminService, mock_client: MagicMock) -> None:
        """Options are joined and passed."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        service.refresh_catalogs(
            "PROJ01", experiments=["E001"], options=["checksum", "delete"], parallel=False
        )

        put_params = mock_client.put.call_args[1]["params"]
        assert put_params["options"] == "checksum,delete"

    def test_refresh_with_failure(self, service: AdminService, mock_client: MagicMock) -> None:
        """Failed refreshes are tracked."""
        def put_side_effect(path: str, **kwargs: object) -> MagicMock:
            if "E002" in path:
                raise RuntimeError("server error")
            return _resp("", content_type="text/plain")

        mock_client.put.side_effect = put_side_effect

        result = service.refresh_catalogs(
            "PROJ01", experiments=["E001", "E002"], parallel=False
        )

        assert "E001" in result["refreshed"]
        assert "E002" in result["failed"]
        assert len(result["errors"]) == 1

    def test_refresh_progress_callback(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Progress callback is invoked."""
        mock_client.put.return_value = _resp("", content_type="text/plain")
        callback = MagicMock()

        service.refresh_catalogs(
            "PROJ01", experiments=["E001"], parallel=False, progress_callback=callback
        )

        callback.assert_called_once_with(1, 1, "E001")


class TestAddUserToGroups:
    """Tests for AdminService.add_user_to_groups."""

    def test_add_user(self, service: AdminService, mock_client: MagicMock) -> None:
        """Add user to groups."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.add_user_to_groups("testuser", ["member"], projects=["PROJ01"])

        assert "PROJ01_member" in result["added"]
        mock_client.put.assert_called_once()

    def test_add_user_failure(self, service: AdminService, mock_client: MagicMock) -> None:
        """Failed group additions are tracked."""
        mock_client.put.side_effect = RuntimeError("forbidden")

        result = service.add_user_to_groups("testuser", ["owner"], projects=["PROJ01"])

        assert "PROJ01_owner" in result["failed"]
        assert len(result["errors"]) == 1

    def test_add_user_without_projects(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Groups without project expansion."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        result = service.add_user_to_groups("testuser", ["PROJ01_member"])

        assert "PROJ01_member" in result["added"]


class TestRemoveUserFromGroups:
    """Tests for AdminService.remove_user_from_groups."""

    def test_remove_user(self, service: AdminService, mock_client: MagicMock) -> None:
        """Remove user from groups."""
        mock_client.delete.return_value = _resp("")

        result = service.remove_user_from_groups("testuser", ["member"], projects=["PROJ01"])

        assert "PROJ01_member" in result["removed"]

    def test_remove_user_failure(self, service: AdminService, mock_client: MagicMock) -> None:
        """Failed removals are tracked."""
        mock_client.delete.side_effect = RuntimeError("not found")

        result = service.remove_user_from_groups("testuser", ["member"], projects=["PROJ01"])

        assert "PROJ01_member" in result["failed"]


class TestListUsers:
    """Tests for AdminService.list_users."""

    def test_list_all_users(self, service: AdminService, mock_client: MagicMock) -> None:
        """List all users."""
        rows = [{"login": "admin"}, {"login": "user1"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.list_users()

        assert len(result) == 2
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/users"

    def test_list_project_users(self, service: AdminService, mock_client: MagicMock) -> None:
        """List users filtered by project."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.list_users(project="PROJ01")

        call_path = mock_client.get.call_args[0][0]
        assert "/data/projects/PROJ01/users" in call_path


class TestGetUser:
    """Tests for AdminService.get_user."""

    def test_get_user_dict(self, service: AdminService, mock_client: MagicMock) -> None:
        """Get user returns dict when response is dict."""
        user = {"login": "admin", "email": "admin@example.org"}
        mock_client.get.return_value = _resp(user)

        result = service.get_user("admin")

        assert result["login"] == "admin"

    def test_get_user_from_result_set(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Get user returns full dict when response is a dict (isinstance check matches first)."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": [{"login": "admin"}]}})

        result = service.get_user("admin")

        # isinstance(data, dict) is True, so full dict is returned
        assert "ResultSet" in result

    def test_get_user_empty_list_response(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Get user returns empty dict when response is an empty list."""
        mock_client.get.return_value = _resp([])

        result = service.get_user("nobody")

        assert result == {}


class TestAuditLog:
    """Tests for AdminService.audit_log."""

    def test_audit_log_all(self, service: AdminService, mock_client: MagicMock) -> None:
        """Get all audit entries."""
        rows = [{"action": "LOGIN", "username": "admin"}]
        mock_client.get.return_value = _resp({"ResultSet": {"Result": rows}})

        result = service.audit_log()

        assert len(result) == 1
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/data/audit"

    def test_audit_log_with_filters(
        self, service: AdminService, mock_client: MagicMock
    ) -> None:
        """Audit log passes filter params."""
        mock_client.get.return_value = _resp({"ResultSet": {"Result": []}})

        service.audit_log(
            project="PROJ01", username="admin", action="LOGIN", since="7d", limit=50
        )

        params = mock_client.get.call_args[1]["params"]
        assert params["project"] == "PROJ01"
        assert params["username"] == "admin"
        assert params["action"] == "LOGIN"
        assert params["since"] == "7d"
        assert params["limit"] == 50


class TestGetServerInfo:
    """Tests for AdminService.get_server_info."""

    def test_get_server_info(self, service: AdminService, mock_client: MagicMock) -> None:
        """get_server_info returns version dict."""
        mock_client.get.return_value = _resp({"version": "1.8.5"})

        result = service.get_server_info()

        assert result["version"] == "1.8.5"


class TestGetSiteConfig:
    """Tests for AdminService.get_site_config."""

    def test_get_all_config(self, service: AdminService, mock_client: MagicMock) -> None:
        """Get all site config."""
        mock_client.get.return_value = _resp({"siteId": "XNAT"})

        result = service.get_site_config()

        assert result["siteId"] == "XNAT"
        call_path = mock_client.get.call_args[0][0]
        assert call_path == "/xapi/siteConfig"

    def test_get_specific_config(self, service: AdminService, mock_client: MagicMock) -> None:
        """Get specific config key."""
        mock_client.get.return_value = _resp({"value": "XNAT"})

        service.get_site_config(key="siteId")

        call_path = mock_client.get.call_args[0][0]
        assert "/xapi/siteConfig/siteId" in call_path


class TestSetSiteConfig:
    """Tests for AdminService.set_site_config."""

    def test_set_site_config(self, service: AdminService, mock_client: MagicMock) -> None:
        """set_site_config issues PUT with json value."""
        mock_client.put.return_value = _resp("", content_type="text/plain")

        assert service.set_site_config("siteId", "NEW_XNAT") is True
        call_path = mock_client.put.call_args[0][0]
        assert "/xapi/siteConfig/siteId" in call_path
        assert mock_client.put.call_args[1]["json"] == "NEW_XNAT"
