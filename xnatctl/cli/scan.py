"""Scan commands for xnatctl."""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    parallel_options,
    require_auth,
)
from xnatctl.core.output import print_error, print_json, print_output, print_success


@click.group()
def scan() -> None:
    """Manage XNAT scans."""
    pass


@scan.command("list")
@click.argument("session_id")
@global_options
@require_auth
@handle_errors
def scan_list(ctx: Context, session_id: str) -> None:
    """List scans in a session.

    Example:
        xnatctl scan list XNAT_E00001
        xnatctl scan list XNAT_E00001 -o json
        xnatctl scan list XNAT_E00001 -q  # IDs only
    """
    from xnatctl.core.validation import validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()

    # Get scans
    resp = client.get_json(f"/data/experiments/{session_id}/scans")
    results = resp.get("ResultSet", {}).get("Result", [])

    # Transform for output
    scans = []
    for r in results:
        scans.append(
            {
                "id": r.get("ID", ""),
                "type": r.get("type", ""),
                "series_description": r.get("series_description", ""),
                "quality": r.get("quality", ""),
                "frames": r.get("frames", ""),
                "note": r.get("note", ""),
            }
        )

    print_output(
        scans,
        format=ctx.output_format,
        columns=["id", "type", "series_description", "quality", "frames"],
        column_labels={
            "id": "ID",
            "type": "Type",
            "series_description": "Series Description",
            "quality": "Quality",
            "frames": "Frames",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@scan.command("show")
@click.argument("session_id")
@click.argument("scan_id")
@global_options
@require_auth
@handle_errors
def scan_show(ctx: Context, session_id: str, scan_id: str) -> None:
    """Show scan details.

    Example:
        xnatctl scan show XNAT_E00001 1
    """
    from xnatctl.core.validation import validate_scan_id, validate_session_id

    session_id = validate_session_id(session_id)
    scan_id = validate_scan_id(scan_id)
    client = ctx.get_client()

    # Get scan details
    resp = client.get_json(f"/data/experiments/{session_id}/scans/{scan_id}")
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Scan not found: {scan_id}")
        raise SystemExit(1)

    scan_data = results[0]

    # Get resources
    try:
        res_resp = client.get_json(f"/data/experiments/{session_id}/scans/{scan_id}/resources")
        resources = res_resp.get("ResultSet", {}).get("Result", [])
    except Exception:
        resources = []

    output = {
        "id": scan_data.get("ID", ""),
        "type": scan_data.get("type", ""),
        "series_description": scan_data.get("series_description", ""),
        "quality": scan_data.get("quality", ""),
        "frames": scan_data.get("frames", ""),
        "note": scan_data.get("note", ""),
        "resources": [r.get("label", "") for r in resources],
    }

    print_output(
        output,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="id",
    )


@scan.command("delete")
@click.argument("session_id")
@click.option("--scans", "-s", required=True, help="Scan IDs (comma-separated or '*' for all)")
@confirm_destructive("Delete these scans?")
@parallel_options
@global_options
@require_auth
@handle_errors
def scan_delete(
    ctx: Context,
    session_id: str,
    scans: str,
    dry_run: bool,
    parallel: bool,
    workers: int,
) -> None:
    """Delete scans from a session.

    Example:
        xnatctl scan delete XNAT_E00001 --scans 1,2,3
        xnatctl scan delete XNAT_E00001 --scans '*'  # Delete all
        xnatctl scan delete XNAT_E00001 --scans 1,2 --dry-run
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from xnatctl.core.validation import validate_scan_ids_input, validate_session_id

    session_id = validate_session_id(session_id)
    scan_ids = validate_scan_ids_input(scans)
    client = ctx.get_client()

    # If wildcard, get all scan IDs
    if scan_ids is None:
        resp = client.get_json(f"/data/experiments/{session_id}/scans")
        results = resp.get("ResultSet", {}).get("Result", [])
        scan_ids = [r.get("ID", "") for r in results if r.get("ID")]

    if not scan_ids:
        print_error("No scans to delete")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would delete {len(scan_ids)} scans:")
        for sid in scan_ids:
            click.echo(f"  - {sid}")
        return

    deleted = []
    failed = []

    def delete_scan(scan_id: str) -> tuple[str, bool, str]:
        """Delete a scan and return status and error message."""
        try:
            resp = client.delete(f"/data/experiments/{session_id}/scans/{scan_id}")
            return scan_id, resp.status_code in (200, 204), ""
        except Exception as e:
            return scan_id, False, str(e)

    if parallel and len(scan_ids) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(scan_ids))) as executor:
            futures = {executor.submit(delete_scan, sid): sid for sid in scan_ids}
            for future in as_completed(futures):
                scan_id, success, error = future.result()
                if success:
                    deleted.append(scan_id)
                else:
                    failed.append((scan_id, error))
    else:
        for scan_id in scan_ids:
            scan_id, success, error = delete_scan(scan_id)
            if success:
                deleted.append(scan_id)
            else:
                failed.append((scan_id, error))

    if deleted:
        print_success(f"Deleted {len(deleted)} scans")

    if failed:
        print_error(f"Failed to delete {len(failed)} scans:")
        for scan_id, error in failed:
            click.echo(f"  - {scan_id}: {error}")
        raise SystemExit(1)


@scan.command("download")
@click.option(
    "--experiment", "-E", "session_id", required=True, help="Session/Experiment ID or label"
)
@click.option("--project", "-P", help="Project ID (required when using session label)")
@click.option("--scans", "-s", required=True, help="Scan IDs (comma-separated or '*' for all)")
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option("--name", help="Output directory name (defaults to experiment value)")
@click.option(
    "--resource",
    "-r",
    default=None,
    help="Resource type to download (DICOM, NIFTI, etc). Omit for all resources.",
)
@click.option("--unzip/--no-unzip", default=False, help="Extract downloaded ZIPs")
@click.option(
    "--cleanup/--no-cleanup",
    default=True,
    help="Remove ZIP after successful extraction (with --unzip)",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@global_options
@require_auth
@handle_errors
def scan_download(
    ctx: Context,
    session_id: str,
    project: str | None,
    scans: str,
    out: str,
    name: str | None,
    resource: str | None,
    unzip: bool,
    cleanup: bool,
    dry_run: bool,
) -> None:
    """Download scans from an image session.

    Downloads all specified scans in a single request using XNAT's batch download
    feature. Output is saved to {out}/{experiment}/scans.zip.

    The output directory defaults to the value passed to -E/--experiment.
    Override it with --name.

    Use --resource to download specific resource type (DICOM, NIFTI, etc).
    Omit --resource to download all resources for the scans.

    Examples:
        xnatctl scan download -E XNAT_E00001 -s 1
        xnatctl scan download -E XNAT_E00001 -s 1 --out ./data
        xnatctl scan download -P PROJECT -E SESSION_LABEL -s 1,2,3 --out ./data
        xnatctl scan download -P PROJECT -E SESSION -s '*' --out ./data
    """
    from xnatctl.core.validation import validate_scan_ids_input, validate_session_id
    from xnatctl.models.progress import DownloadProgress, OperationPhase
    from xnatctl.services.downloads import DownloadService

    session_id = validate_session_id(session_id)
    scan_ids_input = validate_scan_ids_input(scans)
    output_dir = Path(out)
    client = ctx.get_client()

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

    use_all_keyword = scan_ids_input is None
    if scan_ids_input is None:
        scan_ids = ["ALL"]
    else:
        scan_ids = scan_ids_input

    if dry_run:
        scan_desc = "all scans" if use_all_keyword else f"{len(scan_ids)} scans"
        resource_desc = resource if resource else "all resources"
        click.echo(
            f"[DRY-RUN] Would download {scan_desc} ({resource_desc}) to {output_dir}/{name or session_id}/"
        )
        if not use_all_keyword:
            for sid in scan_ids:
                click.echo(f"  - Scan {sid}")
        return

    session_output = output_dir / (name or session_id)
    session_output.mkdir(parents=True, exist_ok=True)
    service = DownloadService(client)

    def progress_cb(progress: DownloadProgress) -> None:
        if progress.phase == OperationPhase.DOWNLOADING and not ctx.quiet:
            if progress.total_bytes:
                pct = progress.bytes_received * 100 // progress.total_bytes
                mb = progress.bytes_received / (1024 * 1024)
                click.echo(f"\r  Downloading: {pct}% ({mb:.1f} MB)", nl=False)

    try:
        summary = service.download_scans(
            session_id=session_id,
            scan_ids=scan_ids,
            output_dir=session_output,
            project=project,
            resource=resource,
            zip_filename="scans.zip",
            extract=unzip,
            cleanup=cleanup,
            progress_callback=progress_cb if not ctx.quiet else None,
        )
    except ValueError as e:
        print_error(str(e))
        raise SystemExit(1) from None

    if not ctx.quiet:
        click.echo()

    if ctx.output_format == "json":
        print_json(
            {
                "session_id": session_id,
                "output_path": summary.output_path,
                "success": summary.success,
                "total_size_mb": round(summary.total_size_mb, 2),
                "errors": summary.errors,
            }
        )
    else:
        if summary.success:
            kept_zip_suffix = (
                f" (kept {session_output / 'scans.zip'})" if unzip and not cleanup else ""
            )
            print_success(
                f"Downloaded scans ({summary.total_size_mb:.1f} MB) to {summary.output_path}{kept_zip_suffix}"
            )
        else:
            print_error(
                f"Download failed: {summary.errors[0] if summary.errors else 'Unknown error'}"
            )
            raise SystemExit(1)
