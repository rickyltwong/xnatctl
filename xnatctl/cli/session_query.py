"""Session query commands for xnatctl (list / show).

Registers ``session list`` and ``session show`` onto the ``session`` group
defined in :mod:`xnatctl.cli.session`, mirroring how
:mod:`xnatctl.cli.session_upload` registers the upload family.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    apply_filter,
    apply_sort_limit,
    default_project_from_context,
    global_options,
    handle_errors,
    list_options,
    require_auth,
    require_project_from_context,
    resolve_columns,
)
from xnatctl.cli.session import session
from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.core.output import OutputFormat, print_error, print_output, print_table
from xnatctl.core.validation import validate_project_id, validate_session_id
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.sessions import SessionService


@session.command("list")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", help="Filter by subject")
@click.option(
    "--modality",
    help=(
        "Filter by modality, case-insensitive (e.g. MR, PET, CT, EEG, US, XA, CR, MG; "
        "PETMR is its own value, distinct from PET; OCT is accepted as an alias for OPT)"
    ),
)
@list_options
@global_options
@handle_errors
@require_auth
def session_list(
    ctx: Context,
    project: str | None,
    subject: str | None,
    modality: str | None,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List sessions/experiments in a project.

    \b
    Example:
        xnatctl session list --project MYPROJ
        xnatctl session list -P MYPROJ --subject SUB001
        xnatctl session list -P MYPROJ --modality MR
        xnatctl session list -P MYPROJ --modality oct
        xnatctl session list -P MYPROJ --sort-by date:desc --limit 10
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    if modality is not None:
        modality = modality.upper()
    sessions = SessionService(ctx.get_client()).list_sessions(
        project, subject=subject, modality=modality
    )

    sessions = apply_filter(sessions, filter_expr)
    sessions = apply_sort_limit(sessions, sort_by, limit)

    default_columns = ["id", "label", "subject", "date", "modality"]
    print_output(
        sessions,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        column_labels={
            "id": "ID",
            "label": "Label",
            "subject": "Subject",
            "date": "Date",
            "modality": "Modality",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@session.command("show")
@click.option(
    "--experiment",
    "-E",
    "session_id",
    required=True,
    metavar="ID_OR_LABEL",
    help="Experiment ID (accession #), or label when -P is provided",
)
@click.option(
    "--project",
    "-P",
    help="Project ID (enables lookup by label; defaults to profile default_project)",
)
@global_options
@handle_errors
@require_auth
def session_show(ctx: Context, session_id: str, project: str | None) -> None:
    """Show session details including scans and resources.

    \b
    Example:
        xnatctl session show -E XNAT_E00001
        xnatctl session show -E SESSION_LABEL -P MYPROJ
    """
    session_id = validate_session_id(session_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    session_ref = ExperimentRef(
        experiment=session_id,
        project_id=project,
        experiment_is_label=project is not None,
    )

    try:
        resolved = hierarchy.resolve_experiment(session_ref)
    except ResourceNotFoundError:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1) from None

    # Keep project scope (and subject scope if known) so nested calls respect
    # project ACLs and match pre-refactor URLs.
    nested_ref = ExperimentRef(
        experiment=resolved.experiment_id,
        project_id=resolved.project_id or project,
        subject=resolved.subject_id or resolved.subject_label,
    )

    # Get scans — resolve xsiType so non-imaging sessions return results
    try:
        scan_params: dict[str, str] = {}
        scan_xsi = hierarchy.resolve_scan_xsi_type(resolved.xsi_type)
        if scan_xsi:
            scan_params["xsiType"] = scan_xsi
        scans_resp = hierarchy.get_experiment_json(nested_ref, "scans", params=scan_params)
        scans = HierarchyService.extract_rows(scans_resp)
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list scans: {exc}", err=True)
        scans = []

    # Get resources
    try:
        res_resp = hierarchy.get_experiment_json(nested_ref, "resources")
        resources = HierarchyService.extract_rows(res_resp)
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list resources: {exc}", err=True)
        resources = []

    # Projects this session is shared into, beyond its primary one --
    # GET /data/experiments/{id}/projects returns every assigned project
    # (primary included), so the primary is filtered back out here.
    try:
        share_rows = [
            r
            for r in SessionService(client).list_shares(resolved.experiment_id)
            if r.get("ID") != (resolved.project_id or project)
        ]
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list shared projects: {exc}", err=True)
        share_rows = []

    if ctx.output_format == OutputFormat.JSON:
        output = {
            "id": resolved.experiment_id,
            "label": resolved.experiment_label or "",
            "subject": resolved.subject_label or resolved.subject_id or "",
            "project": resolved.project_id or "",
            "date": resolved.session_date or "",
            "xsi_type": resolved.xsi_type or "",
            "scans": scans,
            "resources": resources,
            "shared_projects": [
                {"project": r.get("ID", ""), "label": r.get("label", "")} for r in share_rows
            ],
        }
        print_output(output, format=OutputFormat.JSON)
        return

    scan_rows = [
        {
            "id": s.get("ID", ""),
            "type": s.get("type", ""),
            "series": s.get("series_description", ""),
            "quality": s.get("quality", ""),
            "frames": s.get("frames", ""),
        }
        for s in scans
    ]
    res_rows = [
        {
            "label": r.get("label", ""),
            "format": r.get("format", ""),
            "count": r.get("file_count", ""),
            "size": r.get("file_size", ""),
        }
        for r in resources
    ]
    share_display_rows = [
        {"project": r.get("ID", ""), "label": r.get("label", "")} for r in share_rows
    ]

    if ctx.output_format == OutputFormat.TSV:
        # Three TSV blocks (session info, scans, resources) rather than the
        # decorative "[Session: ...]"/"[Scans (N)]" headers the table branch
        # below prints -- those are not data and would corrupt a
        # tab-separated stream. A blank line separates non-empty blocks;
        # each block's own header row (distinct field names, or none under
        # --no-headers) is what tells them apart. Routed through
        # print_output like every other TSV render in the CLI.
        session_row = {
            "id": resolved.experiment_id,
            "label": resolved.experiment_label or "",
            "subject": resolved.subject_label or resolved.subject_id or "",
            "project": resolved.project_id or "",
            "date": resolved.session_date or "",
            "xsi_type": resolved.xsi_type or "",
        }
        print_output(session_row, format=OutputFormat.TSV)
        if scan_rows:
            print()
            print_output(
                scan_rows,
                format=OutputFormat.TSV,
                columns=["id", "type", "series", "quality", "frames"],
            )
        if res_rows:
            print()
            print_output(
                res_rows,
                format=OutputFormat.TSV,
                columns=["label", "format", "count", "size"],
            )
        if share_display_rows:
            print()
            print_output(
                share_display_rows,
                format=OutputFormat.TSV,
                columns=["project", "label"],
            )
        return

    # Print session info
    click.echo(f"\n[Session: {resolved.experiment_label or session_id}]")
    click.echo(f"  ID:      {resolved.experiment_id}")
    click.echo(f"  Subject: {resolved.subject_label or resolved.subject_id or ''}")
    click.echo(f"  Project: {resolved.project_id or ''}")
    click.echo(f"  Date:    {resolved.session_date or ''}")
    click.echo(f"  Type:    {resolved.xsi_type or ''}")

    # Print scans table
    if scan_rows:
        click.echo(f"\n[Scans ({len(scan_rows)})]")
        print_table(
            scan_rows,
            ["id", "type", "series", "quality", "frames"],
            column_labels={
                "id": "ID",
                "type": "Type",
                "series": "Series",
                "quality": "Quality",
                "frames": "Frames",
            },
        )

    # Print resources table
    if res_rows:
        click.echo(f"\n[Resources ({len(res_rows)})]")
        print_table(
            res_rows,
            ["label", "format", "count", "size"],
            column_labels={
                "label": "Label",
                "format": "Format",
                "count": "Files",
                "size": "Size",
            },
        )

    # Print shared-projects table
    if share_display_rows:
        click.echo(f"\n[Shared into ({len(share_display_rows)})]")
        print_table(
            share_display_rows,
            ["project", "label"],
            column_labels={"project": "Project", "label": "Label there"},
        )
