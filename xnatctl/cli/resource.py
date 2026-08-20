"""Resource commands for xnatctl."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import click

from xnatctl.cli.common import (
    Context,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import print_error, print_output, print_success
from xnatctl.models.hierarchy import (
    ExperimentRef,
    HierarchyParentRef,
    ProjectRef,
    ResourceRef,
    ScanRef,
    SubjectRef,
)
from xnatctl.services.downloads import stream_to_file
from xnatctl.services.hierarchy import HierarchyService


@click.group()
def resource() -> None:
    """Manage XNAT resources."""
    pass


def _resolve_resource_parent(
    *,
    session_id: str | None,
    project_id: str | None,
    subject: str | None,
    scan: str | None,
) -> HierarchyParentRef:
    """Build a resource parent ref from CLI scope flags.

    Levels map to XNAT resource URLs (session XOR project/subject):

    \b
    - project:    -P                -> /data/projects/{P}/resources/...
    - subject:    -P -S             -> /data/projects/{P}/subjects/{S}/resources/...
    - experiment: SESSION [+ -P]    -> /data/experiments/{E}/resources/... (or project-scoped)
    - scan:       SESSION --scan N  -> .../scans/{N}/resources/...
    """
    if session_id is not None:
        if subject:
            raise click.UsageError("--subject cannot be combined with a session argument")
        experiment = ExperimentRef(experiment=session_id, project_id=project_id)
        if scan:
            return ScanRef(experiment=experiment, scan_id=scan)
        return experiment
    if scan:
        raise click.UsageError("--scan requires a session argument")
    if subject:
        if not project_id:
            raise click.UsageError("--subject requires --project")
        return SubjectRef(subject=subject, project_id=project_id)
    if project_id:
        return ProjectRef(project_id=project_id)
    raise click.UsageError(
        "Provide a SESSION argument, or --project [--subject] for project/subject-scope resources"
    )


def _validate_resource_list_scope(
    ctx: click.Context,
    param: click.Parameter,
    session_id: str | None,
) -> str | None:
    """Validate resource-list scope during Click parsing, before auth runs."""
    del param
    project_id = ctx.params.get("project_id")
    subject = ctx.params.get("subject")
    scan = ctx.params.get("scan")
    if project_id and session_id:
        raise click.UsageError("Use either SESSION_ID or --project, not both")
    if subject and session_id:
        raise click.UsageError("--subject cannot be combined with SESSION_ID")
    if subject and not project_id:
        raise click.UsageError("--subject requires --project")
    if project_id and scan:
        raise click.UsageError("--scan can only be used with SESSION_ID")
    if not project_id and not session_id:
        raise click.UsageError("Provide SESSION_ID or --project PROJECT_ID")
    return session_id


@resource.command("list")
@click.option("--project", "-P", "project_id", help="List resources at project scope")
@click.option("--subject", "-S", help="List resources at subject scope (requires --project)")
@click.option("--scan", help="Scope to specific scan")
@click.argument("session_id", required=False, callback=_validate_resource_list_scope)
@global_options
@handle_errors
@require_auth
def resource_list(
    ctx: Context,
    session_id: str | None,
    project_id: str | None,
    subject: str | None,
    scan: str | None,
) -> None:
    """List resources at project, subject, session, or scan level.

    \b
    Example:
        xnatctl resource list --project MYPROJ
        xnatctl resource list --project MYPROJ --subject SUB01
        xnatctl resource list XNAT_E00001
        xnatctl resource list XNAT_E00001 --scan 1
    """
    from xnatctl.core.validation import (
        validate_project_id,
        validate_scan_id,
        validate_session_id,
        validate_subject_id,
    )
    from xnatctl.services.resources import ResourceService

    client = ctx.get_client()

    if project_id:
        project_id = validate_project_id(project_id)
    if subject:
        subject = validate_subject_id(subject)
    if session_id is not None:
        session_id = validate_session_id(session_id)
    if scan:
        scan = validate_scan_id(scan)

    resource_parent = _resolve_resource_parent(
        session_id=session_id, project_id=project_id, subject=subject, scan=scan
    )

    results = ResourceService(client).list_rows(resource_parent)

    resources = []
    for r in results:
        resources.append(
            {
                "label": r.get("label", ""),
                "format": r.get("format", ""),
                "file_count": r.get("file_count", ""),
                "file_size": r.get("file_size", ""),
                "content": r.get("content", ""),
            }
        )

    print_output(
        resources,
        format=ctx.output_format,
        columns=["label", "format", "file_count", "file_size", "content"],
        column_labels={
            "label": "Label",
            "format": "Format",
            "file_count": "Files",
            "file_size": "Size",
            "content": "Content",
        },
        quiet=ctx.quiet,
        id_field="label",
    )


@resource.command("show")
@click.argument("session_id")
@click.argument("resource_label")
@click.option("--scan", help="Scope to specific scan")
@global_options
@handle_errors
@require_auth
def resource_show(ctx: Context, session_id: str, resource_label: str, scan: str | None) -> None:
    """Show resource details and files.

    \b
    Example:
        xnatctl resource show XNAT_E00001 DICOM
        xnatctl resource show XNAT_E00001 DICOM --scan 1
    """
    from xnatctl.core.validation import (
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
    )
    from xnatctl.services.resources import ResourceService

    session_id = validate_session_id(session_id)
    resource_label = validate_resource_label(resource_label)
    service = ResourceService(ctx.get_client())
    experiment_ref = ExperimentRef(experiment=session_id)

    resource_parent: HierarchyParentRef
    if scan:
        scan = validate_scan_id(scan)
        resource_parent = ScanRef(experiment=experiment_ref, scan_id=scan)
    else:
        resource_parent = experiment_ref
    encoded_label = quote(resource_label)

    # Get resource info from the collection endpoint. The direct
    # /resources/{label} endpoint often returns XML catalogs instead of JSON.
    results = service.list_rows(resource_parent)
    resource_data = next((row for row in results if row.get("label") == resource_label), None)

    if resource_data is None:
        print_error(f"Resource not found: {resource_label}")
        raise SystemExit(1)

    # Get files
    try:
        files = service.list_file_rows(resource_parent, encoded_label)
    except Exception:
        files = []

    output = {
        "label": resource_data.get("label", resource_label),
        "format": resource_data.get("format", ""),
        "content": resource_data.get("content", ""),
        "file_count": resource_data.get("file_count", len(files)),
        "file_size": resource_data.get("file_size", ""),
        "files": [f.get("Name", "") for f in files[:20]],  # Limit to first 20
    }

    if len(files) > 20:
        output["files_truncated"] = True
        output["total_files"] = len(files)

    print_output(
        output,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="label",
    )


@resource.command("upload")
@click.option(
    "--project",
    "-P",
    "project_id",
    help=(
        "Project ID: project-scope target when no session is given, or "
        "label-based session resolution (defaults to profile default_project) "
        "when a session is given"
    ),
)
@click.option("--subject", "-S", help="Upload to a subject-scope resource (requires --project)")
@click.argument("session_id", required=False)
@click.argument("resource_label", required=False)
@click.argument("path", required=False, type=click.Path(exists=True))
@click.option("--scan", help="Upload to scan resource (requires a session)")
@click.option("--content", help="Content type/description")
@click.option("--format", "file_format", help="File format (e.g., DICOM, NIFTI)")
@global_options
@handle_errors
@require_auth
def resource_upload(
    ctx: Context,
    project_id: str | None,
    subject: str | None,
    session_id: str | None,
    resource_label: str | None,
    path: str | None,
    scan: str | None,
    content: str | None,
    file_format: str | None,
) -> None:
    """Upload file or directory to a resource at any hierarchy level.

    NOTE: This command PUTs files directly to the resource catalog and BYPASSES
    XNAT project-level DICOM anonymization scripts and pipelines. Use
    ``xnatctl session upload`` or ``xnatctl session upload-exam`` when
    anonymization is required.

    Directories are zipped and extracted server-side.

    \b
    Example:
        xnatctl resource upload XNAT_E00001 BIDS ./bids_data
        xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
        xnatctl resource upload XNAT_E00001 DICOM ./dicoms --scan 1
        xnatctl resource upload --project CLM01_UCA_4 SESSION_LABEL DICOM ./file.dcm --scan 1
        xnatctl resource upload -P MYPROJ TEMPLATEFLOW ./tpl_data
        xnatctl resource upload -P MYPROJ -S SUB01 QC ./qc.json
    """
    from xnatctl.core.output import create_progress
    from xnatctl.core.validation import (
        validate_project_id,
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
        validate_subject_id,
    )
    from xnatctl.services.resources import ResourceService

    # Only two positionals supplied -> they are RESOURCE_LABEL and PATH; the
    # parent comes from --project/--subject (project- or subject-scope resources).
    if path is None:
        path = resource_label
        resource_label = session_id
        session_id = None
    if resource_label is None or path is None:
        raise click.UsageError("Provide RESOURCE_LABEL and PATH (with a session or --project).")

    if project_id:
        project_id = validate_project_id(project_id)
    if subject:
        subject = validate_subject_id(subject)
    if session_id is not None:
        session_id = validate_session_id(session_id)
        # Project context enables label-based session resolution.
        project_id = project_id or default_project_from_context(ctx)
    if scan:
        scan = validate_scan_id(scan)
    resource_label = validate_resource_label(resource_label)
    input_path = Path(path)
    client = ctx.get_client()
    service = ResourceService(client)

    parent = _resolve_resource_parent(
        session_id=session_id, project_id=project_id, subject=subject, scan=scan
    )

    # Create resource if it doesn't exist
    try:
        service.create(
            resource_label=resource_label,
            parent=parent,
            format=file_format,
            content=content,
        )
    except Exception:
        pass  # Resource may already exist

    try:
        with create_progress() as progress:
            if input_path.is_dir():
                task = progress.add_task("Creating archive...", total=None)
                progress.update(task, description="Uploading...")
                service.upload_directory(
                    resource_label=resource_label,
                    directory_path=input_path,
                    parent=parent,
                    overwrite=False,
                )
                progress.update(task, description="Done")
            else:
                task = progress.add_task(f"Uploading {input_path.name}...", total=100)
                service.upload_file(
                    resource_label=resource_label,
                    file_path=input_path,
                    parent=parent,
                    extract=False,
                    overwrite=False,
                )
                progress.update(task, completed=100)
    except Exception as exc:
        raise click.ClickException(f"Upload failed: {exc}") from exc

    print_success(f"Uploaded to {resource_label}")


@resource.command("refresh")
@click.argument("uri")
@click.option(
    "--options",
    multiple=True,
    type=click.Choice(["checksum", "delete", "append", "populateStats"]),
    help="Refresh options (can repeat).",
)
@global_options
@handle_errors
@require_auth
def resource_refresh(ctx: Context, uri: str, options: tuple[str, ...]) -> None:
    """Refresh a single XNAT resource catalog by archive URI.

    \b
    Example:
        xnatctl resource refresh \\
          /archive/projects/MYPROJ/subjects/SUBJ/experiments/EXP/scans/1/resources/DICOM \\
          --options append --options populateStats
    """
    from xnatctl.services.admin import AdminService

    options_str = ",".join(options) if options else None
    resp = AdminService(ctx.get_client()).refresh_catalog(uri, options_str)
    if resp.status_code != 200:
        raise click.ClickException(f"Refresh failed [{resp.status_code}]: {resp.text}")
    payload = {"resource": uri, "options": list(options), "status": "ok"}
    print_output(
        payload,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="resource",
    )


@resource.command("download")
@click.option("--project", "-P", "project_id", help="Download a project-scope resource")
@click.option("--subject", "-S", help="Download a subject-scope resource (requires --project)")
@click.argument("session_id", required=False)
@click.argument("resource_label", required=False)
@click.option("--file", "-f", "out", required=True, type=click.Path(), help="Output file path")
@click.option("--scan", help="Download from scan resource (requires a session)")
@global_options
@handle_errors
@require_auth
def resource_download(
    ctx: Context,
    project_id: str | None,
    subject: str | None,
    session_id: str | None,
    resource_label: str | None,
    out: str,
    scan: str | None,
) -> None:
    """Download a resource as ZIP from any hierarchy level.

    \b
    Example:
        xnatctl resource download XNAT_E00001 BIDS --file ./bids.zip
        xnatctl resource download XNAT_E00001 DICOM -f ./dicom.zip --scan 1
        xnatctl resource download -P MYPROJ TEMPLATEFLOW -f ./tf.zip
        xnatctl resource download -P MYPROJ -S SUB01 QC -f ./qc.zip
    """
    from urllib.parse import quote

    from xnatctl.core.output import create_progress
    from xnatctl.core.validation import (
        validate_project_id,
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
        validate_subject_id,
    )

    # Only one positional supplied -> it is the RESOURCE_LABEL; the parent comes
    # from --project/--subject (project- or subject-scope resources).
    if resource_label is None:
        session_id, resource_label = None, session_id
    if resource_label is None:
        raise click.UsageError("Missing RESOURCE_LABEL.")

    if project_id:
        project_id = validate_project_id(project_id)
    if subject:
        subject = validate_subject_id(subject)
    if session_id is not None:
        session_id = validate_session_id(session_id)
    if scan:
        scan = validate_scan_id(scan)
    resource_label = validate_resource_label(resource_label)
    out_path = Path(out)
    client = ctx.get_client()

    parent = _resolve_resource_parent(
        session_id=session_id, project_id=project_id, subject=subject, scan=scan
    )
    url = HierarchyService.build_resource_path(
        ResourceRef(parent=parent, resource_label=quote(resource_label)), "files"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with create_progress() as progress:
        task = progress.add_task(f"Downloading {resource_label}...", total=100)

        def on_progress(written: int, total: int | None) -> None:
            if total:
                progress.update(task, completed=int(written / total * 100))

        stream_to_file(client, url, out_path, params={"format": "zip"}, progress_cb=on_progress)

        progress.update(task, completed=100)

    print_success(f"Downloaded to {out_path}")
