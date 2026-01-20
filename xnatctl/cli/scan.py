"""Scan commands for xnatctl."""

from __future__ import annotations

from typing import List, Optional

import click

from xnatctl.cli.common import Context, global_options, require_auth, handle_errors, confirm_destructive, parallel_options
from xnatctl.core.output import print_output, print_error, print_success, OutputFormat


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
        scans.append({
            "id": r.get("ID", ""),
            "type": r.get("type", ""),
            "series_description": r.get("series_description", ""),
            "quality": r.get("quality", ""),
            "frames": r.get("frames", ""),
            "note": r.get("note", ""),
        })

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
    from xnatctl.core.validation import validate_session_id, validate_scan_id

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
    from xnatctl.core.validation import validate_session_id, validate_scan_ids_input

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
