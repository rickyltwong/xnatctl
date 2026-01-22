"""Admin service for XNAT administrative operations."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from .base import BaseService


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
            path = f"/data/projects/{project}/experiments"
            params = {"format": "json", "columns": "ID"}
            data = self._get(path, params=params)
            experiment_rows = self._extract_results(data)
            experiments = [str(r["ID"]) for r in experiment_rows if r.get("ID")]

        if limit:
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
                path = f"/data/experiments/{exp_id}"
                params: dict[str, Any] = {"pullDataFromHeaders": "true"}
                if option_str:
                    params["options"] = option_str

                self._put(path, params=params)
                return (exp_id, True, "")
            except Exception as e:
                return (exp_id, False, str(e))

        if parallel and total > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
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
                path = f"/data/projects/{group.split('_')[0]}/users/{role}/{username}"
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
                    path = f"/data/projects/{project}/users/{username}"
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
        if project:
            path = f"/data/projects/{project}/users"
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
        path = f"/data/users/{username}"
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

        if project:
            params["project"] = project
        if username:
            params["username"] = username
        if action:
            params["action"] = action
        if since:
            params["since"] = since

        data = self._get(path, params=params)
        return self._extract_results(data)

    def get_server_info(self) -> dict[str, Any]:
        """Get XNAT server information.

        Returns:
            Server info dict with version, build info, etc.
        """
        path = "/data/version"
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
        if key:
            path = f"/xapi/siteConfig/{key}"
        else:
            path = "/xapi/siteConfig"

        return cast(dict[str, Any], self._get(path))

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
        path = f"/xapi/siteConfig/{key}"
        self._put(path, json=value)
        return True
