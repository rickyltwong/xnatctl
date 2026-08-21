"""Scan commands for xnatctl."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    _make_alias_cb,
    _make_noop_cb,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    parallel_options,
    require_auth,
    resolve_workers_from_context,
)
from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.core.output import (
    OutputFormat,
    err_console,
    print_error,
    print_json,
    print_output,
    print_success,
)
from xnatctl.models.hierarchy import ExperimentRef, ScanRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.resources import ResourceService
from xnatctl.services.scans import ScanService


def _inspect_experiment(
    hierarchy: HierarchyService, experiment_ref: ExperimentRef
) -> tuple[ExperimentRef, str | None]:
    """Inspect the parent experiment and derive canonical ID, subject and xsiType.

    Nested scan endpoints are most reliable when addressed with the canonical
    experiment ID. This helper also extracts the experiment xsiType so scan list
    queries can request the correct non-imaging scan subtype when needed.

    The owning subject is carried forward too, because XNAT does not route
    sub-resource suffixes on ``/data/projects/{P}/experiments/{E}``: it answers
    ``/scans`` -- or any other suffix -- with the parent experiment document
    instead of the sub-resource. Inserting the subject segment yields
    ``/data/projects/{P}/subjects/{S}/experiments/{E}/scans``, which routes
    correctly and still enforces the project ACL.

    Args:
        hierarchy: Hierarchy service.
        experiment_ref: Parent experiment reference.

    Returns:
        Tuple of (canonical experiment ref, experiment xsiType).
    """
    try:
        data = hierarchy.get_experiment_json(experiment_ref)
    except ResourceNotFoundError:
        return experiment_ref, None
    if not isinstance(data, dict):
        return experiment_ref, None

    resolved_id: str | None = None
    resolved_subject: str | None = None
    session_xsi: str | None = None

    def _looks_like_experiment(fields: dict[str, Any]) -> bool:
        return any(
            key in fields
            for key in (
                "project",
                "subject_ID",
                "subject_label",
                "date",
                "xsiType",
            )
        )

    item = hierarchy.extract_first_item(data)
    if item is not None:
        fields, meta = item
        session_xsi = str(fields.get("xsiType") or meta.get("xsi:type") or "") or None
        if _looks_like_experiment(fields) or "xsi:type" in meta:
            resolved_id = str(fields.get("ID") or fields.get("id") or "") or None
            resolved_subject = str(fields.get("subject_ID") or "") or None
    else:
        rows = hierarchy.extract_rows(data)
        if rows:
            session_xsi = str(rows[0].get("xsiType") or "") or None
            if _looks_like_experiment(rows[0]):
                resolved_id = str(rows[0].get("ID") or "") or None
                resolved_subject = str(rows[0].get("subject_ID") or "") or None

    # Carry project/subject scope forward so nested scan/resource calls stay
    # on project-scoped URLs (respects project ACLs, matches old behavior).
    # Use the resolved accession ID as the experiment with experiment_is_label=False.
    subject = experiment_ref.subject
    subject_is_label = experiment_ref.subject_is_label
    # The subject segment is what makes the nested suffix route at all (see the
    # docstring), but XNAT only accepts it under a project.
    if experiment_ref.project_id and not subject and resolved_subject:
        subject = resolved_subject
        subject_is_label = False

    if resolved_id or subject != experiment_ref.subject:
        canonical_ref = ExperimentRef(
            experiment=resolved_id or experiment_ref.experiment,
            project_id=experiment_ref.project_id,
            subject=subject,
            experiment_is_label=(False if resolved_id else experiment_ref.experiment_is_label),
            subject_is_label=subject_is_label,
        )
    else:
        canonical_ref = experiment_ref
    return canonical_ref, session_xsi


def _require_scan_addressable(ref: ExperimentRef) -> None:
    """Refuse to issue a scan request that XNAT would apply to the experiment.

    ``HierarchyService.routable_scan_parent`` rewrites a project-scoped
    experiment *ID* to the flat form so its ``/scans`` suffix routes. A genuine
    *label* has no such escape: it cannot drop the project, so the URL stays on
    the prefix XNAT answers with the experiment document. Failing here beats
    reporting an empty scan list, or aiming a DELETE at the session.

    Reached only when inspection could not resolve the label to an accession
    ID -- on the happy path ``_inspect_experiment`` has already done so.
    """
    # Unroutable exactly when the builder could not rewrite it to a form
    # that addresses a scan.
    if ref.project_id and not ref.subject and HierarchyService.routable_scan_parent(ref) == ref:
        raise click.ClickException(
            f"Could not resolve experiment '{ref.experiment}' in project "
            f"'{ref.project_id}' to an accession ID, and scans cannot be "
            "addressed by experiment label alone. Retry with -S/--subject, or "
            "pass the accession ID to -E."
        )


def _build_experiment_ref(
    project: str | None, subject: str | None, session_id: str
) -> ExperimentRef:
    """Build an experiment reference for scan operations.

    Args:
        project: Project ID (optional).
        subject: Subject ID/label (optional, requires project).
        session_id: Experiment ID or label.

    Returns:
        Experiment reference.

    Raises:
        click.ClickException: If subject is given without a resolved project.
    """
    if subject and not project:
        raise click.ClickException(
            "-S/--subject requires -P/--project (or default_project in profile)"
        )
    return ExperimentRef(
        experiment=session_id,
        project_id=project,
        subject=subject,
        experiment_is_label=project is not None,
        subject_is_label=subject is not None,
    )


@click.group()
def scan() -> None:
    """Manage XNAT scans."""
    pass


@scan.command("list")
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
@global_options
@handle_errors
@require_auth
def scan_list(ctx: Context, session_id: str, project: str | None, subject: str | None) -> None:
    """List scans in a session.

    \b
    Example:
        xnatctl scan list -E XNAT_E00001
        xnatctl scan list -E XNAT_E00001 -o json
        xnatctl scan list -E XNAT_E00001 -q  # IDs only
        xnatctl scan list -E SESSION_LABEL -P MYPROJ
        xnatctl scan list -P MYPROJ -S SUB001 -E SESSION_LABEL
    """
    from xnatctl.core.validation import validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)

    results = hierarchy.list_scan_rows(experiment_ref, session_xsi)

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
@click.argument("scan_id")
@global_options
@handle_errors
@require_auth
def scan_show(
    ctx: Context, session_id: str, project: str | None, subject: str | None, scan_id: str
) -> None:
    """Show scan details.

    \b
    Example:
        xnatctl scan show -E XNAT_E00001 1
        xnatctl scan show -E SESSION_LABEL 1 -P MYPROJ
        xnatctl scan show -P MYPROJ -S SUB001 -E SESSION_LABEL 1
    """
    from xnatctl.core.validation import validate_scan_id, validate_session_id

    session_id = validate_session_id(session_id)
    scan_id = validate_scan_id(scan_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, _session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)
    scan_ref = ScanRef(experiment=experiment_ref, scan_id=scan_id)
    scan_data = ScanService(client).get_scan_document(scan_ref)

    if not scan_data:
        print_error(f"Scan not found: {scan_id}")
        raise SystemExit(1)

    # Get resources
    try:
        resources = ResourceService(client).list_rows(scan_ref)
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
@click.option("--scans", "-s", required=True, help="Scan IDs (comma-separated or '*' for all)")
@confirm_destructive("Delete these scans?")
@parallel_options
@global_options
@handle_errors
@require_auth
def scan_delete(
    ctx: Context,
    session_id: str,
    project: str | None,
    subject: str | None,
    scans: str,
    dry_run: bool,
    workers: int | None,
) -> None:
    """Delete scans from a session.

    \b
    Example:
        xnatctl scan delete -E XNAT_E00001 --scans 1,2,3
        xnatctl scan delete -E XNAT_E00001 --scans '*'  # Delete all
        xnatctl scan delete -E XNAT_E00001 --scans 1,2 --dry-run
        xnatctl scan delete -E SESSION_LABEL --scans 1,2,3 -P MYPROJ
    """
    from concurrent.futures import as_completed

    from xnatctl.core.validation import validate_scan_ids_input, validate_session_id

    session_id = validate_session_id(session_id)
    scan_ids = validate_scan_ids_input(scans)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)

    # If wildcard, get all scan IDs
    if scan_ids is None:
        results = hierarchy.list_scan_rows(experiment_ref, session_xsi)
        scan_ids = [r.get("ID", "") for r in results if r.get("ID")]

    if not scan_ids:
        print_error("No scans to delete")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would delete {len(scan_ids)} scans:", err=True)
        for sid in scan_ids:
            click.echo(f"  - {sid}", err=True)
        return

    workers = resolve_workers_from_context(ctx, workers)

    deleted = []
    failed = []
    scan_svc = ScanService(client)

    def delete_scan(scan_id: str) -> tuple[str, bool, str]:
        """Delete a scan and return status and error message."""
        try:
            resp = scan_svc.delete_scan_ref(ScanRef(experiment=experiment_ref, scan_id=scan_id))
            return scan_id, resp.status_code in (200, 204), ""
        except Exception as e:
            return scan_id, False, str(e)

    if workers > 1 and len(scan_ids) > 1:
        with cancellable_pool(min(workers, len(scan_ids))) as (executor, _token):
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
        noun = "scan" if len(deleted) == 1 else "scans"
        # Use highlight=False so Rich does not turn the integer into a
        # styled token; tests assert on the raw "Deleted N scan(s)" text.
        # Stderr: status commentary, not data.
        err_console.print(f"[green]✓[/green] Deleted {len(deleted)} {noun}", highlight=False)

    if failed:
        noun = "scan" if len(failed) == 1 else "scans"
        print_error(f"Failed to delete {len(failed)} {noun}:")
        for scan_id, error in failed:
            click.echo(f"  - {scan_id}: {error}", err=True)
        raise SystemExit(1)


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
@click.option("--scans", "-s", required=True, help="Scan IDs (comma-separated or '*' for all)")
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option("--name", hidden=True, help="Output directory name (defaults to experiment value)")
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
    "--unzip",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("--unzip", "extract", True),
)
@click.option(
    "--no-unzip",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("--no-unzip", "extract", False),
)
@click.option(
    "--cleanup",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_noop_cb("--cleanup"),
    help="Deprecated: noop",
)
@click.option(
    "--no-cleanup",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("--no-cleanup", "keep_zips", True),
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
    scans: str,
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
        xnatctl scan download -E XNAT_E00001 -s 1
        xnatctl scan download -E XNAT_E00001 -s 1 --out ./data
        xnatctl scan download -P PROJECT -E SESSION_LABEL -s 1,2,3 --out ./data
        xnatctl scan download -P PROJECT -E SESSION -s '*' --out ./data
        xnatctl scan download -E XNAT_E00001 -s 1 --verify
    """
    import dataclasses

    from xnatctl.core.validation import validate_scan_ids_input, validate_session_id
    from xnatctl.models.progress import DownloadProgress, OperationPhase, VerificationReport
    from xnatctl.services.downloads import DownloadService

    # Map extract/keep_zips to internal unzip/cleanup
    unzip = extract or keep_zips
    cleanup = extract and not keep_zips

    session_id = validate_session_id(session_id)
    scan_ids_input = validate_scan_ids_input(scans)
    output_dir = Path(out)
    client = ctx.get_client()

    if not project:
        project = default_project_from_context(ctx)

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

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
        scan_desc = "all scans" if use_all_keyword else f"{len(scan_ids)} scans"
        resource_desc = resource[0] if resource else "all resources"
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
    service = DownloadService(client)

    def progress_cb(progress: DownloadProgress) -> None:
        if progress.phase == OperationPhase.DOWNLOADING and not ctx.quiet and progress.total_bytes:
            pct = progress.bytes_received * 100 // progress.total_bytes
            mb = progress.bytes_received / (1024 * 1024)
            click.echo(f"\r  Downloading: {pct}% ({mb:.1f} MB)", nl=False, err=True)

    from xnatctl.core.exceptions import ResourceNotFoundError

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
        if not verification.success:
            if verification.mismatched or verification.missing_local or verification.collisions:
                verification_error = (
                    f"{len(verification.mismatched)} mismatched, "
                    f"{len(verification.missing_local)} missing, "
                    f"{len(verification.collisions)} colliding file(s)"
                )
            else:
                # Nothing mismatched or missing, but nothing matched either --
                # every checked file landed in unverifiable, so the server had
                # no checksums on record for anything the command downloaded.
                verification_error = (
                    "the server provided no checksums for any downloaded file "
                    f"({len(verification.unverifiable)} unverifiable); nothing was verified"
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
        print_json(
            {
                "session_id": session_id,
                "output_path": summary.output_path,
                "success": summary.success,
                "total_size_mb": round(summary.total_size_mb, 2),
                "errors": summary.errors,
                "verification": None
                if verification is None
                else {
                    "matched": verification.matched,
                    "mismatched": verification.mismatched,
                    "missing_local": verification.missing_local,
                    "missing_remote": verification.missing_remote,
                    "unverifiable": verification.unverifiable,
                    "collisions": verification.collisions,
                },
            }
        )
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
