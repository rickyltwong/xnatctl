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
)
from xnatctl.services.hierarchy import HierarchyService


@click.group()
def resource() -> None:
    """Manage XNAT resources."""
    pass


def _validate_resource_list_scope(
    ctx: click.Context,
    param: click.Parameter,
    session_id: str | None,
) -> str | None:
    """Validate resource-list scope during Click parsing, before auth runs."""

    del param
    project_id = ctx.params.get("project_id")
    scan = ctx.params.get("scan")
    if project_id and session_id:
        raise click.UsageError("Use either SESSION_ID or --project, not both")
    if project_id and scan:
        raise click.UsageError("--scan can only be used with SESSION_ID")
    if not project_id and not session_id:
        raise click.UsageError("Provide SESSION_ID or --project PROJECT_ID")
    return session_id


@resource.command("list")
@click.option("--project", "-P", "project_id", help="List resources at project scope")
@click.option("--scan", help="Scope to specific scan")
@click.argument("session_id", required=False, callback=_validate_resource_list_scope)
@global_options
@handle_errors
@require_auth
def resource_list(
    ctx: Context,
    session_id: str | None,
    project_id: str | None,
    scan: str | None,
) -> None:
    """List resources at project, session, or scan level.

    \b
    Example:
        xnatctl resource list --project MYPROJ
        xnatctl resource list XNAT_E00001
        xnatctl resource list XNAT_E00001 --scan 1
    """
    from xnatctl.core.validation import validate_project_id, validate_scan_id, validate_session_id

    client = ctx.get_client()
    hierarchy = HierarchyService(client)

    resource_parent: HierarchyParentRef
    if project_id:
        project_id = validate_project_id(project_id)
        resource_parent = ProjectRef(project_id=project_id)
    elif session_id is not None:
        session_id = validate_session_id(session_id)
        experiment_ref = ExperimentRef(experiment=session_id)
        if scan:
            scan = validate_scan_id(scan)
            resource_parent = ScanRef(experiment=experiment_ref, scan_id=scan)
        else:
            resource_parent = experiment_ref
    else:
        # Guarded by UsageError above; keeps type checkers exhaustive.
        raise click.UsageError("Provide SESSION_ID or --project PROJECT_ID")

    resp = client.get_json(hierarchy.build_resource_collection_path(resource_parent))
    results = HierarchyService.extract_rows(resp)

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

    session_id = validate_session_id(session_id)
    resource_label = validate_resource_label(resource_label)
    client = ctx.get_client()
    hierarchy = HierarchyService(client)
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
    resp = client.get_json(hierarchy.build_resource_collection_path(resource_parent))
    results = HierarchyService.extract_rows(resp)
    resource_data = next((row for row in results if row.get("label") == resource_label), None)

    if resource_data is None:
        print_error(f"Resource not found: {resource_label}")
        raise SystemExit(1)

    # Get files
    try:
        files_resp = client.get_json(
            hierarchy.build_resource_path(
                ResourceRef(parent=resource_parent, resource_label=encoded_label),
                "files",
            )
        )
        files = HierarchyService.extract_rows(files_resp)
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
    help=(
        "Project ID for label-based session resolution; "
        "defaults to active profile's default_project"
    ),
)
@click.argument("session_id")
@click.argument("resource_label")
@click.argument("path", type=click.Path(exists=True))
@click.option("--scan", help="Upload to scan resource instead of session")
@click.option("--content", help="Content type/description")
@click.option("--format", "file_format", help="File format (e.g., DICOM, NIFTI)")
@global_options
@handle_errors
@require_auth
def resource_upload(
    ctx: Context,
    project: str | None,
    session_id: str,
    resource_label: str,
    path: str,
    scan: str | None,
    content: str | None,
    file_format: str | None,
) -> None:
    """Upload file or directory to a resource.

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
    """
    from xnatctl.core.output import create_progress
    from xnatctl.core.validation import (
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
    )
    from xnatctl.services.resources import ResourceService

    session_id = validate_session_id(session_id)
    resource_label = validate_resource_label(resource_label)
    input_path = Path(path)
    client = ctx.get_client()
    service = ResourceService(client)
    if scan:
        scan = validate_scan_id(scan)

    project = project or default_project_from_context(ctx)

    # Create resource if it doesn't exist
    try:
        service.create(
            session_id=session_id,
            resource_label=resource_label,
            scan_id=scan,
            project=project,
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
                    session_id=session_id,
                    resource_label=resource_label,
                    directory_path=input_path,
                    scan_id=scan,
                    project=project,
                    overwrite=False,
                )
                progress.update(task, description="Done")
            else:
                task = progress.add_task(f"Uploading {input_path.name}...", total=100)
                service.upload_file(
                    session_id=session_id,
                    resource_label=resource_label,
                    file_path=input_path,
                    scan_id=scan,
                    project=project,
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
    client = ctx.get_client()
    params: dict[str, str] = {"resource": uri}
    if options:
        params["options"] = ",".join(options)
    resp = client.post("/data/services/refresh/catalog", params=params)
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
@click.argument("session_id")
@click.argument("resource_label")
@click.option("--file", "-f", "out", required=True, type=click.Path(), help="Output file path")
@click.option("--scan", help="Download from scan resource")
@global_options
@handle_errors
@require_auth
def resource_download(
    ctx: Context,
    session_id: str,
    resource_label: str,
    out: str,
    scan: str | None,
) -> None:
    """Download a resource as ZIP.

    \b
    Example:
        xnatctl resource download XNAT_E00001 BIDS --file ./bids.zip
        xnatctl resource download XNAT_E00001 DICOM -f ./dicom.zip --scan 1
    """
    from urllib.parse import quote

    from xnatctl.core.output import create_progress
    from xnatctl.core.validation import (
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
    )

    session_id = validate_session_id(session_id)
    resource_label = validate_resource_label(resource_label)
    out_path = Path(out)
    client = ctx.get_client()

    if scan:
        scan = validate_scan_id(scan)
        url = f"/data/experiments/{session_id}/scans/{scan}/resources/{quote(resource_label)}/files"
    else:
        url = f"/data/experiments/{session_id}/resources/{quote(resource_label)}/files"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with create_progress() as progress:
        task = progress.add_task(f"Downloading {resource_label}...", total=100)

        with client._get_client().stream(
            "GET", url, params={"format": "zip"}, cookies=client._get_cookies()
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(out_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        progress.update(task, completed=int(downloaded / total * 100))

        progress.update(task, completed=100)

    print_success(f"Downloaded to {out_path}")
