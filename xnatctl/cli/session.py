"""Session commands for xnatctl."""

from __future__ import annotations

import time
from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    _make_alias_cb,
    _make_noop_cb,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
    resolve_workers_from_context,
    validate_local_path_option_cb,
)
from xnatctl.core.exceptions import DownloadError, InputValidationError, ResourceNotFoundError
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_json,
    print_output,
    print_success,
)
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
    """Render a service progress line to stderr, unchanged."""
    click.echo(message, err=True)


@click.group()
def session() -> None:
    """Manage XNAT sessions/experiments."""
    pass


@session.command("list")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", help="Filter by subject")
@click.option(
    "--modality", type=click.Choice(["MR", "PET", "CT", "EEG"]), help="Filter by modality"
)
@global_options
@handle_errors
@require_auth
def session_list(
    ctx: Context,
    project: str | None,
    subject: str | None,
    modality: str | None,
) -> None:
    """List sessions/experiments in a project.

    \b
    Example:
        xnatctl session list --project MYPROJ
        xnatctl session list -P MYPROJ --subject SUB001
        xnatctl session list -P MYPROJ --modality MR
    """
    from xnatctl.core.validation import validate_project_id

    project = validate_project_id(require_project_from_context(ctx, project))
    sessions = SessionService(ctx.get_client()).list_sessions(
        project, subject=subject, modality=modality
    )

    print_output(
        sessions,
        format=ctx.output_format,
        columns=["id", "label", "subject", "date", "modality"],
        column_labels={
            "id": "ID",
            "label": "Label",
            "subject": "Subject",
            "date": "Date",
            "modality": "Modality",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@session.command("show")
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
@global_options
@handle_errors
@require_auth
def session_show(ctx: Context, session_id: str, project: str | None) -> None:
    """Show session details including scans and resources.

    \b
    Example:
        xnatctl session show -E XNAT_E00001
        xnatctl session show -E SESSION_LABEL -P MYPROJ
    """
    from xnatctl.core.output import print_table
    from xnatctl.core.validation import validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    session_ref = ExperimentRef(
        experiment=session_id,
        project_id=project,
        experiment_is_label=project is not None,
    )

    try:
        resolved = hierarchy.resolve_experiment(session_ref)
    except ResourceNotFoundError:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1) from None

    # Keep project scope (and subject scope if known) so nested calls respect
    # project ACLs and match pre-refactor URLs.
    nested_ref = ExperimentRef(
        experiment=resolved.experiment_id,
        project_id=resolved.project_id or project,
        subject=resolved.subject_id or resolved.subject_label,
    )

    # Get scans — resolve xsiType so non-imaging sessions return results
    try:
        scan_params: dict[str, str] = {}
        scan_xsi = hierarchy.resolve_scan_xsi_type(resolved.xsi_type)
        if scan_xsi:
            scan_params["xsiType"] = scan_xsi
        scans_resp = hierarchy.get_experiment_json(nested_ref, "scans", params=scan_params)
        scans = HierarchyService.extract_rows(scans_resp)
    except Exception:
        scans = []

    # Get resources
    try:
        res_resp = hierarchy.get_experiment_json(nested_ref, "resources")
        resources = HierarchyService.extract_rows(res_resp)
    except Exception:
        resources = []

    if ctx.output_format == OutputFormat.JSON:
        output = {
            "id": resolved.experiment_id,
            "label": resolved.experiment_label or "",
            "subject": resolved.subject_label or resolved.subject_id or "",
            "project": resolved.project_id or "",
            "date": resolved.session_date or "",
            "xsi_type": resolved.xsi_type or "",
            "scans": scans,
            "resources": resources,
        }
        print_output(output, format=OutputFormat.JSON)
    else:
        # Print session info
        click.echo(f"\n[Session: {resolved.experiment_label or session_id}]")
        click.echo(f"  ID:      {resolved.experiment_id}")
        click.echo(f"  Subject: {resolved.subject_label or resolved.subject_id or ''}")
        click.echo(f"  Project: {resolved.project_id or ''}")
        click.echo(f"  Date:    {resolved.session_date or ''}")
        click.echo(f"  Type:    {resolved.xsi_type or ''}")

        # Print scans table
        if scans:
            click.echo(f"\n[Scans ({len(scans)})]")
            scan_rows = []
            for s in scans:
                scan_rows.append(
                    {
                        "id": s.get("ID", ""),
                        "type": s.get("type", ""),
                        "series": s.get("series_description", ""),
                        "quality": s.get("quality", ""),
                        "frames": s.get("frames", ""),
                    }
                )
            print_table(
                scan_rows,
                ["id", "type", "series", "quality", "frames"],
                column_labels={
                    "id": "ID",
                    "type": "Type",
                    "series": "Series",
                    "quality": "Quality",
                    "frames": "Frames",
                },
            )

        # Print resources table
        if resources:
            click.echo(f"\n[Resources ({len(resources)})]")
            res_rows = []
            for r in resources:
                res_rows.append(
                    {
                        "label": r.get("label", ""),
                        "format": r.get("format", ""),
                        "count": r.get("file_count", ""),
                        "size": r.get("file_size", ""),
                    }
                )
            print_table(
                res_rows,
                ["label", "format", "count", "size"],
                column_labels={
                    "label": "Label",
                    "format": "Format",
                    "count": "Files",
                    "size": "Size",
                },
            )


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
    "--include-resources",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("--include-resources", "session_resources", True),
    help="Deprecated: use --session-resources instead",
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
    help="Deprecated: noop (cleanup is implicit with --extract)",
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
    from xnatctl.core.validation import validate_local_path_component, validate_path_writable

    # Map extract/keep_zips to internal unzip/cleanup
    unzip = extract or keep_zips
    cleanup = extract and not keep_zips

    # Validate mutual exclusion
    if resource and exclude_resource:
        raise click.UsageError("--resource and --exclude-resource are mutually exclusive")

    out_path = Path(out)

    # `--name` (when given) is validated by validate_local_path_option_cb, an
    # eager Click callback that runs at argument-parsing time -- before
    # @require_auth -- so a malformed --name fails without needing a valid
    # session. Without --name, the output directory falls back to the raw
    # session_id (see below); that fallback isn't known until here (it
    # depends on -E), so it's still validated in the command body, and goes
    # through ExperimentRef's URL-safety check (validate_xnat_label, which
    # does not forbid ':') rather than a local-filesystem check.
    if name is None:
        try:
            validate_local_path_component(session_id, "session_id (as the output directory name)")
        except InputValidationError as e:
            raise click.ClickException(str(e)) from e

    # Resolve project and workers from profile defaults
    if not project:
        project = default_project_from_context(ctx)
    workers = resolve_workers_from_context(ctx, workers, default=1)

    # Validate output path
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

    # Create session directory
    session_dir = out_path / (name or session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    from xnatctl.core.output import create_progress

    download_start = time.time()

    # Use parallel path when filtering is active (even with workers=1)
    use_parallel = workers > 1 or resource or exclude_resource

    # Keyed by scan_id, not appended per callback: under the include-filter
    # tier, on_scan_result fires once per (scan, resource) pair, so a scan
    # with N included resources must still collapse to one item and count
    # once in `scans` -- not be inflated N-fold.
    scan_ok: dict[str, bool] = {}
    scan_errors: dict[str, list[str]] = {}
    # Non-scan items (session resources, the sequential path's single
    # archive) -- declared here, before any exception could occur, so
    # `_emit_failed_summary` can always safely read it, no matter which of
    # this function's try/except blocks calls it.
    extra_items: list[TransferItemResult] = []

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
        """Every item known so far: per-scan results plus `extra_items`.

        A function, not a value computed once, because both underlying
        collections keep growing as the command progresses -- called again
        later it picks up whatever has been added since.
        """
        return [*_items_from_scan_ok(), *extra_items]

    def _item_counts(all_items: list[TransferItemResult]) -> tuple[int, int]:
        """(succeeded, failed) counted from *all_items*.

        This is the same per-scan/per-resource bookkeeping the summary's
        `items` field reports, so `status` can never disagree with what
        `items` shows (e.g. one scan whose DICOM resource landed but whose
        NIFTI resource didn't is one failed item, and must drive an overall
        "failed"/"partial" verdict from that, not from a separate raw task
        count).
        """
        succeeded = sum(1 for item in all_items if item.status == "success")
        failed = sum(1 for item in all_items if item.status == "failed")
        return succeeded, failed

    def _emit_failed_summary(exc: BaseException) -> None:
        """Print a failed summary in JSON mode before *exc* propagates.

        A no-op in table mode. The exception itself is never swallowed;
        callers re-raise it unchanged right after this.
        """
        if ctx.output_format != OutputFormat.JSON:
            return
        failure_items = [
            *_current_items(),
            TransferItemResult(id="session", status="failed", error=str(exc)),
        ]
        # Derived from the same items every other emission site uses, not
        # hardcoded: scans that completed before this exception hit (during
        # verify or extraction) still count as successes, so a run that got
        # partway must read "partial", not blanket "failed".
        succeeded_count, failed_count = _item_counts(failure_items)
        TransferSummary(
            operation="download",
            session_id=resolved_session_id,
            project=session_project,
            output_dir=str(session_dir),
            scans=len(scan_ok) if use_parallel else None,
            # Known when the engine call itself already completed (this
            # exception came from verify/extraction, afterward) -- null
            # only when the engine call is what raised, so nothing was
            # ever reported back to know a file count from.
            files=outcome.files if outcome is not None else None,
            duration_seconds=round(time.time() - download_start, 3),
            status=transfer_status(succeeded=succeeded_count, failed=failed_count),
            items=failure_items,
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
                # Reported even under --quiet. Quiet suppresses per-item
                # chatter, not the news that data is missing -- and --quiet is
                # the scripting mode, where silence is most dangerous.
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
        # The sequential path never enumerates individual scans (that's the
        # whole point of the single-ZIP request), so it has no per-scan
        # items -- but it still needs ONE success item, or a session
        # resource failing later would be the only item in `items` and read
        # as a total "failed" instead of "partial" (the scan archive itself
        # is fine; only the unrelated resource isn't).
        extra_items.append(TransferItemResult(id="archive", status="success"))

    scans_count = len(scan_ok) if use_parallel else None
    succeeded_scan_ids = [scan_id for scan_id, ok in scan_ok.items() if ok]

    # Download session-level resources (outside scans)
    session_resource_zips: list[tuple[str, Path]] = []
    if session_resources:
        if not ctx.quiet:
            click.echo("Downloading session-level resources...", err=True)
        # What was actually requested, fetched up front so a resource that
        # never even started downloading (because an earlier one in the
        # batch raised) still shows up as a failed item below -- not just
        # silently missing from `items`. Only fetched for -o json: it costs
        # an extra listing call that table mode's swallow-and-warn text
        # output has no use for.
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
            except Exception as exc:
                listing_error = str(exc)
        session_resource_error: str | None = None
        try:
            # `downloaded=session_resource_zips`: appended to in place as
            # each resource's ZIP finishes, so if a later resource's
            # download raises here, everything that landed before it is
            # still in this list for verification below -- not lost with
            # the exception this except block is about to catch.
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
        except Exception as e:
            session_resource_error = str(e)
            if not ctx.quiet:
                click.echo(f"  Session resources: {e}", err=True)
        # A resource that failed contributes a failed item -- previously
        # this whole block was swallowed silently outside of --verify, so
        # -o json could report "success" with a resource missing on disk.
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
            # `requested_resource_labels` is empty (the listing failed), so
            # the loop above had nothing to report.
            if session_resource_error is None:
                # But the download itself succeeded -- its own internal
                # listing worked and everything it found landed. A
                # transient hiccup in OUR up-front listing call must not
                # read as a total failure the download never actually was;
                # report what we now know landed instead.
                for label in succeeded_resource_labels:
                    extra_items.append(TransferItemResult(id=f"resource:{label}", status="success"))
            else:
                # The download also raised -- but `session_resource_zips`
                # is appended to in place as each resource finishes, so
                # anything that landed *before* the exception is still
                # known even though the batch as a whole didn't complete.
                # Report those as success items too, or a resource that
                # genuinely landed would be missing from `items` entirely
                # while the lone "resources" failure item below made the
                # whole run read as a flat "failed" instead of "partial".
                for label in succeeded_resource_labels:
                    extra_items.append(TransferItemResult(id=f"resource:{label}", status="success"))
                extra_items.append(
                    TransferItemResult(
                        id="resources", status="failed", error=session_resource_error
                    )
                )

    # Extract ZIPs if requested. With --verify also on, the session-resource
    # ZIPs this run just downloaded are held back here: extraction (and, with
    # cleanup, deletion) is deferred until after verification has read them
    # intact, further down. Extracting them now would both flatten away the
    # resource label they carry (a known extract_session_zips limitation, not
    # something worked around here) and, with cleanup on, delete the exact
    # file --verify needs to check.
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
                )
            else:
                extract_session_zips(
                    session_dir,
                    cleanup=cleanup,
                    on_message=None if ctx.quiet else _echo_stderr,
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
            ).emit()
            raise SystemExit(1)
        # Losing scans and exiting 0 is how a partial transfer gets mistaken
        # for a whole one. The names are in the message so the caller can
        # retry exactly what is missing.
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
        # The parallel engine always extracts, regardless of --extract; the
        # sequential engine only does when --extract was passed, else the
        # (still verifiable) ZIP is left in place.
        verify_local_root = session_dir if (use_parallel or unzip) else None
        verify_zip_paths: list[Path | tuple[Path, str]] = (
            [] if verify_local_root is not None else [session_dir / "scans.zip"]
        )
        # Exactly the session-resource ZIPs THIS run downloaded, never
        # rediscovered by globbing the directory -- a stale
        # resources_QC.zip left over from an earlier run must not be able to
        # stand in for a resource whose download failed just now. Verified
        # from the ZIP directly regardless of --extract (see above): each is
        # scoped to exactly one label, keyed with no marker search at all.
        verify_zip_paths.extend((zip_path, label) for label, zip_path in session_resource_zips)
        # De-duped: under the include-filter tier, on_scan_result fires once
        # per (scan, resource) pair, so a scan with N included resources
        # would otherwise appear N times and cost N redundant manifest calls.
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
        # Verification has now read the session-resource ZIPs intact; apply
        # the extract/cleanup that was deferred for them above.
        try:
            if unzip and resource_zip_paths:
                extract_session_zips(
                    session_dir,
                    cleanup=cleanup,
                    on_message=None if ctx.quiet else _echo_stderr,
                    zip_paths=resource_zip_paths,
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
                # Always "failed", not derived from _item_counts: a
                # checksum mismatch/missing file is a data-integrity
                # failure regardless of how many scans otherwise landed,
                # unlike a bookkeeping-only resource miss elsewhere.
                status="failed",
                items=_current_items(),
                verification=TransferVerification.from_report(verification),
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
        # No mismatches, no missing files, no collisions -- nothing was
        # actually verified: either every checked file landed in
        # `unverifiable` (the server had no checksums on record), or the
        # manifest listed nothing at all for a scope with files on disk.
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
        ).emit()
        # A session-resource failure surfaces here even though nothing else
        # went wrong: it's caught by `items`/`status` now, not just --verify.
        if json_status != "success":
            raise SystemExit(1)
        return

    if outcome is not None:
        # Says what arrived rather than only where it was put: "0 scans
        # (0 files)" is the honest report for a session that yielded nothing.
        print_success(
            f"Downloaded {outcome.succeeded} scans ({outcome.files} files) to: {session_dir}"
        )
    else:
        print_success(f"Downloaded session to: {session_dir}")


@click.group()
def local() -> None:
    """Local file operations (no XNAT connection required)."""
    pass


@local.command("extract")
@click.argument("input_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--cleanup/--no-cleanup", default=True, help="Remove ZIPs after extraction")
@click.option("--recursive", "-r", is_flag=True, help="Process subdirectories")
@click.option("--dry-run", is_flag=True, help="Preview what would be extracted")
@handle_errors
def local_extract(input_dir: str, cleanup: bool, recursive: bool, dry_run: bool) -> None:  # noqa: C901  # pre-existing; see pyproject
    """Extract downloaded XNAT session ZIPs.

    This command extracts ZIP files from previously downloaded sessions,
    creating organized subdirectories. Use after downloading without --unzip,
    or to re-process existing downloads.

    \b
    Example:
        # Extract a single session directory
        xnatctl local extract ./data/XNAT_E00001

        # Extract all sessions, keeping ZIPs
        xnatctl local extract ./data --recursive --no-cleanup

        # Preview extraction
        xnatctl local extract ./data --recursive --dry-run
    """
    import shutil
    import zipfile

    input_path = Path(input_dir)

    # Find ZIP files
    if recursive:
        zip_files = list(input_path.rglob("*.zip"))
    else:
        zip_files = list(input_path.glob("*.zip"))

    if not zip_files:
        click.echo("No ZIP files found.", err=True)
        return

    click.echo(f"Found {len(zip_files)} ZIP file(s)", err=True)

    if dry_run:
        click.echo("\n[DRY-RUN] Would extract:", err=True)
        for zip_file in zip_files:
            extract_dir = zip_file.parent / zip_file.stem
            click.echo(f"  {zip_file} -> {extract_dir}/", err=True)
            if cleanup:
                click.echo(f"    (would remove {zip_file.name})", err=True)
        return

    extracted = 0
    failed = 0

    for zip_path in zip_files:
        click.echo(f"Extracting {zip_path.name}...", err=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue

                    member_path = Path(member.filename)
                    if any(part.startswith(".") for part in member_path.parts):
                        continue

                    parts = member_path.parts
                    if len(parts) < 2:
                        continue

                    stripped_path = Path(*parts[1:])
                    output_path = zip_path.parent / stripped_path
                    # Guard against ZipSlip path traversal
                    if not output_path.resolve().is_relative_to(zip_path.parent.resolve()):
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(output_path, "wb") as target:
                        shutil.copyfileobj(source, target)

            extracted += 1

            if cleanup:
                zip_path.unlink()
                click.echo(f"  Removed {zip_path.name}", err=True)
        except zipfile.BadZipFile:
            print_error(f"Invalid ZIP file: {zip_path.name}")
            failed += 1
        except Exception as e:
            print_error(f"Failed to extract {zip_path.name}: {e}")
            failed += 1

    if failed:
        click.echo(f"\nExtracted: {extracted}, Failed: {failed}", err=True)
    else:
        print_success(f"Extracted {extracted} ZIP file(s)")
