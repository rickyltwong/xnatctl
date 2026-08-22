"""Admin service for XNAT administrative operations."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import as_completed
from typing import Any, cast

import httpx

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import InputValidationError
from xnatctl.core.validation import quote_path_segment

from .base import BaseService


def _add_filter_param(params: dict[str, Any], key: str, value: str | None) -> None:
    """Add an optional query-string filter, rejecting an explicitly-empty one.

    ``None`` means "no filter" and is simply omitted. An empty or
    whitespace-only STRING is a different thing -- these filters go into
    query-param values (httpx encodes them safely, so there's no path/route
    injection risk the way an unquoted URL segment would carry), but
    ``value=""`` used to fall through the old truthy check the same way
    ``None`` did, silently widening an audit query to every project/user/
    action instead of failing on the caller's mistake.

    Raises:
        InputValidationError: If ``value`` is supplied but empty or
            whitespace-only.
    """
    if value is None:
        return
    if value.strip() == "":
        raise InputValidationError(
            f"{key} filter cannot be empty or whitespace-only", field=key, value=value
        )
    params[key] = value


class AdminService(BaseService):
    """Service for XNAT administrative operations."""

    def refresh_catalogs(
        self,
        project: str,
        experiments: list[str] | None = None,
        options: list[str] | None = None,
        limit: int | None = None,
        parallel: bool = True,
        workers: int = 4,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh catalog XMLs for project experiments.

        Args:
            project: Project ID
            experiments: Specific experiment IDs (or all if None)
            options: Refresh options (checksum, delete, append, populateStats)
            limit: Limit number of experiments
            parallel: Use parallel execution
            workers: Number of parallel workers
            progress_callback: Callback(current, total, experiment_id)

        Returns:
            Summary dict with refreshed, failed, errors
        """
        # Get experiments if not specified
        if not experiments:
            path = f"/data/projects/{quote_path_segment(project)}/experiments"
            params = {"format": "json", "columns": "ID"}
            data = self._get(path, params=params)
            experiment_rows = self._extract_results(data)
            experiments = [str(r["ID"]) for r in experiment_rows if r.get("ID")]

        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
            experiments = experiments[:limit]

        total = len(experiments)
        summary: dict[str, Any] = {
            "refreshed": [],
            "failed": [],
            "errors": [],
            "total": total,
        }

        # Build options string
        option_str = ",".join(options) if options else ""

        def refresh_experiment(exp_id: str) -> tuple[str, bool, str]:
            """Refresh a single experiment and return status."""
            try:
                path = f"/data/experiments/{quote_path_segment(exp_id)}"
                params: dict[str, Any] = {"pullDataFromHeaders": "true"}
                if option_str:
                    params["options"] = option_str

                self._put(path, params=params)
                return (exp_id, True, "")
            except Exception as e:
                return (exp_id, False, str(e))

        if parallel and total > 1:
            with cancellable_pool(workers) as (executor, _token):
                futures = {
                    executor.submit(refresh_experiment, exp_id): exp_id for exp_id in experiments
                }

                for i, future in enumerate(as_completed(futures)):
                    exp_id, success, error = future.result()
                    if success:
                        summary["refreshed"].append(exp_id)
                    else:
                        summary["failed"].append(exp_id)
                        summary["errors"].append({"experiment": exp_id, "error": error})

                    if progress_callback:
                        progress_callback(i + 1, total, exp_id)
        else:
            for i, exp_id in enumerate(experiments):
                exp_id, success, error = refresh_experiment(exp_id)
                if success:
                    summary["refreshed"].append(exp_id)
                else:
                    summary["failed"].append(exp_id)
                    summary["errors"].append({"experiment": exp_id, "error": error})

                if progress_callback:
                    progress_callback(i + 1, total, exp_id)

        return summary

    def add_user_to_groups(
        self,
        username: str,
        groups: list[str],
        projects: list[str] | None = None,
        role: str = "member",
    ) -> dict[str, Any]:
        """Add a user to XNAT groups.

        Args:
            username: XNAT username
            groups: Group names to add user to
            projects: Project IDs (expands group names per project)
            role: Role (owner, member, collaborator)

        Returns:
            Summary dict with added, failed, errors
        """
        results: dict[str, Any] = {
            "added": [],
            "failed": [],
            "errors": [],
        }

        # Expand groups with projects if provided
        target_groups: list[str] = []
        if projects:
            for project in projects:
                for group in groups:
                    target_groups.append(f"{project}_{group}")
        else:
            target_groups = groups

        for group in target_groups:
            try:
                path = (
                    f"/data/projects/{quote_path_segment(group.split('_')[0])}"
                    f"/users/{quote_path_segment(role)}/{quote_path_segment(username)}"
                )
                self._put(path)
                results["added"].append(group)
            except Exception as e:
                results["failed"].append(group)
                results["errors"].append({"group": group, "error": str(e)})

        return results

    def remove_user_from_groups(
        self,
        username: str,
        groups: list[str],
        projects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Remove a user from XNAT groups.

        Args:
            username: XNAT username
            groups: Group names to remove user from
            projects: Project IDs

        Returns:
            Summary dict with removed, failed, errors
        """
        results: dict[str, Any] = {
            "removed": [],
            "failed": [],
            "errors": [],
        }

        target_groups: list[str] = []
        if projects:
            for project in projects:
                for group in groups:
                    target_groups.append(f"{project}_{group}")
        else:
            target_groups = groups

        for group in target_groups:
            try:
                parts = group.split("_")
                if len(parts) >= 2:
                    project = parts[0]
                    path = (
                        f"/data/projects/{quote_path_segment(project)}"
                        f"/users/{quote_path_segment(username)}"
                    )
                    self._delete(path)
                    results["removed"].append(group)
            except Exception as e:
                results["failed"].append(group)
                results["errors"].append({"group": group, "error": str(e)})

        return results

    def list_users(
        self,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        """List users.

        Args:
            project: Filter by project

        Returns:
            List of user dicts
        """
        # `is not None`, not truthy: `project=""` is a caller mistake, not
        # "no filter" -- it must not silently widen to every user on the site.
        if project is not None:
            path = f"/data/projects/{quote_path_segment(project)}/users"
        else:
            path = "/data/users"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def get_user(
        self,
        username: str,
    ) -> dict[str, Any]:
        """Get user details.

        Args:
            username: Username

        Returns:
            User details dict
        """
        path = f"/data/users/{quote_path_segment(username)}"
        params = {"format": "json"}
        data = self._get(path, params=params)

        if isinstance(data, dict):
            return data
        results = self._extract_results(data)
        if results:
            return results[0]
        return {}

    def audit_log(
        self,
        project: str | None = None,
        username: str | None = None,
        action: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get audit log entries.

        Args:
            project: Filter by project
            username: Filter by username
            action: Filter by action type
            since: Time filter (e.g., "7d", "2024-01-01")
            limit: Maximum results

        Returns:
            List of audit log entries
        """
        path = "/data/audit"
        params: dict[str, Any] = {"format": "json", "limit": limit}

        _add_filter_param(params, "project", project)
        _add_filter_param(params, "username", username)
        _add_filter_param(params, "action", action)
        _add_filter_param(params, "since", since)

        data = self._get(path, params=params)
        return self._extract_results(data)

    def get_server_info(self) -> dict[str, Any]:
        """Get XNAT server information.

        Returns:
            Server info dict with version, build info, etc.
        """
        path = "/xapi/siteConfig/buildInfo/version"
        return cast(dict[str, Any], self._get(path))

    def get_site_config(
        self,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Get site configuration.

        Args:
            key: Specific config key (or all if None)

        Returns:
            Configuration dict
        """
        # `is not None`, not truthy: `key=""` is a caller mistake, not "get
        # everything" -- it must not silently widen to the whole site
        # config. quote_path_segment already rejects the empty string.
        if key is not None:
            path = f"/xapi/siteConfig/{quote_path_segment(key)}"
        else:
            path = "/xapi/siteConfig"

        return cast(dict[str, Any], self._get(path))

    # -------------------------------------------------------------------------
    # Raw accessors used by the CLI
    #
    # These target the specific endpoints the CLI has always called, which
    # differ from the typed methods above: ``refresh_catalogs`` PUTs each
    # experiment, whereas the CLI POSTs the catalog-refresh service; ``audit_log``
    # reads ``/data/audit``, whereas the CLI reads ``/xapi/audit``;
    # ``add_user_to_groups`` PUTs per-project group paths, whereas the CLI PUTs
    # the ``/xapi/users/{u}/groups`` list. The endpoints are not interchangeable,
    # so the CLI's wire calls live here rather than being repointed.
    # -------------------------------------------------------------------------

    def list_experiments_for_refresh(self, project: str) -> list[dict[str, Any]]:
        """Return raw ``(ID, subject_ID, label)`` experiment rows for a project."""
        resp = self.client.get_json(
            f"/data/projects/{quote_path_segment(project)}/experiments",
            params={"columns": "ID,subject_ID,label"},
        )
        rows: list[dict[str, Any]] = resp.get("ResultSet", {}).get("Result", [])
        return rows

    def refresh_catalog(self, resource: str, options: str | None = None) -> httpx.Response:
        """POST a single catalog-refresh request and return the raw response."""
        params: dict[str, str] = {"resource": resource}
        if options:
            params["options"] = options
        return self.client.post("/data/services/refresh/catalog", params=params)

    def put_user_groups(self, username: str, groups: list[str]) -> httpx.Response:
        """PUT the group list for a user and return the raw response."""
        return self.client.put(f"/xapi/users/{quote_path_segment(username)}/groups", json=groups)

    def get_xapi_audit(
        self,
        limit: int,
        project: str | None = None,
        username: str | None = None,
        action: str | None = None,
    ) -> Any:
        """GET ``/xapi/audit`` with the CLI's filters and return raw JSON."""
        params: dict[str, Any] = {"limit": limit}
        _add_filter_param(params, "project", project)
        _add_filter_param(params, "user", username)
        _add_filter_param(params, "action", action)
        return self.client.get_json("/xapi/audit", params=params)

    def set_site_config(
        self,
        key: str,
        value: Any,
    ) -> bool:
        """Set site configuration value.

        Args:
            key: Config key
            value: Config value

        Returns:
            True if successful
        """
        path = f"/xapi/siteConfig/{quote_path_segment(key)}"
        self._put(path, json=value)
        return True
