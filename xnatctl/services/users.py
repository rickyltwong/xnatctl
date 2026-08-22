"""User service for XNAT user-lifecycle administration (/xapi/users)."""

from __future__ import annotations

import builtins
from typing import Any

import httpx

from xnatctl.core.validation import quote_path_segment

from .base import BaseService


class UserService(BaseService):
    """Service for XNAT user administration via the ``/xapi/users`` API.

    Distinct from :class:`~xnatctl.services.admin.AdminService`, which owns the
    legacy ``/data/users`` group-membership calls (``admin user add``,
    ``put_user_groups``). This service is the ``/xapi/users`` surface: account
    lifecycle (enable/disable/verify), site-wide roles, group membership
    lookups, and killing a user's active sessions.
    """

    def list(self, active_only: bool = False) -> builtins.list[dict[str, Any]]:
        """List XNAT user accounts.

        Args:
            active_only: If True, list only users with an active session
                (``/xapi/users/active``) instead of every account.

        Returns:
            List of user dicts.

        The default listing hits ``/xapi/users/profiles``, not the bare
        ``/xapi/users`` -- confirmed against xnat-web's ``UsersApi``:
        ``/xapi/users`` returns ``List<String>`` (usernames only, no
        email/enabled/verified), which would break every column this
        listing renders. ``/xapi/users/active`` is a
        ``Map<String, Map<String, Object>>`` keyed by username (each value
        carrying ``sessions``/``count``), normalized to rows here with the
        key folded in as ``username``. Any element that is not itself a JSON
        object (a bare string, say) is wrapped rather than surfaced raw, so
        a caller can always rely on dict rows.
        """
        path = "/xapi/users/active" if active_only else "/xapi/users/profiles"
        data = self.client.get_json(path)

        if isinstance(data, list):
            return [row if isinstance(row, dict) else {"username": row} for row in data]
        if isinstance(data, dict):
            rows: builtins.list[dict[str, Any]] = []
            for key, value in data.items():
                if isinstance(value, dict):
                    row = dict(value)
                    row.setdefault("username", key)
                    rows.append(row)
                elif isinstance(value, list):
                    rows.append({"username": key, "sessions": value})
                else:
                    rows.append({"username": key, "value": value})
            return rows
        return []

    def get(self, username: str) -> dict[str, Any]:
        """Get one user's details.

        Args:
            username: XNAT username.

        Returns:
            User details dict, or an empty dict if the response was not a
            JSON object.
        """
        path = f"/xapi/users/{quote_path_segment(username)}"
        data = self.client.get_json(path)
        return data if isinstance(data, dict) else {}

    def set_enabled(self, username: str, enabled: bool) -> httpx.Response:
        """Enable or disable a user account.

        Args:
            username: XNAT username.
            enabled: True to enable the account, False to disable it.
        """
        flag = "true" if enabled else "false"
        path = f"/xapi/users/{quote_path_segment(username)}/enabled/{flag}"
        return self.client.put(path)

    def list_roles(self, username: str) -> builtins.list[str]:
        """List a user's site-wide roles.

        Args:
            username: XNAT username.

        Returns:
            List of role names.
        """
        path = f"/xapi/users/{quote_path_segment(username)}/roles"
        data = self.client.get_json(path)
        if isinstance(data, list):
            return [str(r) for r in data]
        return []

    def grant_role(self, username: str, role: str) -> httpx.Response:
        """Grant a site-wide role to a user.

        Args:
            username: XNAT username.
            role: Role name (server-defined, e.g. "Administrator").
        """
        path = f"/xapi/users/{quote_path_segment(username)}/roles/{quote_path_segment(role)}"
        return self.client.put(path)

    def revoke_role(self, username: str, role: str) -> httpx.Response:
        """Revoke a site-wide role from a user.

        Args:
            username: XNAT username.
            role: Role name to revoke.
        """
        path = f"/xapi/users/{quote_path_segment(username)}/roles/{quote_path_segment(role)}"
        return self.client.delete(path)

    def groups(self, username: str) -> builtins.list[dict[str, Any]]:
        """List the XNAT groups a user belongs to.

        Args:
            username: XNAT username.

        Returns:
            List of ``{"group": id}`` rows. ``GET /xapi/users/{u}/groups``
            returns a bare ``Set<String>`` of group IDs (confirmed against
            xnat-web's ``UsersApi.usersIdGroupsGet``), not group objects --
            each ID is wrapped so callers always get dict rows.
        """
        path = f"/xapi/users/{quote_path_segment(username)}/groups"
        data = self.client.get_json(path)
        if isinstance(data, list):
            return [g if isinstance(g, dict) else {"group": g} for g in data]
        return []

    def kill_sessions(self, username: str) -> httpx.Response:
        """Terminate a user's active XNAT sessions.

        This is the fix-command for concurrent-session exhaustion of a
        shared/service account: XNAT enforces a per-user cap on concurrent
        sessions, so a credential shared across many clients (a batch-ETL
        service account, for example) accumulates sessions from crashed or
        timed-out clients that never logged out, until every new login starts
        failing with 401s even though the password is correct. Killing the
        stale sessions frees the slots back up.

        Args:
            username: XNAT username whose active sessions should be killed.
        """
        path = f"/xapi/users/active/{quote_path_segment(username)}"
        return self.client.delete(path)
