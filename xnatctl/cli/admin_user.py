"""``admin user`` -- user account lifecycle commands (registered on the admin group)."""

from __future__ import annotations

import json
from typing import Any

import click

from xnatctl.cli.admin import admin
from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    confirm_destructive,
    confirm_destructive_when,
    global_options,
    handle_errors,
    list_options,
    reject_blank_option_value,
    require_auth,
    resolve_columns,
)
from xnatctl.core.output import (
    print_error,
    print_output,
    print_success,
)
from xnatctl.core.validation import validate_project_id
from xnatctl.services.admin import AdminService
from xnatctl.services.projects import ProjectService
from xnatctl.services.users import UserService


@admin.group()
def user() -> None:
    """User management commands."""
    pass


@user.command("add")
@click.argument("username")
@click.argument("groups", nargs=-1, required=True)
@click.option("--projects", help="Comma-separated project IDs to generate group names")
@click.option("--role", default="member", help="Role for project groups (default: member)")
@global_options
@handle_errors
@require_auth
def user_add(
    ctx: Context,
    username: str,
    groups: tuple[str, ...],
    projects: str | None,
    role: str,
) -> None:
    """Add a user to XNAT groups.

    Groups can be specified directly or generated from project IDs.

    \b
    Example:
        xnatctl admin user add jsmith PROJ1_member PROJ2_owner
        xnatctl admin user add jsmith --projects PROJ1,PROJ2 --role member
    """
    service = AdminService(ctx.get_client())
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
    resp = service.put_user_groups(username, group_list)

    if resp.status_code == 200:
        print_success(f"Added {username} to {len(group_list)} groups")
        for g in group_list:
            click.echo(f"  - {g}")
    elif resp.status_code == 202:
        # Partial success
        try:
            failed = resp.json() if resp.content else group_list
        except json.JSONDecodeError:
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


@user.command("list")
@click.option("--active", is_flag=True, help="List only users with an active session")
@list_options
@global_options
@handle_errors
@require_auth
def user_list(
    ctx: Context,
    active: bool,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List XNAT user accounts.

    \b
    Example:
        xnatctl admin user list
        xnatctl admin user list --active
        xnatctl admin user list -o json
        xnatctl admin user list -q  # usernames only
        xnatctl admin user list --filter 'enabled:true' --sort-by username
    """
    service = UserService(ctx.get_client())
    users = service.list(active_only=active)

    rows = []
    for u in users:
        rows.append(
            {
                "username": u.get("username") or u.get("login") or u.get("ID") or "",
                "email": u.get("email", ""),
                "enabled": u.get("enabled", ""),
                "verified": u.get("verified", ""),
                "last_login": (
                    u.get("lastLogin") or u.get("last_login") or u.get("lastSuccessfulLogin") or ""
                ),
            }
        )

    rows = apply_filter(rows, filter_expr)
    rows = apply_sort_limit(rows, sort_by, limit)

    default_columns = ["username", "email", "enabled", "verified", "last_login"]
    print_output(
        rows,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        column_labels={
            "username": "Username",
            "email": "Email",
            "enabled": "Enabled",
            "verified": "Verified",
            "last_login": "Last Login",
        },
        quiet=ctx.quiet,
        id_field="username",
    )


@user.command("show")
@click.argument("username")
@global_options
@handle_errors
@require_auth
def user_show(ctx: Context, username: str) -> None:
    """Show details for one XNAT user.

    \b
    Example:
        xnatctl admin user show jsmith
    """
    service = UserService(ctx.get_client())
    data = service.get(username)

    if not data:
        print_error(f"User not found: {username}")
        raise SystemExit(1)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="username")


@user.command("enable")
@click.argument("username")
@confirm_destructive("Enable this user account?")
@global_options
@handle_errors
@require_auth
def user_enable(ctx: Context, username: str, dry_run: bool) -> None:
    """Enable a user account.

    \b
    Example:
        xnatctl admin user enable jsmith --yes
    """
    if dry_run:
        click.echo(f"[DRY-RUN] Would enable user: {username}", err=True)
        return

    service = UserService(ctx.get_client())
    service.set_enabled(username, True)
    print_success(f"Enabled user: {username}")


@user.command("disable")
@click.argument("username")
@confirm_destructive("Disable this user account?")
@global_options
@handle_errors
@require_auth
def user_disable(ctx: Context, username: str, dry_run: bool) -> None:
    """Disable a user account.

    \b
    Example:
        xnatctl admin user disable jsmith --yes
        xnatctl admin user disable jsmith --dry-run
    """
    if dry_run:
        click.echo(f"[DRY-RUN] Would disable user: {username}", err=True)
        return

    service = UserService(ctx.get_client())
    service.set_enabled(username, False)
    print_success(f"Disabled user: {username}")


def _user_roles_mutating(kwargs: dict[str, Any]) -> bool:
    """`confirm_destructive_when` predicate for `admin user roles`.

    Raises the mutual-exclusion error here, ahead of the confirmation
    prompt/audit write, rather than in the command body where it would only
    run after the (unwarranted, for an invalid combination) prompt.
    """
    grant_role = kwargs.get("grant_role")
    revoke_role = kwargs.get("revoke_role")
    if grant_role and revoke_role:
        raise click.UsageError("--grant and --revoke are mutually exclusive")
    return bool(grant_role or revoke_role)


@user.command("roles")
@click.argument("username")
@click.option(
    "--grant", "grant_role", callback=reject_blank_option_value, help="Grant a role to the user"
)
@click.option(
    "--revoke",
    "revoke_role",
    callback=reject_blank_option_value,
    help="Revoke a role from the user",
)
@confirm_destructive_when(_user_roles_mutating, "Modify this user's roles?")
@global_options
@handle_errors
@require_auth
def user_roles(
    ctx: Context,
    username: str,
    grant_role: str | None,
    revoke_role: str | None,
    dry_run: bool,
) -> None:
    """List, grant, or revoke a user's site-wide roles.

    \b
    Example:
        xnatctl admin user roles jsmith
        xnatctl admin user roles jsmith --grant Administrator --yes
        xnatctl admin user roles jsmith --revoke Administrator --dry-run
    """
    service = UserService(ctx.get_client())

    if not grant_role and not revoke_role:
        roles = service.list_roles(username)
        print_output(
            [{"role": r} for r in roles],
            format=ctx.output_format,
            columns=["role"],
            quiet=ctx.quiet,
            id_field="role",
        )
        return

    action = "grant" if grant_role else "revoke"
    role = grant_role or revoke_role

    if dry_run:
        click.echo(f"[DRY-RUN] Would {action} role '{role}' for {username}", err=True)
        return

    if grant_role:
        service.grant_role(username, grant_role)
        print_success(f"Granted role '{grant_role}' to {username}")
    else:
        assert revoke_role is not None
        service.revoke_role(username, revoke_role)
        print_success(f"Revoked role '{revoke_role}' from {username}")


@user.command("remove")
@click.argument("username")
@click.option("--project", "-P", required=True, help="Project ID to remove the user from")
@confirm_destructive("Remove this user from the project's groups?")
@global_options
@handle_errors
@require_auth
def user_remove(ctx: Context, username: str, project: str, dry_run: bool) -> None:
    """Remove a user from a project's groups.

    Removes the user from every group they hold on the project (the same
    resolution ``project revoke`` uses) -- almost always one, but a user
    found in more than one is removed from all of them.

    \b
    Example:
        xnatctl admin user remove jsmith --project MYPROJ --yes
    """
    project = validate_project_id(project)

    if dry_run:
        click.echo(f"[DRY-RUN] Would remove {username} from project {project}", err=True)
        return

    service = ProjectService(ctx.get_client())
    removed = service.revoke(project, username)
    print_success(f"Removed {username} from project {project} (groups: {', '.join(removed)})")


@user.command("kill-sessions")
@click.argument("username")
@confirm_destructive("Terminate this user's active sessions?")
@global_options
@handle_errors
@require_auth
def user_kill_sessions(ctx: Context, username: str, dry_run: bool) -> None:
    """Terminate a user's active XNAT sessions.

    Use this when a shared/service account has exhausted its concurrent-
    session limit and every new login is failing with 401s -- clearing the
    stale sessions frees up slots for real logins.

    \b
    Example:
        xnatctl admin user kill-sessions svc_ingest --yes
    """
    if dry_run:
        click.echo(f"[DRY-RUN] Would terminate sessions for: {username}", err=True)
        return

    service = UserService(ctx.get_client())
    service.kill_sessions(username)
    print_success(f"Terminated active sessions for: {username}")


@user.command("groups")
@click.argument("username")
@global_options
@handle_errors
@require_auth
def user_groups(ctx: Context, username: str) -> None:
    """List the XNAT groups a user belongs to.

    \b
    Example:
        xnatctl admin user groups jsmith
        xnatctl admin user groups jsmith -o json
    """
    service = UserService(ctx.get_client())
    groups = service.groups(username)

    print_output(
        groups,
        format=ctx.output_format,
        columns=["group"],
        column_labels={"group": "Group"},
        quiet=ctx.quiet,
        id_field="group",
    )
