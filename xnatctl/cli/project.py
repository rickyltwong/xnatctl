"""Project commands for xnatctl."""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    confirm_destructive,
    confirm_destructive_when,
    create_dest_client,
    dest_profile_options,
    global_options,
    handle_errors,
    list_options,
    parallel_options,
    require_auth,
    resolve_columns,
    resolve_workers_from_context,
)
from xnatctl.core.exceptions import XNATCtlError
from xnatctl.core.output import print_error, print_output, print_success
from xnatctl.core.validation import validate_project_id
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.projects import ProjectService


@click.group()
def project() -> None:
    """Manage XNAT projects."""
    pass


@project.command("list")
@list_options
@global_options
@handle_errors
@require_auth
def project_list(
    ctx: Context,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List accessible projects.

    \b
    Example:
        xnatctl project list
        xnatctl project list -o json
        xnatctl project list -q  # IDs only
        xnatctl project list --filter 'name:Study*' --sort-by name --limit 10
    """
    service = ProjectService(ctx.get_client())
    results = service.list_rows("ID,name,pi_lastname,description")

    # Transform for output
    projects = []
    for r in results:
        projects.append(
            {
                "id": r.get("ID", ""),
                "name": r.get("name", ""),
                "pi": r.get("pi_lastname", ""),
                "description": (r.get("description", "") or "")[:50],
            }
        )

    projects = apply_filter(projects, filter_expr)
    projects = apply_sort_limit(projects, sort_by, limit)

    default_columns = ["id", "name", "pi", "description"]
    print_output(
        projects,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        column_labels={"id": "ID", "name": "Name", "pi": "PI", "description": "Description"},
        quiet=ctx.quiet,
        id_field="id",
    )


@project.command("show")
@click.argument("project_id")
@global_options
@handle_errors
@require_auth
def project_show(ctx: Context, project_id: str) -> None:
    """Show project details.

    \b
    Example:
        xnatctl project show MYPROJECT
    """
    project_id = validate_project_id(project_id)
    service = ProjectService(ctx.get_client())

    # Get project details
    project_data = service.get_detail(project_id)

    if not project_data:
        print_error(f"Project not found: {project_id}")
        raise SystemExit(1)

    # Get counts
    try:
        subject_count: int | str = len(service.subject_rows(project_id))
    except (XNATCtlError, ValueError):
        subject_count = "?"

    try:
        session_count: int | str = len(service.experiment_rows(project_id))
    except (XNATCtlError, ValueError):
        session_count = "?"

    output = {
        "id": project_data.get("ID", ""),
        "name": project_data.get("name", ""),
        "secondary_id": project_data.get("secondary_ID", ""),
        "pi": project_data.get("pi_lastname", ""),
        "description": project_data.get("description", ""),
        "accessibility": project_data.get("accessibility", ""),
        "subjects": subject_count,
        "sessions": session_count,
    }

    print_output(
        output,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="id",
    )


@project.command("create")
@click.argument("project_id")
@click.option("--name", help="Project name (defaults to ID)")
@click.option("--description", help="Project description")
@click.option("--pi", help="Principal investigator last name")
@click.option(
    "--accessibility", type=click.Choice(["public", "protected", "private"]), default="private"
)
@global_options
@handle_errors
@require_auth
def project_create(
    ctx: Context,
    project_id: str,
    name: str | None,
    description: str | None,
    pi: str | None,
    accessibility: str,
) -> None:
    """Create a new project.

    \b
    Example:
        xnatctl project create NEWPROJ --name "New Project" --pi Smith
    """
    project_id = validate_project_id(project_id)
    service = ProjectService(ctx.get_client())

    resp = service.create_via_post(
        project_id,
        name=name,
        description=description,
        pi_lastname=pi,
        accessibility=accessibility,
    )

    if resp.status_code in (200, 201):
        print_success(f"Project created: {project_id}")
    else:
        print_error(f"Failed to create project: {resp.text}")
        raise SystemExit(1)


def _display_role(project: str, group_id: str) -> str:
    """Strip a project's ``{project}_`` prefix off a GROUP_ID for display.

    ``owner``/``member``/``collaborator`` reads better in a table than the
    raw XNAT group ID (``PROJ01_owner``). A group ID that doesn't carry the
    expected prefix (a server-specific group, say) is shown verbatim rather
    than mangled.
    """
    prefix = f"{project}_"
    return group_id[len(prefix) :] if group_id.startswith(prefix) else group_id


@project.command("users")
@click.argument("project")
@global_options
@handle_errors
@require_auth
def project_users(ctx: Context, project: str) -> None:
    """List a project's users and roles.

    A user with more than one role on the project appears once per role.

    \b
    Example:
        xnatctl project users MYPROJ
        xnatctl project users MYPROJ -o json
        xnatctl project users MYPROJ -q  # usernames only
    """
    project = validate_project_id(project)
    service = ProjectService(ctx.get_client())
    rows = service.list_users(project)

    output = []
    for r in rows:
        username = r.get("login") or r.get("username") or r.get("ID") or ""
        group_id = r.get("GROUP_ID") or r.get("groupname") or r.get("group") or ""
        output.append(
            {
                "username": username,
                "role": _display_role(project, group_id) if group_id else "",
                "email": r.get("email", ""),
            }
        )

    print_output(
        output,
        format=ctx.output_format,
        columns=["username", "role", "email"],
        column_labels={"username": "Username", "role": "Role", "email": "Email"},
        quiet=ctx.quiet,
        id_field="username",
    )


@project.command("grant")
@click.argument("project")
@click.argument("username")
@click.option(
    "--role",
    type=click.Choice(["owner", "member", "collaborator"]),
    required=True,
    help="Role to grant",
)
@confirm_destructive("Grant this user access to the project?")
@global_options
@handle_errors
@require_auth
def project_grant(ctx: Context, project: str, username: str, role: str, dry_run: bool) -> None:
    """Grant a user a role on a project.

    Batch mode (granting a role from a file of usernames) is not included
    here -- it depends on batch-input plumbing that has not landed yet.

    \b
    Example:
        xnatctl project grant MYPROJ jsmith --role member --yes
        xnatctl project grant MYPROJ jsmith --role owner --dry-run
    """
    project = validate_project_id(project)

    if dry_run:
        click.echo(f"[DRY-RUN] Would grant {username} the {role} role on {project}", err=True)
        return

    service = ProjectService(ctx.get_client())
    service.grant(project, username, role)
    print_success(f"Granted {username} the {role} role on {project}")


@project.command("revoke")
@click.argument("project")
@click.argument("username")
@confirm_destructive("Revoke this user's access to the project?")
@global_options
@handle_errors
@require_auth
def project_revoke(ctx: Context, project: str, username: str, dry_run: bool) -> None:
    """Revoke a user's access to a project.

    Removes the user from every group they hold on the project -- almost
    always one, but a user found in more than one is removed from all of them.

    \b
    Example:
        xnatctl project revoke MYPROJ jsmith --yes
    """
    project = validate_project_id(project)

    if dry_run:
        click.echo(f"[DRY-RUN] Would revoke {username}'s access to {project}", err=True)
        return

    service = ProjectService(ctx.get_client())
    removed = service.revoke(project, username)
    roles = ", ".join(_display_role(project, g) for g in removed)
    print_success(f"Revoked {username}'s access to {project} (removed from: {roles})")


@project.command("access")
@click.argument("project")
@click.option(
    "--set",
    "set_level",
    type=click.Choice(["public", "protected", "private"]),
    help="Set the accessibility level",
)
@confirm_destructive_when(
    lambda kw: kw.get("set_level") is not None,
    "Change this project's accessibility level?",
)
@global_options
@handle_errors
@require_auth
def project_access(ctx: Context, project: str, set_level: str | None, dry_run: bool) -> None:
    """Get or set a project's accessibility level.

    \b
    Example:
        xnatctl project access MYPROJ
        xnatctl project access MYPROJ --set protected --yes
        xnatctl project access MYPROJ --set private --dry-run
    """
    project = validate_project_id(project)
    service = ProjectService(ctx.get_client())

    if set_level is None:
        level = service.get_accessibility(project)
        print_output(
            {"project": project, "accessibility": level},
            format=ctx.output_format,
            quiet=ctx.quiet,
            id_field="project",
        )
        return

    if dry_run:
        click.echo(f"[DRY-RUN] Would set {project} accessibility to {set_level}", err=True)
        return

    service.set_accessibility(project, set_level)
    print_success(f"Set {project} accessibility to {set_level}")


@project.command("requests")
@click.argument("project")
@global_options
@handle_errors
@require_auth
def project_requests(ctx: Context, project: str) -> None:
    """List a project's access requests, pending and resolved.

    Read-only: XNAT does not offer admin-side approval or denial of a
    project access request over REST. A PAR is an invitation, and resolving
    one (accept or decline) always acts on the CURRENT SESSION USER --
    confirmed against xnat-web's ``ProjectAccessRequest.process()``, which
    calls ``setUserId(user.getID())`` and, on acceptance,
    ``Groups.addUserToGroup(_level, user, user, ...)`` for whichever account
    is authenticated when the call is made. An admin resolving someone
    else's PAR this way would add THEMSELVES to the project, not the
    intended user -- so the invited user has to log in and accept it
    themselves; there is nothing safe for this command to do on their
    behalf.

    The listing includes every request ever made for the project, not just
    pending ones -- there is no pending-only filter server-side. The
    ``approved`` column shows each row's resolution state (unresolved,
    accepted, or declined).

    \b
    Example:
        xnatctl project requests MYPROJ
        xnatctl project requests MYPROJ -o json
    """
    project = validate_project_id(project)
    service = ProjectService(ctx.get_client())

    requests = service.access_requests(project)
    print_output(
        requests,
        format=ctx.output_format,
        columns=["par_id", "login", "email", "level", "create_date", "approved"],
        column_labels={
            "par_id": "ID",
            "login": "Approver",
            "email": "Requester Email",
            "level": "Level",
            "create_date": "Requested",
            "approved": "Approved",
        },
        quiet=ctx.quiet,
        id_field="par_id",
    )


# =============================================================================
# Transfer Commands
# =============================================================================


@project.command("transfer")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@click.option("--dest-project", required=True, help="Destination project ID")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Transfer config YAML")
@dest_profile_options
@global_options
@handle_errors
@require_auth
@confirm_destructive("Transfer data to destination XNAT?")
@parallel_options
def project_transfer(
    ctx: Context,
    source_project: str,
    dest_project: str,
    config_path: str | None,
    dest_profile: str | None,
    dest_url: str | None,
    dest_user: str | None,
    dest_pass: str | None,
    dry_run: bool,
    workers: int | None,
) -> None:
    """Transfer project data to another XNAT instance.

    Incrementally syncs subjects, experiments, and resources from the source
    project to the destination, tracking state in a local SQLite database.

    \b
    Example:
        xnatctl project transfer -P SRC --dest-profile staging --dest-project DST
        xnatctl project transfer -P SRC --dest-profile staging --dest-project DST --dry-run
    """
    # Deferred as a group: TransferOrchestrator alone pulls in the whole
    # transfer subsystem (conflicts/discovery/executor/filter/scan_pipeline/
    # verifier -- several thousand lines). cli/main.py loads this module
    # unconditionally to register the `project` group, so a module-scope
    # import here would make every `project list`/`show`/`create` invocation
    # pay to parse the transfer pipeline too. CONFIG_DIR costs little on its
    # own but is grouped with it for locality (same feature, same call
    # sites -- see transfer-status/-history below).
    from xnatctl.core.config import CONFIG_DIR
    from xnatctl.core.state import TransferStateStore
    from xnatctl.services.transfer.orchestrator import TransferOrchestrator

    workers = resolve_workers_from_context(ctx, workers)

    source_client = ctx.get_client()

    dest_client = create_dest_client(
        ctx,
        dest_profile=dest_profile,
        dest_url=dest_url,
        dest_user=dest_user,
        dest_pass=dest_pass,
    )
    dest_client.authenticate()

    if config_path:
        config = TransferConfig.from_yaml(Path(config_path))
    else:
        config = TransferConfig(
            source_project=source_project,
            dest_project=dest_project,
        )

    config.source_project = source_project
    config.dest_project = dest_project

    state_store = TransferStateStore(CONFIG_DIR / "transfer.db")

    try:
        orchestrator = TransferOrchestrator(
            source_client=source_client,
            dest_client=dest_client,
            state_store=state_store,
            config=config,
        )

        def _progress(msg: str) -> None:
            if not ctx.quiet:
                click.echo(msg, err=True)

        result = orchestrator.run(dry_run=dry_run, progress_callback=_progress)

        summary = {
            "source": str(source_client.base_url),
            "destination": str(dest_client.base_url),
            "source_project": source_project,
            "dest_project": dest_project,
            "subjects_synced": result.subjects_synced,
            "subjects_failed": result.subjects_failed,
            "subjects_skipped": result.subjects_skipped,
            "experiments_synced": result.experiments_synced,
            "success": result.success,
            "dry_run": dry_run,
        }

        if result.errors:
            summary["errors"] = result.errors

        print_output(
            summary,
            format=ctx.output_format,
            quiet=ctx.quiet,
            id_field="source_project",
        )

        if not result.success:
            raise SystemExit(1)

    finally:
        state_store.close()
        dest_client.close()


@project.command("transfer-status")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@global_options
@handle_errors
@require_auth
def project_transfer_status(ctx: Context, source_project: str) -> None:
    """Show status of the last transfer run.

    \b
    Example:
        xnatctl project transfer-status -P MYPROJECT
    """
    # Deferred: see the comment on the transfer imports in project_transfer above.
    from xnatctl.core.config import CONFIG_DIR
    from xnatctl.core.state import TransferStateStore

    db_path = CONFIG_DIR / "transfer.db"
    if not db_path.exists():
        print_error("No transfer history found")
        raise SystemExit(1)

    source_client = ctx.get_client()
    store = TransferStateStore(db_path)

    try:
        history = store.get_sync_history(str(source_client.base_url), source_project)
        if not history:
            print_error(f"No transfers found for {source_project}")
            raise SystemExit(1)

        last = history[0]
        print_output(
            {
                "sync_id": last["id"],
                "status": last["status"],
                "started": last["sync_start"],
                "ended": last.get("sync_end", "in progress"),
                "subjects_synced": last["subjects_synced"],
                "subjects_failed": last["subjects_failed"],
                "subjects_skipped": last["subjects_skipped"],
                "destination": last["dest_url"],
                "dest_project": last["dest_project"],
            },
            format=ctx.output_format,
            quiet=ctx.quiet,
            id_field="sync_id",
        )
    finally:
        store.close()


@project.command("transfer-history")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@global_options
@handle_errors
@require_auth
def project_transfer_history(ctx: Context, source_project: str) -> None:
    """Show transfer history for a project.

    \b
    Example:
        xnatctl project transfer-history -P MYPROJECT
        xnatctl project transfer-history -P MYPROJECT -o json
    """
    # Deferred: see the comment on the transfer imports in project_transfer above.
    from xnatctl.core.config import CONFIG_DIR
    from xnatctl.core.state import TransferStateStore

    db_path = CONFIG_DIR / "transfer.db"
    if not db_path.exists():
        print_error("No transfer history found")
        raise SystemExit(1)

    source_client = ctx.get_client()
    store = TransferStateStore(db_path)

    try:
        history = store.get_sync_history(str(source_client.base_url), source_project)
        if not history:
            print_error(f"No transfers found for {source_project}")
            raise SystemExit(1)

        rows = []
        for h in history:
            rows.append(
                {
                    "id": h["id"],
                    "status": h["status"],
                    "started": h["sync_start"][:19],
                    "dest": h["dest_url"],
                    "synced": h["subjects_synced"],
                    "failed": h["subjects_failed"],
                }
            )

        print_output(
            rows,
            format=ctx.output_format,
            columns=["id", "status", "started", "dest", "synced", "failed"],
            column_labels={
                "id": "ID",
                "status": "Status",
                "started": "Started",
                "dest": "Destination",
                "synced": "Synced",
                "failed": "Failed",
            },
            quiet=ctx.quiet,
            id_field="id",
        )
    finally:
        store.close()


@project.command("transfer-check")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@click.option("--dest-project", required=True, help="Destination project ID")
@dest_profile_options
@global_options
@handle_errors
@require_auth
def project_transfer_check(
    ctx: Context,
    source_project: str,
    dest_project: str,
    dest_profile: str | None,
    dest_url: str | None,
    dest_user: str | None,
    dest_pass: str | None,
) -> None:
    """Pre-flight check for transfer permissions and connectivity.

    Verifies that both source and destination are reachable, authenticated,
    and that the user has sufficient permissions.

    \b
    Example:
        xnatctl project transfer-check -P SRC --dest-profile staging --dest-project DST
    """
    source_client = ctx.get_client()
    dest_client = create_dest_client(
        ctx,
        dest_profile=dest_profile,
        dest_url=dest_url,
        dest_user=dest_user,
        dest_pass=dest_pass,
    )

    checks: list[dict[str, str]] = []

    try:
        src_info = source_client.ping()
        checks.append(
            {
                "check": "Source connectivity",
                "status": "OK",
                "detail": src_info.version or "",
            }
        )
    except Exception as e:  # noqa: BLE001 -- a probe's own failure is the FAIL result
        checks.append(
            {
                "check": "Source connectivity",
                "status": "FAIL",
                "detail": str(e),
            }
        )

    try:
        src_user = source_client.whoami()
        checks.append(
            {
                "check": "Source auth",
                "status": "OK",
                "detail": src_user.username,
            }
        )
    except Exception as e:  # noqa: BLE001 -- a probe's own failure is the FAIL result
        checks.append({"check": "Source auth", "status": "FAIL", "detail": str(e)})

    try:
        dest_client.authenticate()
        dst_info = dest_client.ping()
        checks.append(
            {
                "check": "Dest connectivity",
                "status": "OK",
                "detail": dst_info.version or "",
            }
        )
    except Exception as e:  # noqa: BLE001 -- a probe's own failure is the FAIL result
        checks.append(
            {
                "check": "Dest connectivity",
                "status": "FAIL",
                "detail": str(e),
            }
        )

    try:
        dst_user = dest_client.whoami()
        checks.append(
            {
                "check": "Dest auth",
                "status": "OK",
                "detail": dst_user.username,
            }
        )
    except Exception as e:  # noqa: BLE001 -- a probe's own failure is the FAIL result
        checks.append({"check": "Dest auth", "status": "FAIL", "detail": str(e)})

    dest_client.close()

    print_output(
        checks,
        format=ctx.output_format,
        columns=["check", "status", "detail"],
        column_labels={"check": "Check", "status": "Status", "detail": "Detail"},
        quiet=ctx.quiet,
        id_field="check",
    )

    if any(c["status"] == "FAIL" for c in checks):
        raise SystemExit(1)


@project.command("transfer-init")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@click.option("--dest-project", required=True, help="Destination project ID")
@click.option("--output-file", "-f", type=click.Path(), help="Output YAML path")
@handle_errors
def project_transfer_init(
    source_project: str,
    dest_project: str,
    output_file: str | None,
) -> None:
    """Generate a starter transfer configuration YAML.

    \b
    Example:
        xnatctl project transfer-init -P SRC --dest-project DST
        xnatctl project transfer-init -P SRC --dest-project DST -f transfer.yaml
    """
    yaml_content = TransferConfig.scaffold(source_project, dest_project)

    if output_file:
        Path(output_file).write_text(yaml_content)
        print_success(f"Config written to {output_file}")
    else:
        click.echo(yaml_content)
