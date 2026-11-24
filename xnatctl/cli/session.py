"""Session commands for xnatctl."""

from __future__ import annotations

import time
from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    resolve_workers_from_context,
    validate_local_path_option_cb,
)
from xnatctl.core.exceptions import (
    DownloadError,
    InputValidationError,
    ResourceNotFoundError,
)
from xnatctl.core.output import (
    OutputFormat,
    create_progress,
    print_error,
    print_json,
    print_success,
)
from xnatctl.core.validation import validate_local_path_component, validate_path_writable
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.models.progress import (
    TransferItemResult,
    TransferSummary,
    TransferVerification,
    VerificationReport,
    transfer_status,
)
from xnatctl.services.downloads import DownloadOutcome, DownloadService, ScanResult
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.sessions import SessionService
from xnatctl.services.zip_extract import extract_session_zips


def _echo_stderr(message: str) -> None:
    click.echo(message, err=True)


@click.group()
def session() -> None:
    """Manage XNAT sessions/experiments."""
    pass


@session.command("download")
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
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option(
    "--name",
    hidden=True,
    callback=validate_local_path_option_cb,
    is_eager=True,
    help="Output directory name (defaults to session ID)",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=None,
    show_default="1 (or profile)",
    help="Parallel download workers (1 = sequential single-ZIP, >1 = parallel per-scan)",
)
@click.option(
    "--resource",
    "-r",
    multiple=True,
    help="Resource types to include (repeatable). Omit for all scan resources.",
)
@click.option(
    "--exclude-resource",
    multiple=True,
    help="Resource types to exclude (repeatable). Cannot combine with --resource.",
)
@click.option(
    "--session-resources", is_flag=True, hidden=True, help="Include session-level resources"
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
def session_download(  # noqa: C901  # pre-existing; see pyproject
    ctx: Context,
    session_id: str,
    project: str | None,
    out: str,
    name: str | None,
    workers: int | None,
    resource: tuple[str, ...],
    exclude_resource: tuple[str, ...],
    session_resources: bool,
    extract: bool,
    keep_zips: bool,
    verify: bool,
    dry_run: bool,
) -> None:
    """Download session data.

    -E accepts an XNAT experiment ID (accession #) or a session label.
    When using a label, -P is required (or set default_project in your profile).

    By default, all scan resource types (DICOM, NII, SNAPSHOTS, etc.) are
    downloaded. Use -r to include specific types, or --exclude-resource to
    exclude specific types.

    \b
    Example:
        xnatctl session download -E XNAT_E00001
        xnatctl session download -E XNAT_E00001 --out ./data --workers 8
        xnatctl session download -E XNAT_E00001 -w 8 -r DICOM
        xnatctl session download -E XNAT_E00001 -w 8 -r DICOM -r NII
        xnatctl session download -E XNAT_E00001 -w 8 --exclude-resource SNAPSHOTS
        xnatctl session download -E XNAT_E00001 --out ./data --session-resources
        xnatctl session download -E XNAT_E00001 --out ./data --dry-run
        xnatctl session download -E XNAT_E00001 --verify
    """
    unzip = extract or keep_zips
    cleanup = extract and not keep_zips

    if resource and exclude_resource:
        raise click.UsageError("--resource and --exclude-resource are mutually exclusive")

    out_path = Path(out)

    if name is None:
        try:
            validate_local_path_component(session_id, "session_id (as the output directory name)")
        except InputValidationError as e:
            raise click.ClickException(str(e)) from e

    if not project:
        project = default_project_from_context(ctx)
    workers = resolve_workers_from_context(ctx, workers, default=1)

    if not out_path.exists():
        out_path.mkdir(parents=True, exist_ok=True)
    validate_path_writable(out_path)

    client = ctx.get_client()

    hierarchy = HierarchyService(client)
    session_ref = ExperimentRef(
        experiment=session_id,
        project_id=project,
        experiment_is_label=project is not None,
    )

    try:
        resolved = hierarchy.resolve_experiment(session_ref)
    except ResourceNotFoundError:
        if project:
            print_error(f"Session '{session_id}' not found in project '{project}'")
        else:
            print_error(f"Session not found: {session_id}")
        raise SystemExit(1) from None

    resolved_session_id = resolved.experiment_id
    session_project = resolved.project_id or project or ""
    subject = resolved.subject_id or resolved.subject_label

    if not subject:
        print_error(f"Could not determine subject for session: {session_id}")
        raise SystemExit(1)

    if dry_run:
        if ctx.output_format == OutputFormat.JSON:
            print_json(
                {
                    "operation": "download",
                    "dry_run": True,
                    "session_id": resolved_session_id,
                    "project": session_project,
                    "subject": subject,
                    "output_dir": str(out_path / (name or session_id)),
                    "workers": workers,
                    "resources": list(resource)
                    if resource
                    else list(exclude_resource)
                    if exclude_resource
                    else "all",
                    "resource_filter_mode": "include"
                    if resource
                    else "exclude"
                    if exclude_resource
                    else "all",
                    "session_resources": session_resources,
                }
            )
            return
        click.echo(f"[DRY-RUN] Would download session {session_id}", err=True)
        if resolved_session_id != session_id:
            click.echo(f"  Resolved ID: {resolved_session_id}", err=True)
        click.echo(f"  Project: {session_project}", err=True)
        click.echo(f"  Subject: {subject}", err=True)
        click.echo(f"  Output: {out_path / (name or session_id)}", err=True)
        click.echo(f"  Workers: {workers}", err=True)
        if resource:
            click.echo(f"  Resources: {', '.join(resource)}", err=True)
        elif exclude_resource:
            click.echo(f"  Exclude resources: {', '.join(exclude_resource)}", err=True)
        else:
            click.echo("  Resources: all", err=True)
        click.echo(f"  Session resources: {session_resources}", err=True)
        return

    session_dir = out_path / (name or session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    download_start = time.time()

    use_parallel = workers > 1 or resource or exclude_resource

    scan_ok: dict[str, bool] = {}
    scan_errors: dict[str, list[str]] = {}
    extra_items: list[TransferItemResult] = []
    session_zip_skipped: list[str] = []

    def _skipped_unsafe_entries() -> int:
        return (outcome.skipped_unsafe_entries if outcome is not None else 0) + len(
            session_zip_skipped
        )

    def _items_from_scan_ok() -> list[TransferItemResult]:
        return [
            TransferItemResult(
                id=scan_id,
                status="success" if ok else "failed",
                error="; ".join(scan_errors[scan_id]) if not ok else None,
            )
            for scan_id, ok in scan_ok.items()
        ]

    def _current_items() -> list[TransferItemResult]:
        return [*_items_from_scan_ok(), *extra_items]

    def _item_counts(all_items: list[TransferItemResult]) -> tuple[int, int]:
        succeeded = sum(1 for item in all_items if item.status == "success")
        failed = sum(1 for item in all_items if item.status == "failed")
        return succeeded, failed

    def _emit_failed_summary(exc: BaseException) -> None:
        if ctx.output_format != OutputFormat.JSON:
            return
        failure_items = [
            *_current_items(),
            TransferItemResult(id="session", status="failed", error=str(exc)),
        ]
        succeeded_count, failed_count = _item_counts(failure_items)
        TransferSummary(
            operation="download",
            session_id=resolved_session_id,
            project=session_project,
            output_dir=str(session_dir),
            scans=len(scan_ok) if use_parallel else None,
            files=outcome.files if outcome is not None else None,
            duration_seconds=round(time.time() - download_start, 3),
            status=transfer_status(succeeded=succeeded_count, failed=failed_count),
            items=failure_items,
            skipped_unsafe_entries=_skipped_unsafe_entries(),
        ).emit()

    outcome: DownloadOutcome | None = None
    if use_parallel:

        def on_scan_start(count: int) -> None:
            if ctx.quiet:
                return
            if count == 0:
                click.echo("No scans found in session", err=True)
            else:
                click.echo(f"Downloading {count} scans in parallel...", err=True)

        def on_scan_result(result: ScanResult) -> None:
            scan_ok[result.scan_id] = scan_ok.get(result.scan_id, True) and result.ok
            if result.ok:
                if not ctx.quiet:
                    status = f" ({result.message})" if result.message else ""
                    click.echo(f"  Scan {result.scan_id} done{status}", err=True)
            else:
                scan_errors.setdefault(result.scan_id, []).append(result.message)
                click.echo(f"  Scan {result.scan_id} FAILED: {result.message}", err=True)

        try:
            outcome = DownloadService(client).download_session_fast(
                session_project=session_project,
                subject=subject,
                resolved_session_id=resolved_session_id,
                session_dir=session_dir,
                workers=max(workers, 1),
                include_resources=resource,
                exclude_resources=exclude_resource,
                on_start=on_scan_start,
                on_scan_result=on_scan_result,
            )
        except Exception as exc:
            _emit_failed_summary(exc)
            raise
        if outcome.failed:
            click.echo(
                f"{len(outcome.failed)}/{outcome.succeeded + len(outcome.failed)} "
                "scan downloads failed",
                err=True,
            )
    else:
        try:
            with create_progress() as progress:
                task = progress.add_task("Downloading scans...", total=100)

                def on_scan_progress(written: int, total: int | None) -> None:
                    if total:
                        progress.update(task, completed=int(written / total * 100))

                DownloadService(client).download_session_archive(
                    session_project=session_project,
                    subject=subject,
                    resolved_session_id=resolved_session_id,
                    session_dir=session_dir,
                    progress_cb=on_scan_progress,
                )

                progress.update(task, completed=100, description="Scans downloaded")
        except Exception as exc:
            _emit_failed_summary(exc)
            raise
        extra_items.append(TransferItemResult(id="archive", status="success"))

    scans_count = len(scan_ok) if use_parallel else None
    succeeded_scan_ids = [scan_id for scan_id, ok in scan_ok.items() if ok]

    session_resource_zips: list[tuple[str, Path]] = []
    if session_resources:
        if not ctx.quiet:
            click.echo("Downloading session-level resources...", err=True)
        requested_resource_labels: list[str] = []
        listing_error: str | None = None
        if ctx.output_format == OutputFormat.JSON:
            try:
                requested_resource_labels = [
                    str(row["label"])
                    for row in SessionService(client).experiment_resource_rows(
                        resolved_session_id, project=session_project, subject=subject
                    )
                    if row.get("label")
                ]
            except Exception as exc:  # noqa: BLE001  # documented soft isolation -- result threaded through as listing_error, not swallowed (see comment above)
                listing_error = str(exc)
        session_resource_error: str | None = None
        try:
            DownloadService(client).download_session_level_resources(
                session_project=session_project,
                subject=subject,
                resolved_session_id=resolved_session_id,
                session_dir=session_dir,
                downloaded=session_resource_zips,
            )
            if not ctx.quiet:
                click.echo(
                    f"  Session resources downloaded ({len(session_resource_zips)})", err=True
                )
        except Exception as e:  # noqa: BLE001  # warned to stderr and folded into JSON report (see comment above)
            session_resource_error = str(e)
            if not ctx.quiet:
                click.echo(f"  Session resources: {e}", err=True)
        succeeded_resource_labels = {label for label, _zip in session_resource_zips}
        for label in requested_resource_labels:
            if label in succeeded_resource_labels:
                extra_items.append(TransferItemResult(id=f"resource:{label}", status="success"))
            else:
                extra_items.append(
                    TransferItemResult(
                        id=f"resource:{label}",
                        status="failed",
                        error=session_resource_error or "not downloaded",
                    )
                )
        if listing_error is not None:
            if session_resource_error is None:
                for label in succeeded_resource_labels:
                    extra_items.append(TransferItemResult(id=f"resource:{label}", status="success"))
            else:
                for label in succeeded_resource_labels:
                    extra_items.append(TransferItemResult(id=f"resource:{label}", status="success"))
                extra_items.append(
                    TransferItemResult(
                        id="resources", status="failed", error=session_resource_error
                    )
                )

    resource_zip_paths = [zip_path for _label, zip_path in session_resource_zips]
    try:
        if unzip:
            if verify and resource_zip_paths:
                non_resource_zips = [
                    p for p in session_dir.glob("*.zip") if p not in resource_zip_paths
                ]
                extract_session_zips(
                    session_dir,
                    cleanup=cleanup,
                    on_message=None if ctx.quiet else _echo_stderr,
                    zip_paths=non_resource_zips,
                    skipped_out=session_zip_skipped,
                )
            else:
                extract_session_zips(
                    session_dir,
                    cleanup=cleanup,
                    on_message=None if ctx.quiet else _echo_stderr,
                    skipped_out=session_zip_skipped,
                )
    except Exception as exc:
        _emit_failed_summary(exc)
        raise

    if outcome is not None and outcome.failed:
        if ctx.output_format == OutputFormat.JSON:
            all_items = _current_items()
            succeeded_count, failed_count = _item_counts(all_items)
            TransferSummary(
                operation="download",
                session_id=resolved_session_id,
                project=session_project,
                output_dir=str(session_dir),
                scans=scans_count,
                files=outcome.files,
                duration_seconds=round(time.time() - download_start, 3),
                status=transfer_status(succeeded=succeeded_count, failed=failed_count),
                items=all_items,
                skipped_unsafe_entries=_skipped_unsafe_entries(),
            ).emit()
            raise SystemExit(1)
        lost = ", ".join(scan_id for scan_id, _msg in outcome.failed)
        raise DownloadError(
            f"{len(outcome.failed)} of {outcome.succeeded + len(outcome.failed)} scans "
            f"failed to download ({lost}). The session directory is incomplete: "
            f"{session_dir}",
            session_id,
            {"failed_scans": dict(outcome.failed)},
        )

    verification: VerificationReport | None = None
    if verify:
        if not ctx.quiet:
            click.echo("Verifying downloaded files...", err=True)
        verify_local_root = session_dir if (use_parallel or unzip) else None
        verify_zip_paths: list[Path | tuple[Path, str]] = (
            [] if verify_local_root is not None else [session_dir / "scans.zip"]
        )
        verify_zip_paths.extend((zip_path, label) for label, zip_path in session_resource_zips)
        verify_scan_ids = list(dict.fromkeys(succeeded_scan_ids)) if use_parallel else None
        try:
            verification = DownloadService(client).verify_scan_downloads(
                session_id=session_id,
                project=session_project,
                subject=subject,
                scan_ids=verify_scan_ids,
                include_resources=resource,
                exclude_resources=exclude_resource,
                include_session_resources=session_resources,
                local_root=verify_local_root,
                zip_paths=verify_zip_paths,
            )
        except Exception as exc:
            _emit_failed_summary(exc)
            raise
        for path in verification.collisions:
            click.echo(f"  COLLISION: {path}", err=True)
        for path in verification.mismatched:
            click.echo(f"  MISMATCH: {path}", err=True)
        for path in verification.missing_local:
            click.echo(f"  MISSING: {path}", err=True)
        if verification.success and not ctx.quiet:
            print_success(f"Verified {verification.matched} files")
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
        try:
            if unzip and resource_zip_paths:
                extract_session_zips(
                    session_dir,
                    cleanup=cleanup,
                    on_message=None if ctx.quiet else _echo_stderr,
                    zip_paths=resource_zip_paths,
                    skipped_out=session_zip_skipped,
                )
        except Exception as exc:
            _emit_failed_summary(exc)
            raise

    files_transferred = outcome.files if outcome is not None else None

    if verification is not None and not verification.success:
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="download",
                session_id=resolved_session_id,
                project=session_project,
                output_dir=str(session_dir),
                scans=scans_count,
                files=files_transferred,
                duration_seconds=round(time.time() - download_start, 3),
                status="failed",
                items=_current_items(),
                verification=TransferVerification.from_report(verification),
                skipped_unsafe_entries=_skipped_unsafe_entries(),
            ).emit()
            raise SystemExit(1)
        if verification.mismatched or verification.missing_local or verification.collisions:
            raise DownloadError(
                f"Verification failed: {len(verification.mismatched)} mismatched, "
                f"{len(verification.missing_local)} missing, "
                f"{len(verification.collisions)} colliding file(s). Session directory: "
                f"{session_dir}",
                session_id,
                {
                    "mismatched": verification.mismatched,
                    "missing_local": verification.missing_local,
                    "collisions": verification.collisions,
                },
            )
        if verification.unverifiable:
            raise DownloadError(
                "Verification failed: the server provided no checksums for any "
                f"downloaded file ({len(verification.unverifiable)} unverifiable); "
                "nothing was verified.",
                session_id,
                {"unverifiable": verification.unverifiable},
            )
        raise DownloadError(
            "Verification failed: the server manifest listed none of the "
            f"{len(verification.missing_remote)} in-scope local file(s); "
            "nothing was verified.",
            session_id,
            {"missing_remote": verification.missing_remote},
        )

    if ctx.output_format == OutputFormat.JSON:
        all_items = _current_items()
        succeeded_count, failed_count = _item_counts(all_items)
        json_status = transfer_status(succeeded=succeeded_count, failed=failed_count)
        TransferSummary(
            operation="download",
            session_id=resolved_session_id,
            project=session_project,
            output_dir=str(session_dir),
            scans=scans_count,
            files=files_transferred,
            duration_seconds=round(time.time() - download_start, 3),
            status=json_status,
            items=all_items,
            verification=TransferVerification.from_report(verification)
            if verification is not None
            else None,
            skipped_unsafe_entries=_skipped_unsafe_entries(),
        ).emit()
        if json_status != "success":
            raise SystemExit(1)
        return

    if outcome is not None:
        print_success(
            f"Downloaded {outcome.succeeded} scans ({outcome.files} files) to: {session_dir}"
        )
    else:
        print_success(f"Downloaded session to: {session_dir}")
