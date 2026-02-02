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
@click.argument("session_id")
@click.option("--project", "-P", help="Project ID (improves performance)")
@click.option("--scans", "-s", required=True, help="Scan IDs (comma-separated or '*' for all)")
@click.option("--out", required=True, type=click.Path(), help="Output directory")
@click.option(
    "--resource",
    "-r",
    default="DICOM",
    help="Resource type to download (default: DICOM)",
)
@click.option("--unzip/--no-unzip", default=True, help="Extract downloaded ZIPs")
@click.option(
    "--cleanup/--no-cleanup",
    default=True,
    help="Remove ZIPs after successful extraction",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@parallel_options
@global_options
@require_auth
@handle_errors
def scan_download(
    ctx: Context,
    session_id: str,
    project: str | None,
    scans: str,
    out: str,
    resource: str,
    unzip: bool,
    cleanup: bool,
    dry_run: bool,
    parallel: bool,
    workers: int,
) -> None:
    """Download scans from an image session.

    Example:
        xnatctl scan download XNAT_E00001 --scans 1 --out ./data
        xnatctl scan download XNAT_E00001 --scans 1,2,3 --out ./data
        xnatctl scan download XNAT_E00001 --scans '*' --out ./data
        xnatctl scan download SESSION_LABEL -P PROJECT_ID --scans 1 --out ./data
        xnatctl scan download XNAT_E00001 --scans 1 --out ./data --resource NIFTI
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from xnatctl.core.validation import validate_scan_ids_input, validate_session_id
    from xnatctl.models.progress import DownloadProgress, OperationPhase
    from xnatctl.services.downloads import DownloadService

    session_id = validate_session_id(session_id)
    scan_ids = validate_scan_ids_input(scans)
    output_dir = Path(out)
    client = ctx.get_client()

    # If wildcard, get all scan IDs
    if scan_ids is None:
        if project:
            resp = client.get_json(f"/data/projects/{project}/experiments/{session_id}/scans")
        else:
            resp = client.get_json(f"/data/experiments/{session_id}/scans")
        results = resp.get("ResultSet", {}).get("Result", [])
        scan_ids = [r.get("ID", "") for r in results if r.get("ID")]

    if not scan_ids:
        print_error("No scans to download")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would download {len(scan_ids)} scans to {output_dir}:")
        for sid in scan_ids:
            click.echo(f"  - Scan {sid} ({resource})")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    service = DownloadService(client)

    succeeded = []
    failed = []

    def download_one_scan(scan_id: str) -> tuple[str, bool, str, float]:
        """Download a single scan."""
        scan_output = output_dir / f"scan_{scan_id}"

        def progress_cb(progress: DownloadProgress) -> None:
            if progress.phase == OperationPhase.DOWNLOADING and not ctx.quiet:
                if progress.total_bytes:
                    pct = progress.bytes_received * 100 // progress.total_bytes
                    click.echo(
                        f"\r  Scan {scan_id}: {pct}% ({progress.bytes_received // 1024} KB)",
                        nl=False,
                    )

        try:
            summary = service.download_scan(
                session_id=session_id,
                scan_id=scan_id,
                output_dir=scan_output,
                project=project,
                resource=resource,
                progress_callback=progress_cb if not ctx.quiet else None,
            )

            if summary.success:
                return scan_id, True, "", summary.total_size_mb
            else:
                return scan_id, False, summary.errors[0] if summary.errors else "Unknown error", 0.0

        except Exception as e:
            return scan_id, False, str(e), 0.0

    total_size_mb = 0.0

    if parallel and len(scan_ids) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(scan_ids))) as executor:
            futures = {executor.submit(download_one_scan, sid): sid for sid in scan_ids}
            for future in as_completed(futures):
                scan_id, success, error, size_mb = future.result()
                if success:
                    succeeded.append(scan_id)
                    total_size_mb += size_mb
                    if not ctx.quiet:
                        click.echo(f"\r  Scan {scan_id}: Done ({size_mb:.1f} MB)")
                else:
                    failed.append((scan_id, error))
    else:
        for scan_id in scan_ids:
            scan_id, success, error, size_mb = download_one_scan(scan_id)
            if success:
                succeeded.append(scan_id)
                total_size_mb += size_mb
                if not ctx.quiet:
                    click.echo(f"\r  Scan {scan_id}: Done ({size_mb:.1f} MB)")
            else:
                failed.append((scan_id, error))

    # Summary
    if ctx.output_format == "json":
        print_json(
            {
                "session_id": session_id,
                "output_dir": str(output_dir),
                "succeeded": succeeded,
                "failed": [{"scan_id": s, "error": e} for s, e in failed],
                "total_size_mb": round(total_size_mb, 2),
            }
        )
    else:
        if succeeded:
            print_success(
                f"Downloaded {len(succeeded)} scans ({total_size_mb:.1f} MB) to {output_dir}"
            )

        if failed:
            print_error(f"Failed to download {len(failed)} scans:")
            for scan_id, error in failed:
                click.echo(f"  - {scan_id}: {error}")
            raise SystemExit(1)
