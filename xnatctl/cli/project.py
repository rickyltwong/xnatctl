"""Project commands for xnatctl."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    create_dest_client,
    dest_profile_options,
    global_options,
    handle_errors,
    parallel_options,
    require_auth,
)
from xnatctl.core.output import print_error, print_output, print_success
from xnatctl.models.hierarchy import ProjectRef
from xnatctl.services.hierarchy import HierarchyService


@click.group()
def project() -> None:
    """Manage XNAT projects."""
    pass


@project.command("list")
@global_options
@require_auth
@handle_errors
def project_list(ctx: Context) -> None:
    """List accessible projects.

    Example:
        xnatctl project list
        xnatctl project list -o json
        xnatctl project list -q  # IDs only
    """
    client = ctx.get_client()

    # Get projects
    resp = client.get_json(
        "/data/projects",
        params={"columns": "ID,name,pi_lastname,description"},
    )
    results = HierarchyService.extract_rows(resp)

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

    print_output(
        projects,
        format=ctx.output_format,
        columns=["id", "name", "pi", "description"],
        column_labels={"id": "ID", "name": "Name", "pi": "PI", "description": "Description"},
        quiet=ctx.quiet,
        id_field="id",
    )


@project.command("show")
@click.argument("project_id")
@global_options
@require_auth
@handle_errors
def project_show(ctx: Context, project_id: str) -> None:
    """Show project details.

    Example:
        xnatctl project show MYPROJECT
    """
    from xnatctl.core.validation import validate_project_id

    project_id = validate_project_id(project_id)
    client = ctx.get_client()
    hierarchy = HierarchyService(client)

    # Get project details
    resp = client.get_json(hierarchy.build_project_path(ProjectRef(project_id=project_id)))
    project_data: dict[str, Any] | None
    project_item = hierarchy.extract_first_item(resp)
    if project_item is not None:
        project_data, _project_meta = project_item
    else:
        results = HierarchyService.extract_rows(resp)
        project_data = results[0] if results else None

    if not project_data:
        print_error(f"Project not found: {project_id}")
        raise SystemExit(1)

    # Get counts
    try:
        subjects_resp = client.get_json(
            hierarchy.build_project_path(ProjectRef(project_id=project_id), "subjects")
        )
        subject_count: int | str = len(HierarchyService.extract_rows(subjects_resp))
    except Exception:
        subject_count = "?"

    try:
        sessions_resp = client.get_json(
            hierarchy.build_project_path(ProjectRef(project_id=project_id), "experiments")
        )
        session_count: int | str = len(HierarchyService.extract_rows(sessions_resp))
    except Exception:
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
@require_auth
@handle_errors
def project_create(
    ctx: Context,
    project_id: str,
    name: str | None,
    description: str | None,
    pi: str | None,
    accessibility: str,
) -> None:
    """Create a new project.

    Example:
        xnatctl project create NEWPROJ --name "New Project" --pi Smith
    """
    from xnatctl.core.validation import validate_project_id

    project_id = validate_project_id(project_id)
    client = ctx.get_client()

    # Build project XML
    name = name or project_id
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<xnat:Project ID="{project_id}" xmlns:xnat="http://nrg.wustl.edu/xnat">
    <xnat:name>{name}</xnat:name>
"""
    if description:
        xml += f"    <xnat:description>{description}</xnat:description>\n"
    if pi:
        xml += f"    <xnat:PI><xnat:lastname>{pi}</xnat:lastname></xnat:PI>\n"
    xml += "</xnat:Project>"

    # Create project
    resp = client.post(
        f"/data/projects/{project_id}",
        params={"accessibility": accessibility},
        data=xml,
        headers={"Content-Type": "text/xml"},
    )

    if resp.status_code in (200, 201):
        print_success(f"Project created: {project_id}")
    else:
        print_error(f"Failed to create project: {resp.text}")
        raise SystemExit(1)


# =============================================================================
# Transfer Commands
# =============================================================================


@project.command("transfer")
@click.option("-P", "--project", "source_project", required=True, help="Source project ID")
@click.option("--dest-project", required=True, help="Destination project ID")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Transfer config YAML")
@dest_profile_options
@global_options
@require_auth
@handle_errors
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

    Example:
        xnatctl project transfer -P SRC --dest-profile staging --dest-project DST
        xnatctl project transfer -P SRC --dest-profile staging --dest-project DST --dry-run
    """
    from xnatctl.core.config import CONFIG_DIR
    from xnatctl.core.state import TransferStateStore
    from xnatctl.models.transfer import TransferConfig
    from xnatctl.services.transfer.orchestrator import TransferOrchestrator

    # Resolve workers from profile
    if workers is None:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        workers = profile.workers if (profile and profile.workers is not None) else 4

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
@require_auth
@handle_errors
def project_transfer_status(ctx: Context, source_project: str) -> None:
    """Show status of the last transfer run.

    Example:
        xnatctl project transfer-status -P MYPROJECT
    """
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
@require_auth
@handle_errors
def project_transfer_history(ctx: Context, source_project: str) -> None:
    """Show transfer history for a project.

    Example:
        xnatctl project transfer-history -P MYPROJECT
        xnatctl project transfer-history -P MYPROJECT -o json
    """
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
@require_auth
@handle_errors
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
                "detail": src_info["version"],
            }
        )
    except Exception as e:
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
                "detail": src_user["username"],
            }
        )
    except Exception as e:
        checks.append({"check": "Source auth", "status": "FAIL", "detail": str(e)})

    try:
        dest_client.authenticate()
        dst_info = dest_client.ping()
        checks.append(
            {
                "check": "Dest connectivity",
                "status": "OK",
                "detail": dst_info["version"],
            }
        )
    except Exception as e:
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
                "detail": dst_user["username"],
            }
        )
    except Exception as e:
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

    Example:
        xnatctl project transfer-init -P SRC --dest-project DST
        xnatctl project transfer-init -P SRC --dest-project DST -f transfer.yaml
    """
    from xnatctl.models.transfer import TransferConfig

    yaml_content = TransferConfig.scaffold(source_project, dest_project)

    if output_file:
        Path(output_file).write_text(yaml_content)
        print_success(f"Config written to {output_file}")
    else:
        click.echo(yaml_content)
