"""Resource commands for xnatctl."""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import Context, global_options, handle_errors, require_auth
from xnatctl.core.output import print_error, print_output, print_success


@click.group()
def resource() -> None:
    """Manage XNAT resources."""
    pass


@resource.command("list")
@click.argument("session_id")
@click.option("--scan", help="Scope to specific scan")
@global_options
@require_auth
@handle_errors
def resource_list(ctx: Context, session_id: str, scan: str | None) -> None:
    """List resources at session or scan level.

    Example:
        xnatctl resource list XNAT_E00001
        xnatctl resource list XNAT_E00001 --scan 1
    """
    from xnatctl.core.validation import validate_scan_id, validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()

    if scan:
        scan = validate_scan_id(scan)
        url = f"/data/experiments/{session_id}/scans/{scan}/resources"
    else:
        url = f"/data/experiments/{session_id}/resources"

    resp = client.get_json(url)
    results = resp.get("ResultSet", {}).get("Result", [])

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
@require_auth
@handle_errors
def resource_show(ctx: Context, session_id: str, resource_label: str, scan: str | None) -> None:
    """Show resource details and files.

    Example:
        xnatctl resource show XNAT_E00001 DICOM
        xnatctl resource show XNAT_E00001 DICOM --scan 1
    """
    from urllib.parse import quote

    from xnatctl.core.validation import (
        validate_resource_label,
        validate_scan_id,
        validate_session_id,
    )

    session_id = validate_session_id(session_id)
    resource_label = validate_resource_label(resource_label)
    client = ctx.get_client()

    if scan:
        scan = validate_scan_id(scan)
        base_url = f"/data/experiments/{session_id}/scans/{scan}/resources/{quote(resource_label)}"
    else:
        base_url = f"/data/experiments/{session_id}/resources/{quote(resource_label)}"

    # Get resource info
    resp = client.get_json(base_url)
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Resource not found: {resource_label}")
        raise SystemExit(1)

    resource_data = results[0] if isinstance(results, list) else results

    # Get files
    try:
        files_resp = client.get_json(f"{base_url}/files")
        files = files_resp.get("ResultSet", {}).get("Result", [])
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
@click.argument("session_id")
@click.argument("resource_label")
@click.argument("path", type=click.Path(exists=True))
@click.option("--scan", help="Upload to scan resource instead of session")
@click.option("--content", help="Content type/description")
@click.option("--format", "file_format", help="File format (e.g., DICOM, NIFTI)")
@global_options
@require_auth
@handle_errors
def resource_upload(
    ctx: Context,
    session_id: str,
    resource_label: str,
    path: str,
    scan: str | None,
    content: str | None,
    file_format: str | None,
) -> None:
    """Upload file or directory to a resource.

    Directories are zipped and extracted server-side.

    Example:
        xnatctl resource upload XNAT_E00001 BIDS ./bids_data
        xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
        xnatctl resource upload XNAT_E00001 DICOM ./dicoms --scan 1
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

    # Create resource if it doesn't exist
    try:
        service.create(
            session_id=session_id,
            resource_label=resource_label,
            scan_id=scan,
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
                    extract=False,
                    overwrite=False,
                )
                progress.update(task, completed=100)
    except Exception as exc:
        raise click.ClickException(f"Upload failed: {exc}") from exc

    print_success(f"Uploaded to {resource_label}")


@resource.command("download")
@click.argument("session_id")
@click.argument("resource_label")
@click.option("--file", "-f", "out", required=True, type=click.Path(), help="Output file path")
@click.option("--scan", help="Download from scan resource")
@global_options
@require_auth
@handle_errors
def resource_download(
    ctx: Context,
    session_id: str,
    resource_label: str,
    out: str,
    scan: str | None,
) -> None:
    """Download a resource as ZIP.

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
