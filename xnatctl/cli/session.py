"""Session commands for xnatctl."""

from __future__ import annotations

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
)
from xnatctl.core.exceptions import DownloadError, ResourceNotFoundError
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_json,
    print_output,
    print_success,
)
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.models.progress import VerificationReport
from xnatctl.services.downloads import (
    DownloadOutcome,
    DownloadService,
    ScanResult,
    extract_session_zips,
)
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.sessions import SessionService


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
@click.option("--name", hidden=True, help="Output directory name (defaults to session ID)")
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
    from xnatctl.core.validation import validate_path_writable

    # Map extract/keep_zips to internal unzip/cleanup
    unzip = extract or keep_zips
    cleanup = extract and not keep_zips

    # Validate mutual exclusion
    if resource and exclude_resource:
        raise click.UsageError("--resource and --exclude-resource are mutually exclusive")

    out_path = Path(out)

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

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

    # Use parallel path when filtering is active (even with workers=1)
    use_parallel = workers > 1 or resource or exclude_resource

    outcome: DownloadOutcome | None = None
    succeeded_scan_ids: list[str] = []
    if use_parallel:

        def on_scan_start(count: int) -> None:
            if ctx.quiet:
                return
            if count == 0:
                click.echo("No scans found in session", err=True)
            else:
                click.echo(f"Downloading {count} scans in parallel...", err=True)

        def on_scan_result(result: ScanResult) -> None:
            if result.ok:
                succeeded_scan_ids.append(result.scan_id)
                if not ctx.quiet:
                    status = f" ({result.message})" if result.message else ""
                    click.echo(f"  Scan {result.scan_id} done{status}", err=True)
            else:
                # Reported even under --quiet. Quiet suppresses per-item
                # chatter, not the news that data is missing -- and --quiet is
                # the scripting mode, where silence is most dangerous.
                click.echo(f"  Scan {result.scan_id} FAILED: {result.message}", err=True)

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
        if outcome.failed:
            click.echo(
                f"{len(outcome.failed)}/{outcome.succeeded + len(outcome.failed)} "
                "scan downloads failed",
                err=True,
            )
    else:
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

    # Download session-level resources (outside scans)
    session_resource_zips: list[tuple[str, Path]] = []
    if session_resources:
        if not ctx.quiet:
            click.echo("Downloading session-level resources...", err=True)
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
            if not ctx.quiet:
                click.echo(f"  Session resources: {e}", err=True)

    # Extract ZIPs if requested. With --verify also on, the session-resource
    # ZIPs this run just downloaded are held back here: extraction (and, with
    # cleanup, deletion) is deferred until after verification has read them
    # intact, further down. Extracting them now would both flatten away the
    # resource label they carry (a known extract_session_zips limitation, not
    # something worked around here) and, with cleanup on, delete the exact
    # file --verify needs to check.
    resource_zip_paths = [zip_path for _label, zip_path in session_resource_zips]
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

    if outcome is not None and outcome.failed:
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
        if ctx.output_format == OutputFormat.JSON:
            # A minimal, standalone block for now -- a later change folds
            # this into a full transfer summary alongside the rest of the
            # command's (currently unstructured) output.
            print_json(
                {
                    "verification": {
                        "matched": verification.matched,
                        "mismatched": verification.mismatched,
                        "missing_local": verification.missing_local,
                        "missing_remote": verification.missing_remote,
                        "unverifiable": verification.unverifiable,
                        "collisions": verification.collisions,
                    }
                }
            )

        # Verification has now read the session-resource ZIPs intact; apply
        # the extract/cleanup that was deferred for them above.
        if unzip and resource_zip_paths:
            extract_session_zips(
                session_dir,
                cleanup=cleanup,
                on_message=None if ctx.quiet else _echo_stderr,
                zip_paths=resource_zip_paths,
            )

    if verification is not None and not verification.success:
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
        # No mismatches, no missing files, no collisions -- every file
        # checked landed in `unverifiable`: the server had no checksums on
        # record for anything, so nothing was actually verified.
        raise DownloadError(
            "Verification failed: the server provided no checksums for any "
            f"downloaded file ({len(verification.unverifiable)} unverifiable); "
            "nothing was verified.",
            session_id,
            {"unverifiable": verification.unverifiable},
        )

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
