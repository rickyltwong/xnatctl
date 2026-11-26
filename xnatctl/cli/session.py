"""Session commands for xnatctl."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
from xnatctl.core.logging import FILE_ONLY_ATTR
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

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)


def _echo_stderr(message: str) -> None:
    click.echo(message, err=True)


def _items_status(items: list[TransferItemResult]) -> Literal["success", "partial", "failed"]:
    succeeded = sum(1 for item in items if item.status == "success")
    failed = sum(1 for item in items if item.status == "failed")
    return transfer_status(succeeded=succeeded, failed=failed)


@dataclass
class _SessionDownload:
    """One ``session download`` run: the resolved target and every outcome recorded so far."""

    ctx: Context
    client: XNATClient
    session_id: str
    resolved_session_id: str
    session_project: str
    subject: str
    session_dir: Path
    started: float
    use_parallel: bool
    outcome: DownloadOutcome | None = None
    scan_ok: dict[str, bool] = field(default_factory=dict)
    scan_errors: dict[str, list[str]] = field(default_factory=dict)
    extra_items: list[TransferItemResult] = field(default_factory=list)
    resource_zips: list[tuple[str, Path]] = field(default_factory=list)
    zip_skipped: list[str] = field(default_factory=list)

    @property
    def resource_zip_paths(self) -> list[Path]:
        return [zip_path for _label, zip_path in self.resource_zips]

    def items(self) -> list[TransferItemResult]:
        scan_items = [
            TransferItemResult(
                id=scan_id,
                status="success" if ok else "failed",
                error="; ".join(self.scan_errors[scan_id]) if not ok else None,
            )
            for scan_id, ok in self.scan_ok.items()
        ]
        return [*scan_items, *self.extra_items]

    def emit_summary(
        self,
        items: list[TransferItemResult],
        status: Literal["success", "partial", "failed"],
        verification: VerificationReport | None = None,
    ) -> None:
        outcome = self.outcome
        TransferSummary(
            operation="download",
            session_id=self.resolved_session_id,
            project=self.session_project,
            output_dir=str(self.session_dir),
            scans=len(self.scan_ok) if self.use_parallel else None,
            files=outcome.files if outcome is not None else None,
            duration_seconds=round(time.time() - self.started, 3),
            status=status,
            items=items,
            verification=TransferVerification.from_report(verification)
            if verification is not None
            else None,
            skipped_unsafe_entries=(outcome.skipped_unsafe_entries if outcome is not None else 0)
            + len(self.zip_skipped),
        ).emit()

    @contextmanager
    def failure_summary(self) -> Iterator[None]:
        """Emit the JSON summary for whatever escapes the block, then re-raise it."""
        try:
            yield
        except Exception as exc:
            if self.ctx.output_format == OutputFormat.JSON:
                items = [
                    *self.items(),
                    TransferItemResult(id="session", status="failed", error=str(exc)),
                ]
                self.emit_summary(items, _items_status(items))
            raise


def _resolve_download_session(
    client: XNATClient, session_id: str, project: str | None
) -> tuple[str, str, str]:
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

    subject = resolved.subject_id or resolved.subject_label
    if not subject:
        print_error(f"Could not determine subject for session: {session_id}")
        raise SystemExit(1)
    return resolved.experiment_id, resolved.project_id or project or "", subject


def _print_download_plan(
    ctx: Context,
    *,
    session_id: str,
    resolved_session_id: str,
    session_project: str,
    subject: str,
    session_dir: Path,
    workers: int,
    resource: tuple[str, ...],
    exclude_resource: tuple[str, ...],
    session_resources: bool,
) -> None:
    if ctx.output_format == OutputFormat.JSON:
        print_json(
            {
                "operation": "download",
                "dry_run": True,
                "session_id": resolved_session_id,
                "project": session_project,
                "subject": subject,
                "output_dir": str(session_dir),
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
    click.echo(f"  Output: {session_dir}", err=True)
    click.echo(f"  Workers: {workers}", err=True)
    if resource:
        click.echo(f"  Resources: {', '.join(resource)}", err=True)
    elif exclude_resource:
        click.echo(f"  Exclude resources: {', '.join(exclude_resource)}", err=True)
    else:
        click.echo("  Resources: all", err=True)
    click.echo(f"  Session resources: {session_resources}", err=True)


def _download_scans_parallel(
    run: _SessionDownload,
    *,
    workers: int,
    resource: tuple[str, ...],
    exclude_resource: tuple[str, ...],
) -> None:
    ctx = run.ctx

    def on_scan_start(count: int) -> None:
        if ctx.quiet:
            return
        if count == 0:
            click.echo("No scans found in session", err=True)
        else:
            click.echo(f"Downloading {count} scans in parallel...", err=True)

    def on_scan_result(result: ScanResult) -> None:
        run.scan_ok[result.scan_id] = run.scan_ok.get(result.scan_id, True) and result.ok
        if result.ok:
            if not ctx.quiet:
                status = f" ({result.message})" if result.message else ""
                click.echo(f"  Scan {result.scan_id} done{status}", err=True)
        else:
            run.scan_errors.setdefault(result.scan_id, []).append(result.message)
            click.echo(f"  Scan {result.scan_id} FAILED: {result.message}", err=True)

    with run.failure_summary():
        outcome = DownloadService(run.client).download_session_fast(
            session_project=run.session_project,
            subject=run.subject,
            resolved_session_id=run.resolved_session_id,
            session_dir=run.session_dir,
            workers=max(workers, 1),
            include_resources=resource,
            exclude_resources=exclude_resource,
            on_start=on_scan_start,
            on_scan_result=on_scan_result,
        )
    run.outcome = outcome
    if outcome.failed:
        click.echo(
            f"{len(outcome.failed)}/{outcome.succeeded + len(outcome.failed)} "
            "scan downloads failed",
            err=True,
        )


def _download_scans_archive(run: _SessionDownload) -> None:
    with run.failure_summary(), create_progress() as progress:
        task = progress.add_task("Downloading scans...", total=100)

        def on_scan_progress(written: int, total: int | None) -> None:
            if total:
                progress.update(task, completed=int(written / total * 100))

        DownloadService(run.client).download_session_archive(
            session_project=run.session_project,
            subject=run.subject,
            resolved_session_id=run.resolved_session_id,
            session_dir=run.session_dir,
            progress_cb=on_scan_progress,
        )

        progress.update(task, completed=100, description="Scans downloaded")
    run.extra_items.append(TransferItemResult(id="archive", status="success"))


def _download_session_resources(run: _SessionDownload) -> None:
    ctx = run.ctx
    if not ctx.quiet:
        click.echo("Downloading session-level resources...", err=True)
    requested_labels: list[str] = []
    listing_error: str | None = None
    if ctx.output_format == OutputFormat.JSON:
        try:
            requested_labels = [
                str(row["label"])
                for row in SessionService(run.client).experiment_resource_rows(
                    run.resolved_session_id, project=run.session_project, subject=run.subject
                )
                if row.get("label")
            ]
        except Exception as exc:
            logger.exception("Listing session resources failed", extra={FILE_ONLY_ATTR: True})
            listing_error = str(exc)
    download_error: str | None = None
    try:
        DownloadService(run.client).download_session_level_resources(
            session_project=run.session_project,
            subject=run.subject,
            resolved_session_id=run.resolved_session_id,
            session_dir=run.session_dir,
            downloaded=run.resource_zips,
        )
        if not ctx.quiet:
            click.echo(f"  Session resources downloaded ({len(run.resource_zips)})", err=True)
    except Exception as e:
        logger.exception("Downloading session resources failed", extra={FILE_ONLY_ATTR: True})
        download_error = str(e)
        if not ctx.quiet:
            click.echo(f"  Session resources: {e}", err=True)
    run.extra_items.extend(
        _session_resource_items(requested_labels, run.resource_zips, listing_error, download_error)
    )


def _session_resource_items(
    requested_labels: list[str],
    downloaded: list[tuple[str, Path]],
    listing_error: str | None,
    download_error: str | None,
) -> list[TransferItemResult]:
    succeeded_labels = {label for label, _zip in downloaded}
    items: list[TransferItemResult] = []
    for label in requested_labels:
        if label in succeeded_labels:
            items.append(TransferItemResult(id=f"resource:{label}", status="success"))
        else:
            items.append(
                TransferItemResult(
                    id=f"resource:{label}",
                    status="failed",
                    error=download_error or "not downloaded",
                )
            )
    if listing_error is not None:
        for label in succeeded_labels:
            items.append(TransferItemResult(id=f"resource:{label}", status="success"))
        if download_error is not None:
            items.append(TransferItemResult(id="resources", status="failed", error=download_error))
    return items


def _extract_before_verify(run: _SessionDownload, *, cleanup: bool, verify: bool) -> None:
    resource_zip_paths = run.resource_zip_paths
    with run.failure_summary():
        if verify and resource_zip_paths:
            non_resource_zips = [
                p for p in run.session_dir.glob("*.zip") if p not in resource_zip_paths
            ]
            extract_session_zips(
                run.session_dir,
                cleanup=cleanup,
                on_message=None if run.ctx.quiet else _echo_stderr,
                zip_paths=non_resource_zips,
                skipped_out=run.zip_skipped,
            )
        else:
            extract_session_zips(
                run.session_dir,
                cleanup=cleanup,
                on_message=None if run.ctx.quiet else _echo_stderr,
                skipped_out=run.zip_skipped,
            )


def _raise_for_failed_scans(run: _SessionDownload) -> None:
    outcome = run.outcome
    if outcome is None or not outcome.failed:
        return
    if run.ctx.output_format == OutputFormat.JSON:
        items = run.items()
        run.emit_summary(items, _items_status(items))
        raise SystemExit(1)
    lost = ", ".join(scan_id for scan_id, _msg in outcome.failed)
    raise DownloadError(
        f"{len(outcome.failed)} of {outcome.succeeded + len(outcome.failed)} scans "
        f"failed to download ({lost}). The session directory is incomplete: "
        f"{run.session_dir}",
        run.session_id,
        {"failed_scans": dict(outcome.failed)},
    )


def _verify_download(
    run: _SessionDownload,
    *,
    unzip: bool,
    cleanup: bool,
    resource: tuple[str, ...],
    exclude_resource: tuple[str, ...],
    session_resources: bool,
) -> VerificationReport:
    ctx = run.ctx
    if not ctx.quiet:
        click.echo("Verifying downloaded files...", err=True)
    verify_local_root = run.session_dir if (run.use_parallel or unzip) else None
    verify_zip_paths: list[Path | tuple[Path, str]] = (
        [] if verify_local_root is not None else [run.session_dir / "scans.zip"]
    )
    verify_zip_paths.extend((zip_path, label) for label, zip_path in run.resource_zips)
    succeeded_scan_ids = [scan_id for scan_id, ok in run.scan_ok.items() if ok]
    verify_scan_ids = list(dict.fromkeys(succeeded_scan_ids)) if run.use_parallel else None
    with run.failure_summary():
        verification = DownloadService(run.client).verify_scan_downloads(
            session_id=run.session_id,
            project=run.session_project,
            subject=run.subject,
            scan_ids=verify_scan_ids,
            include_resources=resource,
            exclude_resources=exclude_resource,
            include_session_resources=session_resources,
            local_root=verify_local_root,
            zip_paths=verify_zip_paths,
        )
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
    resource_zip_paths = run.resource_zip_paths
    if unzip and resource_zip_paths:
        with run.failure_summary():
            extract_session_zips(
                run.session_dir,
                cleanup=cleanup,
                on_message=None if ctx.quiet else _echo_stderr,
                zip_paths=resource_zip_paths,
                skipped_out=run.zip_skipped,
            )
    return verification


def _raise_for_failed_verification(run: _SessionDownload, verification: VerificationReport) -> None:
    if verification.success:
        return
    if run.ctx.output_format == OutputFormat.JSON:
        run.emit_summary(run.items(), "failed", verification)
        raise SystemExit(1)
    if verification.mismatched or verification.missing_local or verification.collisions:
        raise DownloadError(
            f"Verification failed: {len(verification.mismatched)} mismatched, "
            f"{len(verification.missing_local)} missing, "
            f"{len(verification.collisions)} colliding file(s). Session directory: "
            f"{run.session_dir}",
            run.session_id,
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
            run.session_id,
            {"unverifiable": verification.unverifiable},
        )
    raise DownloadError(
        "Verification failed: the server manifest listed none of the "
        f"{len(verification.missing_remote)} in-scope local file(s); "
        "nothing was verified.",
        run.session_id,
        {"missing_remote": verification.missing_remote},
    )


def _report_download(run: _SessionDownload, verification: VerificationReport | None) -> None:
    if run.ctx.output_format == OutputFormat.JSON:
        items = run.items()
        status = _items_status(items)
        run.emit_summary(items, status, verification)
        if status != "success":
            raise SystemExit(1)
        return

    if run.outcome is not None:
        print_success(
            f"Downloaded {run.outcome.succeeded} scans ({run.outcome.files} files) "
            f"to: {run.session_dir}"
        )
    else:
        print_success(f"Downloaded session to: {run.session_dir}")


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
def session_download(
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
    resolved_session_id, session_project, subject = _resolve_download_session(
        client, session_id, project
    )
    session_dir = out_path / (name or session_id)

    if dry_run:
        _print_download_plan(
            ctx,
            session_id=session_id,
            resolved_session_id=resolved_session_id,
            session_project=session_project,
            subject=subject,
            session_dir=session_dir,
            workers=workers,
            resource=resource,
            exclude_resource=exclude_resource,
            session_resources=session_resources,
        )
        return

    session_dir.mkdir(parents=True, exist_ok=True)
    run = _SessionDownload(
        ctx=ctx,
        client=client,
        session_id=session_id,
        resolved_session_id=resolved_session_id,
        session_project=session_project,
        subject=subject,
        session_dir=session_dir,
        started=time.time(),
        use_parallel=bool(workers > 1 or resource or exclude_resource),
    )

    if run.use_parallel:
        _download_scans_parallel(
            run, workers=workers, resource=resource, exclude_resource=exclude_resource
        )
    else:
        _download_scans_archive(run)

    if session_resources:
        _download_session_resources(run)

    if unzip:
        _extract_before_verify(run, cleanup=cleanup, verify=verify)

    _raise_for_failed_scans(run)

    verification: VerificationReport | None = None
    if verify:
        verification = _verify_download(
            run,
            unzip=unzip,
            cleanup=cleanup,
            resource=resource,
            exclude_resource=exclude_resource,
            session_resources=session_resources,
        )
        _raise_for_failed_verification(run, verification)

    _report_download(run, verification)
