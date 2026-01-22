"""Project commands for xnatctl."""

from __future__ import annotations

import click

from xnatctl.cli.common import Context, global_options, handle_errors, require_auth
from xnatctl.core.output import print_error, print_output


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
    resp = client.get_json("/data/projects", params={"columns": "ID,name,pi_lastname,description"})
    results = resp.get("ResultSet", {}).get("Result", [])

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

    # Get project details
    resp = client.get_json(f"/data/projects/{project_id}")
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Project not found: {project_id}")
        raise SystemExit(1)

    project_data = results[0]

    # Get counts
    try:
        subjects_resp = client.get_json(f"/data/projects/{project_id}/subjects")
        subject_count: int | str = len(subjects_resp.get("ResultSet", {}).get("Result", []))
    except Exception:
        subject_count = "?"

    try:
        sessions_resp = client.get_json(f"/data/projects/{project_id}/experiments")
        session_count: int | str = len(sessions_resp.get("ResultSet", {}).get("Result", []))
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
    from xnatctl.core.output import print_success
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
