"""``scan download`` -- per-scan resource download (registered on the scan group)."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Literal

import click

from xnatctl.cli.common import (
    Context,
    _make_forwarding_alias_cb,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    validate_local_path_option_cb,
)
from xnatctl.cli.scan import scan
from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError
from xnatctl.core.output import OutputFormat, print_error, print_json, print_success
from xnatctl.core.validation import (
    validate_local_path_component,
    validate_scan_ids_input,
    validate_session_id,
)
from xnatctl.models.progress import (
    DownloadProgress,
    OperationPhase,
    TransferItemResult,
    TransferSummary,
    TransferVerification,
    VerificationReport,
    transfer_status,
)


@scan.command("download")
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
@click.option(
    "--subject",
    "-S",
    help="Subject ID/label (narrows experiment lookup, requires -P)",
)
@click.option(
    "--scans",
    multiple=True,
    # Eager so it is processed before the deprecated -s alias, which merges
    # into this option's value (see _make_forwarding_alias_cb).
    is_eager=True,
    help="Scan IDs: repeatable (--scans 1 --scans 2), comma-separated "
    "(--scans 1,2,3), or '*' for all.",
)
@click.option(
    "-s",
    "legacy_scans_s",
    multiple=True,
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("-s", "scans"),
)
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option(
    "--name",
    hidden=True,
    callback=validate_local_path_option_cb,
    is_eager=True,
    help="Output directory name (defaults to experiment value)",
)
@click.option(
    "--resource",
    "-r",
    multiple=True,
    help="Resource type to download (e.g., DICOM). Omit for all resources.",
)
@click.option(
    "--extract/--no-extract", default=False, help="Extract ZIPs and remove archives after download"
)
@click.option(
    "--keep-zips", is_flag=True, hidden=True, help="With --extract, keep ZIP files after extraction"
)
@click.option(
    "--verify",
    is_flag=True,
    help=(
        "Verify downloaded files against server MD5 checksums after download; "
        "fails if the server has no checksums for anything downloaded"
    ),
)
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@global_options
@handle_errors
@require_auth
def scan_download(  # noqa: C901  # pre-existing; see pyproject
    ctx: Context,
    session_id: str,
    project: str | None,
    subject: str | None,
    scans: tuple[str, ...],
    out: str,
    name: str | None,
    resource: tuple[str, ...],
    extract: bool,
    keep_zips: bool,
    verify: bool,
    dry_run: bool,
) -> None:
    """Download scans from an image session.

    Downloads all specified scans in a single request using XNAT's batch download
    feature. Output is saved to {out}/{experiment}/scans.zip.

    The output directory defaults to the value passed to -E/--experiment.
    Override it with --name.

    Use --resource to download a specific resource type (DICOM, NIFTI, etc).
    Only one --resource value is supported per invocation.
    Omit --resource to download all resources for the scans.

    \b
    Examples:
        xnatctl scan download -E XNAT_E00001 --scans 1
        xnatctl scan download -E XNAT_E00001 --scans 1 --out ./data
        xnatctl scan download -P PROJECT -E SESSION_LABEL --scans 1,2,3 --out ./data
        xnatctl scan download -P PROJECT -E SESSION --scans '*' --out ./data
        xnatctl scan download -E XNAT_E00001 --scans 1 --verify
    """
    # `--scans` is not required=True at the Click level: the deprecated
    # `-s` alias forwards into this same param via a callback that runs
    # after Click's own required-check would already have fired (Click
    # checks each option's own parsed value, not ctx.params), so
    # required=True here would reject a bare `-s`. Enforced by hand instead.
    if not scans:
        raise click.UsageError("Missing option '--scans'.")

    # Map extract/keep_zips to internal unzip/cleanup
    unzip = extract or keep_zips
    cleanup = extract and not keep_zips

    session_id = validate_session_id(session_id)
    scan_ids_input = validate_scan_ids_input(",".join(scans))
    output_dir = Path(out)
    client = ctx.get_client()

    if not project:
        project = default_project_from_context(ctx)

    # `--name` (when given) is validated by validate_local_path_option_cb, an
    # eager Click callback that runs at argument-parsing time -- before
    # @require_auth -- so a malformed --name fails without needing a valid
    # session. Without --name, the output directory falls back to the raw
    # session_id (see below); that fallback isn't known until here (it
    # depends on -E), so it's still validated in the command body, and goes
    # through validate_session_id's URL-safety check rather than a
    # local-filesystem check, so a label like "CON" (a legal session ID, but
    # a reserved Windows device name) would otherwise reach the filesystem
    # unvalidated. Mirrors session download's own fallback.
    if name is None:
        try:
            validate_local_path_component(session_id, "session_id (as the output directory name)")
        except InputValidationError as e:
            raise click.ClickException(str(e)) from e

    use_all_keyword = scan_ids_input is None
    if scan_ids_input is None:
        scan_ids = ["ALL"]
    else:
        scan_ids = scan_ids_input

    # Validate resource option (only one value supported)
    if len(resource) > 1:
        raise click.ClickException(
            "Only one --resource value is supported per invocation. "
            "Use session download with -r for multi-resource filtering."
        )
    resource_filter: str | None = resource[0] if resource else None

    if dry_run:
        resource_desc = resource[0] if resource else "all resources"
        if ctx.output_format == OutputFormat.JSON:
            print_json(
                {
                    "operation": "download",
                    "dry_run": True,
                    "session_id": session_id,
                    "project": project,
                    "scans": "all" if use_all_keyword else scan_ids,
                    "resource": resource_desc,
                    "output_dir": str(output_dir / (name or session_id)),
                }
            )
            return
        scan_desc = "all scans" if use_all_keyword else f"{len(scan_ids)} scans"
        click.echo(
            f"[DRY-RUN] Would download {scan_desc} ({resource_desc}) "
            f"to {output_dir}/{name or session_id}/",
            err=True,
        )
        if not use_all_keyword:
            for sid in scan_ids:
                click.echo(f"  - Scan {sid}", err=True)
        return

    session_output = output_dir / (name or session_id)
    session_output.mkdir(parents=True, exist_ok=True)
    # Deferred: tests patch xnatctl.services.downloads.DownloadService to
    # intercept the lookup; a module-scope import would bind the real class
    # before the patch runs (see tests/test_cli_scan.py).
    from xnatctl.services.downloads import DownloadService

    service = DownloadService(client)

    def progress_cb(progress: DownloadProgress) -> None:
        if progress.phase == OperationPhase.DOWNLOADING and not ctx.quiet and progress.total_bytes:
            pct = progress.bytes_received * 100 // progress.total_bytes
            mb = progress.bytes_received / (1024 * 1024)
            click.echo(f"\r  Downloading: {pct}% ({mb:.1f} MB)", nl=False, err=True)

    download_start = time.time()

    # One ZIP request covers the whole spec (XNAT's comma-separated batch
    # download) -- it succeeds or fails atomically, so it is exactly one
    # item, identified by what was asked for, not by the individual IDs in
    # it (there is no per-scan outcome to report).
    spec_id = "ALL" if use_all_keyword else ",".join(scan_ids)
    requested_scan_count = None if use_all_keyword else len(scan_ids)

    try:
        summary = service.download_scans(
            session_id=session_id,
            scan_ids=scan_ids,
            output_dir=session_output,
            project=project,
            subject=subject,
            resource=resource_filter,
            zip_filename="scans.zip",
            extract=unzip,
            cleanup=cleanup,
            progress_callback=progress_cb if not ctx.quiet else None,
        )
    except (ValueError, ResourceNotFoundError) as e:
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="download",
                session_id=session_id,
                project=project,
                output_dir=str(session_output),
                scans=requested_scan_count,
                files=None,
                bytes=None,
                duration_seconds=round(time.time() - download_start, 3),
                status="failed",
                items=[TransferItemResult(id=spec_id, status="failed", error=str(e))],
            ).emit()
        print_error(str(e))
        raise SystemExit(1) from None

    if not ctx.quiet:
        # Terminate the \r progress line above, which also lives on stderr.
        click.echo(err=True)

    download_succeeded = summary.success
    verification: VerificationReport | None = None
    verification_error: str | None = None
    if verify and summary.success:
        if not ctx.quiet:
            click.echo("Verifying downloaded files...", err=True)
        verify_local_root = session_output / "scans" if unzip else None
        verify_zip_paths = () if verify_local_root is not None else (session_output / "scans.zip",)
        try:
            verification = service.verify_scan_downloads(
                session_id=session_id,
                project=project,
                subject=subject,
                scan_ids=None if use_all_keyword else scan_ids,
                resource_filter=resource_filter,
                local_root=verify_local_root,
                # `_safe_extract_zip` preserves a raw ZIP member's own
                # session/experiment-label wrapper unstripped under this
                # extraction root -- always wrapped, never session download's
                # unwrapped shape.
                local_root_wrapped=True,
                zip_paths=verify_zip_paths,
            )
        except Exception as exc:  # noqa: BLE001  # emits JSON failure summary before re-raising unchanged
            if ctx.output_format == OutputFormat.JSON:
                TransferSummary(
                    operation="download",
                    session_id=session_id,
                    project=project,
                    output_dir=summary.output_path,
                    scans=requested_scan_count,
                    # The download itself succeeded (this block only runs
                    # when it did); only verification blew up.
                    files=summary.total_files,
                    bytes=round(summary.total_size_mb * 1024 * 1024),
                    duration_seconds=round(time.time() - download_start, 3),
                    status="failed",
                    items=[TransferItemResult(id=spec_id, status="failed", error=str(exc))],
                    skipped_unsafe_entries=summary.skipped_unsafe_entries,
                ).emit()
            raise
        if not verification.success:
            if verification.mismatched or verification.missing_local or verification.collisions:
                verification_error = (
                    f"{len(verification.mismatched)} mismatched, "
                    f"{len(verification.missing_local)} missing, "
                    f"{len(verification.collisions)} colliding file(s)"
                )
            elif verification.unverifiable:
                # Nothing mismatched or missing, but nothing matched either --
                # every checked file landed in unverifiable, so the server had
                # no checksums on record for anything the command downloaded.
                verification_error = (
                    "the server provided no checksums for any downloaded file "
                    f"({len(verification.unverifiable)} unverifiable); nothing was verified"
                )
            else:
                # The manifest listed nothing at all for a scope that has
                # files on disk -- nothing was verified.
                verification_error = (
                    "the server manifest listed none of the "
                    f"{len(verification.missing_remote)} in-scope local file(s); "
                    "nothing was verified"
                )
        summary = dataclasses.replace(
            summary,
            verification=verification,
            success=summary.success and verification.success,
            errors=[*summary.errors, verification_error] if verification_error else summary.errors,
        )
        for path in verification.collisions:
            click.echo(f"  COLLISION: {path}", err=True)
        for path in verification.mismatched:
            click.echo(f"  MISMATCH: {path}", err=True)
        for path in verification.missing_local:
            click.echo(f"  MISSING: {path}", err=True)

    if ctx.output_format == OutputFormat.JSON:
        item_status: Literal["success", "failed"] = "success" if summary.success else "failed"
        item_error = None if summary.success else (summary.errors[0] if summary.errors else None)
        TransferSummary(
            operation="download",
            session_id=session_id,
            project=project,
            output_dir=summary.output_path,
            scans=requested_scan_count,
            # Gated on the download itself succeeding, not the (possibly
            # verify-combined) final `summary.success`: files that landed
            # and then failed checksum verification were still transferred.
            files=summary.total_files if download_succeeded else None,
            bytes=round(summary.total_size_mb * 1024 * 1024) if download_succeeded else None,
            duration_seconds=round(time.time() - download_start, 3),
            status=transfer_status(
                succeeded=1 if summary.success else 0,
                failed=0 if summary.success else 1,
                success=summary.success,
            ),
            items=[TransferItemResult(id=spec_id, status=item_status, error=item_error)],
            verification=TransferVerification.from_report(verification)
            if verification is not None
            else None,
            skipped_unsafe_entries=summary.skipped_unsafe_entries,
        ).emit()
    else:
        if summary.success:
            if unzip and not cleanup:
                if len(resource) > 1:
                    kept_zip_suffix = f" (kept {len(resource)} ZIP files)"
                else:
                    zip_name = f"scans_{resource[0]}.zip" if resource else "scans.zip"
                    kept_zip_suffix = f" (kept {session_output / zip_name})"
            else:
                kept_zip_suffix = ""
            print_success(
                f"Downloaded scans ({summary.total_size_mb:.1f} MB) to {summary.output_path}{kept_zip_suffix}"
            )
            if verification is not None and not ctx.quiet:
                click.echo(f"  Verified {verification.matched} files", err=True)
                if verification.unverifiable:
                    click.echo(
                        f"  {len(verification.unverifiable)} file(s) have no server "
                        "checksum and were not verified",
                        err=True,
                    )
                if verification.missing_remote:
                    click.echo(
                        f"  {len(verification.missing_remote)} local file(s) absent "
                        "from the server manifest were not verified",
                        err=True,
                    )
        elif download_succeeded:
            # The download itself succeeded; only verification failed -- say
            # so, rather than the misleading "Download failed".
            print_error(f"Verification failed: {verification_error}")
        else:
            print_error(
                f"Download failed: {summary.errors[0] if summary.errors else 'Unknown error'}"
            )

    # Format-independent failure exit: -o json must also return nonzero.
    if not summary.success:
        raise SystemExit(1)
