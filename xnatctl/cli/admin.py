"""Admin commands for xnatctl."""

from __future__ import annotations

from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    parallel_options,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_error, print_output, print_success


@click.group()
def admin() -> None:
    """Administrative operations."""
    pass


@admin.command("refresh-catalogs")
@click.argument("project")
@click.option(
    "--option",
    "-O",
    multiple=True,
    type=click.Choice(["checksum", "delete", "append", "populateStats"]),
    help="Refresh options (can repeat)",
)
@click.option("--experiment", "-e", multiple=True, help="Specific experiment IDs (can repeat)")
@click.option("--limit", type=int, help="Limit number of experiments")
@parallel_options
@global_options
@require_auth
@handle_errors
def admin_refresh_catalogs(
    ctx: Context,
    project: str,
    option: tuple,
    experiment: tuple,
    limit: int | None,
    parallel: bool,
    workers: int,
) -> None:
    """Refresh catalog XMLs for project experiments.

    Options:
    - checksum: Generate missing checksums
    - delete: Remove entries without files
    - append: Add entries for new files
    - populateStats: Update resource statistics

    Example:
        xnatctl admin refresh-catalogs MYPROJ
        xnatctl admin refresh-catalogs MYPROJ --option checksum --option delete
        xnatctl admin refresh-catalogs MYPROJ --experiment XNAT_E00001 --experiment XNAT_E00002
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from xnatctl.core.output import create_progress
    from xnatctl.core.validation import validate_project_id

    project = validate_project_id(project)
    client = ctx.get_client()
    options = list(option) if option else None
    experiment_ids = list(experiment) if experiment else None

    # Get experiments
    resp = client.get_json(
        f"/data/projects/{project}/experiments",
        params={"columns": "ID,subject_ID,label"},
    )
    results = resp.get("ResultSet", {}).get("Result", [])

    experiments = []
    for entry in results:
        exp_id = entry.get("ID", "").strip()
        subject_id = entry.get("subject_ID", "").strip()

        if exp_id and subject_id:
            experiments.append((subject_id, exp_id))

    if not experiments:
        click.echo(f"No experiments found for project {project}")
        return

    # Filter by specific IDs
    if experiment_ids:
        targets = set(experiment_ids)
        experiments = [exp for exp in experiments if exp[1] in targets]

    # Apply limit
    if limit and limit > 0:
        experiments = experiments[:limit]

    if not experiments:
        click.echo("No experiments matched selection")
        return

    # Prepare options parameter
    options_param = ",".join(options) if options else None

    refreshed = []
    failed = []

    def refresh_one(exp: tuple) -> tuple[str, bool, str]:
        """Refresh a single experiment catalog and return status."""
        subject_id, exp_id = exp
        resource_path = f"/archive/projects/{project}/subjects/{subject_id}/experiments/{exp_id}"
        params = {"resource": resource_path}
        if options_param:
            params["options"] = options_param

        try:
            resp = client.post("/data/services/refresh/catalog", params=params)
            return exp_id, resp.status_code == 200, ""
        except Exception as e:
            return exp_id, False, str(e)

    with create_progress() as progress:
        task = progress.add_task("Refreshing catalogs...", total=len(experiments))

        if parallel and len(experiments) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(experiments))) as executor:
                futures = {executor.submit(refresh_one, exp): exp for exp in experiments}
                for future in as_completed(futures):
                    exp_id, success, error = future.result()
                    if success:
                        refreshed.append(exp_id)
                    else:
                        failed.append((exp_id, error))
                    progress.advance(task)
        else:
            for exp in experiments:
                exp_id, success, error = refresh_one(exp)
                if success:
                    refreshed.append(exp_id)
                else:
                    failed.append((exp_id, error))
                progress.advance(task)

    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "project": project,
                "refreshed": refreshed,
                "failed": [{"id": eid, "error": err} for eid, err in failed],
                "count": len(refreshed),
            },
            format=OutputFormat.JSON,
        )
    else:
        if refreshed:
            print_success(f"Refreshed {len(refreshed)} experiments")
        if failed:
            print_error(f"Failed to refresh {len(failed)} experiments")
            for exp_id, error in failed[:5]:
                click.echo(f"  - {exp_id}: {error}")


@admin.group()
def user() -> None:
    """User management commands."""
    pass


@user.command("add-to-groups")
@click.argument("username")
@click.argument("groups", nargs=-1, required=True)
@click.option("--projects", help="Comma-separated project IDs to generate group names")
@click.option("--role", default="member", help="Role for project groups (default: member)")
@global_options
@require_auth
@handle_errors
def user_add_to_groups(
    ctx: Context,
    username: str,
    groups: tuple,
    projects: str | None,
    role: str,
) -> None:
    """Add user to XNAT groups.

    Groups can be specified directly or generated from project IDs.

    Example:
        xnatctl admin user add-to-groups jsmith PROJ1_member PROJ2_owner
        xnatctl admin user add-to-groups jsmith --projects PROJ1,PROJ2 --role member
    """
    from urllib.parse import quote

    client = ctx.get_client()
    group_list = list(groups)

    # Generate groups from projects if specified
    if projects:
        for proj in projects.split(","):
            proj = proj.strip()
            if proj:
                group_list.append(f"{proj}_{role}")

    if not group_list:
        print_error("No groups specified")
        raise SystemExit(1)

    # Add user to groups
    resp = client.put(
        f"/xapi/users/{quote(username)}/groups",
        json=group_list,
    )

    if resp.status_code == 200:
        print_success(f"Added {username} to {len(group_list)} groups")
        for g in group_list:
            click.echo(f"  - {g}")
    elif resp.status_code == 202:
        # Partial success
        try:
            failed = resp.json() if resp.content else group_list
        except Exception:
            failed = group_list

        added = [g for g in group_list if g not in failed]
        print_success(f"Added {username} to {len(added)}/{len(group_list)} groups")

        if added:
            click.echo("Added:")
            for g in added:
                click.echo(f"  - {g}")

        if failed:
            print_error(f"Failed to add to {len(failed)} groups:")
            for g in failed:
                click.echo(f"  - {g}")
    else:
        print_error(f"Failed to add user to groups: {resp.text}")
        raise SystemExit(1)


@admin.command("audit")
@click.option("--project", "-P", help="Filter by project")
@click.option("--user", "-u", "username", help="Filter by user")
@click.option("--action", help="Filter by action type")
@click.option("--since", help="Time range (e.g., '7d', '2024-01-01')")
@click.option("--limit", type=int, default=50, help="Max results")
@global_options
@require_auth
@handle_errors
def admin_audit(
    ctx: Context,
    project: str | None,
    username: str | None,
    action: str | None,
    since: str | None,
    limit: int,
) -> None:
    """View audit log (if available).

    Note: Audit log availability depends on XNAT server configuration.

    Example:
        xnatctl admin audit --project MYPROJ --limit 20
        xnatctl admin audit --user admin --since 7d
    """
    client = ctx.get_client()

    # Build query params
    params: dict[str, Any] = {"limit": limit}
    if project:
        params["project"] = project
    if username:
        params["user"] = username
    if action:
        params["action"] = action

    try:
        # Try the audit API endpoint
        resp = client.get_json("/xapi/audit", params=params)

        if isinstance(resp, list):
            entries = resp
        else:
            entries = resp.get("ResultSet", {}).get("Result", resp.get("items", []))

        if not entries:
            click.echo("No audit entries found")
            return

        print_output(
            entries[:limit],
            format=ctx.output_format,
            columns=["timestamp", "user", "action", "resource", "project"],
            quiet=ctx.quiet,
        )

    except Exception as e:
        # Audit API may not be available
        print_error(f"Audit log not available: {e}")
        click.echo("Note: Audit logging may not be enabled on this XNAT server")
        raise SystemExit(1) from e
