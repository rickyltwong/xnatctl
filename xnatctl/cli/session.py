"""Session commands for xnatctl."""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
    parallel_options,
    require_auth,
)
from xnatctl.core.output import OutputFormat, print_error, print_output, print_success


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
@require_auth
@handle_errors
def session_list(
    ctx: Context,
    project: str | None,
    subject: str | None,
    modality: str | None,
) -> None:
    """List sessions/experiments in a project.

    Example:
        xnatctl session list --project MYPROJ
        xnatctl session list -P MYPROJ --subject SUB001
        xnatctl session list -P MYPROJ --modality MR
    """
    from xnatctl.core.validation import validate_project_id

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    client = ctx.get_client()

    # Build query
    params = {"columns": "ID,label,subject_label,date,xsiType"}
    if subject:
        params["subject_label"] = subject

    # Get sessions
    resp = client.get_json(f"/data/projects/{project}/experiments", params=params)
    results = resp.get("ResultSet", {}).get("Result", [])

    # Filter and transform
    sessions = []
    for r in results:
        xsi_type = r.get("xsiType", "")

        # Filter by modality
        if modality:
            if modality == "MR" and "MRSession" not in xsi_type:
                continue
            elif modality == "PET" and "PETSession" not in xsi_type:
                continue
            elif modality == "CT" and "CTSession" not in xsi_type:
                continue
            elif modality == "EEG" and "EEGSession" not in xsi_type:
                continue

        # Extract modality from xsiType
        detected_modality = "?"
        if "MRSession" in xsi_type:
            detected_modality = "MR"
        elif "PETSession" in xsi_type:
            detected_modality = "PET"
        elif "CTSession" in xsi_type:
            detected_modality = "CT"
        elif "EEGSession" in xsi_type:
            detected_modality = "EEG"

        sessions.append(
            {
                "id": r.get("ID", ""),
                "label": r.get("label", ""),
                "subject": r.get("subject_label", ""),
                "date": r.get("date", ""),
                "modality": detected_modality,
            }
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
@click.argument("session_id")
@global_options
@require_auth
@handle_errors
def session_show(ctx: Context, session_id: str) -> None:
    """Show session details including scans and resources.

    Example:
        xnatctl session show XNAT_E00001
    """
    from xnatctl.core.output import print_table
    from xnatctl.core.validation import validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()

    # Get session details
    resp = client.get_json(f"/data/experiments/{session_id}")
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1)

    session_data = results[0]

    # Get scans
    try:
        scans_resp = client.get_json(f"/data/experiments/{session_id}/scans")
        scans = scans_resp.get("ResultSet", {}).get("Result", [])
    except Exception:
        scans = []

    # Get resources
    try:
        res_resp = client.get_json(f"/data/experiments/{session_id}/resources")
        resources = res_resp.get("ResultSet", {}).get("Result", [])
    except Exception:
        resources = []

    if ctx.output_format == OutputFormat.JSON:
        output = {
            "id": session_data.get("ID", ""),
            "label": session_data.get("label", ""),
            "subject": session_data.get("subject_label", ""),
            "project": session_data.get("project", ""),
            "date": session_data.get("date", ""),
            "xsi_type": session_data.get("xsiType", ""),
            "scans": scans,
            "resources": resources,
        }
        print_output(output, format=OutputFormat.JSON)
    else:
        # Print session info
        click.echo(f"\n[Session: {session_data.get('label', session_id)}]")
        click.echo(f"  ID:      {session_data.get('ID', '')}")
        click.echo(f"  Subject: {session_data.get('subject_label', '')}")
        click.echo(f"  Project: {session_data.get('project', '')}")
        click.echo(f"  Date:    {session_data.get('date', '')}")
        click.echo(f"  Type:    {session_data.get('xsiType', '')}")

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
@click.argument("session_id")
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option("--name", help="Output directory name (defaults to session ID)")
@click.option("--include-resources", is_flag=True, help="Include session-level resources")
@click.option("--include-assessors", is_flag=True, help="Include assessor data")
@click.option("--pattern", help="File pattern filter (e.g., '*.dcm')")
@click.option("--resume", is_flag=True, help="Resume interrupted download")
@click.option("--verify", is_flag=True, help="Verify checksums after download")
@click.option("--unzip/--no-unzip", default=False, help="Extract downloaded ZIPs")
@click.option(
    "--cleanup/--no-cleanup",
    default=True,
    help="Remove ZIPs after successful extraction (with --unzip)",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@parallel_options
@global_options
@require_auth
@handle_errors
def session_download(
    ctx: Context,
    session_id: str,
    out: str,
    name: str | None,
    include_resources: bool,
    include_assessors: bool,
    pattern: str | None,
    resume: bool,
    verify: bool,
    unzip: bool,
    cleanup: bool,
    dry_run: bool,
    parallel: bool,
    workers: int,
) -> None:
    """Download session data.

    Example:
        xnatctl session download XNAT_E00001
        xnatctl session download XNAT_E00001 --out ./data
        xnatctl session download XNAT_E00001 --name CLM01_CAMH_0041 --out ./data
        xnatctl session download XNAT_E00001 --out ./data --include-resources
        xnatctl session download XNAT_E00001 --out ./data --unzip --cleanup
        xnatctl session download XNAT_E00001 --out ./data --dry-run
    """
    from xnatctl.core.validation import validate_path_writable, validate_session_id

    session_id = validate_session_id(session_id)
    out_path = Path(out)

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

    # Validate output path
    if not out_path.exists():
        out_path.mkdir(parents=True, exist_ok=True)
    validate_path_writable(out_path)

    client = ctx.get_client()

    # Get session info to find project/subject
    resp = client.get_json(f"/data/experiments/{session_id}")
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1)

    session_data = results[0]
    project = session_data.get("project", "")
    subject = session_data.get("subject_ID", "") or session_data.get("subject_label", "")

    if dry_run:
        click.echo(f"[DRY-RUN] Would download session {session_id}")
        click.echo(f"  Project: {project}")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  Output: {out_path / (name or session_id)}")
        click.echo(f"  Include resources: {include_resources}")
        click.echo(f"  Include assessors: {include_assessors}")
        return

    # Create session directory
    session_dir = out_path / (name or session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    from xnatctl.core.output import create_progress

    with create_progress() as progress:
        # Download scans
        task = progress.add_task("Downloading scans...", total=100)

        scans_url = (
            f"/data/projects/{project}/subjects/{subject}/experiments/{session_id}/scans/ALL/files"
        )
        scans_zip = session_dir / "scans.zip"

        # Stream download
        with client._get_client().stream(
            "GET", scans_url, params={"format": "zip"}, cookies=client._get_cookies()
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(scans_zip, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        progress.update(task, completed=int(downloaded / total * 100))

        progress.update(task, completed=100, description="Scans downloaded")

        # Download resources if requested
        if include_resources:
            task2 = progress.add_task("Downloading resources...", total=None)
            try:
                res_url = f"/data/projects/{project}/subjects/{subject}/experiments/{session_id}/resources"
                res_resp = client.get_json(res_url)
                resources = res_resp.get("ResultSet", {}).get("Result", [])

                for res in resources:
                    label = res.get("label", "resource")
                    res_zip = session_dir / f"resources_{label}.zip"
                    files_url = f"{res_url}/{label}/files"

                    with client._get_client().stream(
                        "GET", files_url, params={"format": "zip"}, cookies=client._get_cookies()
                    ) as resp:
                        resp.raise_for_status()
                        with open(res_zip, "wb") as f:
                            for chunk in resp.iter_bytes():
                                f.write(chunk)

                progress.update(task2, description=f"Resources downloaded ({len(resources)})")
            except Exception as e:
                progress.update(task2, description=f"Resources: {e}")

    # Extract ZIPs if requested
    if unzip:
        _extract_session_zips(session_dir, cleanup=cleanup, quiet=ctx.quiet)

    print_success(f"Downloaded session to: {session_dir}")


@session.command("upload")
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", required=True, help="Subject ID")
@click.option("--session", "-E", required=True, help="Session label")
@click.option("--username", "-u", help="XNAT username (REST upload)")
@click.option("--password", help="XNAT password (REST upload)")
@click.option(
    "--transport",
    type=click.Choice(["rest", "dicom-store"]),
    default="rest",
    help="Upload transport (default: rest)",
)
# REST upload options
@click.option(
    "--archive-format",
    type=click.Choice(["tar", "zip"]),
    default="tar",
    help="Archive format for REST upload (default: tar)",
)
@click.option(
    "--upload-workers",
    type=int,
    default=4,
    help="Parallel upload workers (default: 4)",
)
@click.option(
    "--archive-workers",
    type=int,
    default=4,
    help="Parallel archive workers (default: 4)",
)
@click.option(
    "--overwrite",
    type=click.Choice(["none", "append", "delete"]),
    default="delete",
    help="Overwrite mode (default: delete)",
)
@click.option(
    "--direct-archive/--prearchive",
    default=True,
    help="Direct archive or use prearchive (default: direct)",
)
@click.option(
    "--ignore-unparsable/--no-ignore-unparsable",
    default=True,
    help="Skip unparsable DICOM files (default: yes)",
)
# DICOM C-STORE options
@click.option(
    "--dicom-host",
    envvar="XNAT_DICOM_HOST",
    help="DICOM SCP host (env: XNAT_DICOM_HOST)",
)
@click.option(
    "--dicom-port",
    type=int,
    default=104,
    envvar="XNAT_DICOM_PORT",
    help="DICOM SCP port (default: 104, env: XNAT_DICOM_PORT)",
)
@click.option(
    "--called-aet",
    envvar="XNAT_DICOM_CALLED_AET",
    help="Called AE Title (env: XNAT_DICOM_CALLED_AET)",
)
@click.option(
    "--calling-aet",
    default="XNATCTL",
    envvar="XNAT_DICOM_CALLING_AET",
    help="Calling AE Title (default: XNATCTL, env: XNAT_DICOM_CALLING_AET)",
)
@click.option(
    "--dicom-workers",
    type=int,
    default=4,
    help="Parallel DICOM C-STORE associations (default: 4)",
)
# Common options
@click.option("--dry-run", is_flag=True, help="Preview without uploading")
@global_options
@require_auth
@handle_errors
def session_upload(
    ctx: Context,
    input_path: str,
    project: str | None,
    subject: str,
    session: str,
    username: str | None,
    password: str | None,
    transport: str,
    archive_format: str,
    upload_workers: int,
    archive_workers: int,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
    dicom_host: str | None,
    dicom_port: int,
    called_aet: str | None,
    calling_aet: str,
    dicom_workers: int,
    dry_run: bool,
) -> None:
    """Upload DICOM session via REST import or DICOM C-STORE.

    Supports both single archive files and directories of DICOM files.
    For directories, files are split into N batches where N = upload-workers.

    REST Upload Examples:

        # Upload a single archive
        xnatctl session upload ./archive.zip -P MYPROJ -S SUB001 -E SESS001

        # Upload a directory with parallel batching
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001

        # High-throughput settings for fast storage/network
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 \\
            --upload-workers 16 --archive-workers 8

    DICOM C-STORE Examples:

        # Using DICOM network transfer
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 \\
            --transport dicom-store --dicom-host xnat.example.org \\
            --called-aet XNAT --dicom-port 8104
    """
    from xnatctl.core.validation import (
        validate_project_id,
        validate_session_id,
        validate_subject_id,
    )

    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None
        if not project:
            profile_name = ctx.profile_name or (
                ctx.config.default_profile if ctx.config else "default"
            )
            raise click.ClickException(
                f"Project required. Pass --project/-P or set default_project in profile '{profile_name}'."
            )

    project = validate_project_id(project)
    subject = validate_subject_id(subject)
    session = validate_session_id(session)

    source_path = Path(input_path)

    # Dry run handling
    if dry_run:
        click.echo("[DRY-RUN] Would upload with the following settings:")
        click.echo(f"  Source: {source_path}")
        click.echo(f"  Project: {project}")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  Session: {session}")
        click.echo(f"  Transport: {transport}")
        if transport == "rest":
            click.echo(f"  Archive format: {archive_format}")
            click.echo(f"  Upload workers: {upload_workers}")
            click.echo(f"  Archive workers: {archive_workers}")
            click.echo(f"  Overwrite: {overwrite}")
            click.echo(f"  Direct archive: {direct_archive}")
        else:
            click.echo(f"  DICOM host: {dicom_host}")
            click.echo(f"  DICOM port: {dicom_port}")
            click.echo(f"  Called AET: {called_aet}")
            click.echo(f"  Calling AET: {calling_aet}")
            click.echo(f"  Workers: {dicom_workers}")
        return

    # DICOM C-STORE transport
    if transport == "dicom-store":
        _upload_dicom_store(
            ctx=ctx,
            source_path=source_path,
            dicom_host=dicom_host,
            dicom_port=dicom_port,
            called_aet=called_aet,
            calling_aet=calling_aet,
            dicom_workers=dicom_workers,
        )
        return

    # REST transport
    if source_path.is_file():
        # Single archive upload
        _upload_single_archive(
            ctx=ctx,
            archive_path=source_path,
            project=project,
            subject=subject,
            session=session,
            overwrite=overwrite,
            direct_archive=direct_archive,
            ignore_unparsable=ignore_unparsable,
        )
    elif source_path.is_dir():
        # Directory upload with parallel batching
        _upload_directory_parallel(
            ctx=ctx,
            source_dir=source_path,
            project=project,
            subject=subject,
            session=session,
            username=username,
            password=password,
            upload_workers=upload_workers,
            archive_workers=archive_workers,
            archive_format=archive_format,
            overwrite=overwrite,
            direct_archive=direct_archive,
            ignore_unparsable=ignore_unparsable,
        )


def _upload_single_archive(
    ctx: Context,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
) -> None:
    """Upload a single archive file."""
    from xnatctl.core.output import create_progress

    client = ctx.get_client()

    # Only show progress for table output and not quiet
    show_progress = ctx.output_format == OutputFormat.TABLE and not ctx.quiet

    if show_progress:
        from xnatctl.core.output import create_progress

        with create_progress() as progress:
            task = progress.add_task(f"Uploading {archive_path.name}...", total=100)

            _do_single_upload(
                client,
                archive_path,
                project,
                subject,
                session,
                overwrite,
                direct_archive,
                ignore_unparsable,
            )

            progress.update(task, completed=100)
    else:
        _do_single_upload(
            client,
            archive_path,
            project,
            subject,
            session,
            overwrite,
            direct_archive,
            ignore_unparsable,
        )

    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {"success": True, "file": str(archive_path), "session": session},
            format=OutputFormat.JSON,
        )
    else:
        print_success(f"Uploaded {archive_path.name}")


def _do_single_upload(
    client,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
) -> None:
    """Execute the actual upload."""
    content_type = (
        "application/zip" if archive_path.suffix.lower() == ".zip" else "application/x-tar"
    )

    with open(archive_path, "rb") as f:
        resp = client.post(
            "/data/services/import",
            params={
                "import-handler": "DICOM-zip",
                "project": project,
                "subject": subject,
                "session": session,
                "overwrite": overwrite,
                "Direct-Archive": "true" if direct_archive else "false",
                "Ignore-Unparsable": "true" if ignore_unparsable else "false",
                "inbody": "true",
            },
            data=f,
            headers={"Content-Type": content_type},
            timeout=10800,  # 3 hour timeout
        )

    if resp.status_code != 200:
        print_error(f"Upload failed: {resp.text[:200]}")
        raise SystemExit(1)


def _upload_directory_parallel(
    ctx: Context,
    source_dir: Path,
    project: str,
    subject: str,
    session: str,
    username: str | None,
    password: str | None,
    upload_workers: int,
    archive_workers: int,
    archive_format: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
) -> None:
    """Upload a directory of DICOM files using parallel batching."""
    from xnatctl.core.config import get_credentials
    from xnatctl.uploaders.parallel_rest import (
        UploadProgress,
        upload_dicom_parallel_rest,
    )

    client = ctx.get_client()
    session_token = client.session_token

    if not session_token:
        env_username, env_password = get_credentials()
        username = username or env_username
        password = password or env_password

        # Ensure we have credentials for parallel uploads (each thread authenticates)
        if not username:
            username = click.prompt("Username")
        if not password:
            password = click.prompt("Password", hide_input=True)

    # Progress callback - only for table output and not quiet
    show_progress = ctx.output_format == OutputFormat.TABLE and not ctx.quiet

    if show_progress:
        from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            transient=True,
        ) as progress:
            task_id = progress.add_task("Preparing...", total=None)

            def progress_callback(p: UploadProgress) -> None:
                """Update the Rich progress UI for parallel uploads."""
                if p.total > 0:
                    progress.update(task_id, total=p.total, completed=p.current)
                progress.update(task_id, description=f"[{p.phase}] {p.message}")

            summary = upload_dicom_parallel_rest(
                base_url=client.base_url,
                username=username,
                password=password,
                session_token=session_token,
                verify_ssl=client.verify_ssl,
                source_dir=source_dir,
                project=project,
                subject=subject,
                session=session,
                upload_workers=upload_workers,
                archive_workers=archive_workers,
                archive_format=archive_format,
                overwrite=overwrite,
                direct_archive=direct_archive,
                ignore_unparsable=ignore_unparsable,
                progress_callback=progress_callback,
            )
    else:
        summary = upload_dicom_parallel_rest(
            base_url=client.base_url,
            username=username,
            password=password,
            session_token=session_token,
            verify_ssl=client.verify_ssl,
            source_dir=source_dir,
            project=project,
            subject=subject,
            session=session,
            upload_workers=upload_workers,
            archive_workers=archive_workers,
            archive_format=archive_format,
            overwrite=overwrite,
            direct_archive=direct_archive,
            ignore_unparsable=ignore_unparsable,
        )

    # Output results
    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "success": summary.success,
                "total_files": summary.total_files,
                "total_size_mb": round(summary.total_size_mb, 2),
                "duration_seconds": round(summary.duration, 2),
                "batches_succeeded": summary.batches_succeeded,
                "batches_failed": summary.batches_failed,
                "errors": summary.errors,
            },
            format=OutputFormat.JSON,
        )
    else:
        if summary.success:
            print_success(
                f"Uploaded {summary.total_files} files "
                f"({summary.total_size_mb:.1f} MB) in {summary.duration:.1f}s"
            )
        else:
            print_error(
                f"Upload completed with errors: "
                f"{summary.batches_failed}/{summary.batches_succeeded + summary.batches_failed} batches failed"
            )
            for error in summary.errors[:5]:  # Show first 5 errors
                click.echo(f"  - {error}", err=True)
            if len(summary.errors) > 5:
                click.echo(f"  ... and {len(summary.errors) - 5} more errors", err=True)
            raise SystemExit(1)


def _upload_dicom_store(
    ctx: Context,
    source_path: Path,
    dicom_host: str | None,
    dicom_port: int,
    called_aet: str | None,
    calling_aet: str,
    dicom_workers: int,
) -> None:
    """Upload via DICOM C-STORE protocol."""
    # Validate required options
    if not dicom_host:
        print_error("DICOM C-STORE requires --dicom-host or XNAT_DICOM_HOST environment variable")
        raise SystemExit(1)

    if not called_aet:
        print_error(
            "DICOM C-STORE requires --called-aet or XNAT_DICOM_CALLED_AET environment variable"
        )
        raise SystemExit(1)

    if not source_path.is_dir():
        print_error("DICOM C-STORE requires a directory of DICOM files, not an archive")
        raise SystemExit(1)

    # Lazy import to avoid requiring pynetdicom for non-DICOM operations
    try:
        from xnatctl.uploaders.dicom_store import send_dicom_store
    except ImportError as e:
        print_error(
            "DICOM C-STORE requires pydicom and pynetdicom. "
            "Install with: pip install xnatctl[dicom]"
        )
        raise SystemExit(1) from e

    # Execute upload
    if not ctx.quiet:
        click.echo(f"Sending DICOM files to {dicom_host}:{dicom_port} ({called_aet})...")

    summary = send_dicom_store(
        dicom_root=source_path,
        host=dicom_host,
        port=dicom_port,
        called_aet=called_aet,
        calling_aet=calling_aet,
        workers=dicom_workers,
        cleanup=True,
    )

    # Output results
    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "success": summary.success,
                "total_files": summary.total_files,
                "sent": summary.sent,
                "failed": summary.failed,
            },
            format=OutputFormat.JSON,
        )
    else:
        if summary.success:
            print_success(f"Sent {summary.sent}/{summary.total_files} DICOM files")
        else:
            print_error(
                f"DICOM C-STORE completed with errors: "
                f"{summary.failed}/{summary.total_files} files failed"
            )
            click.echo(f"Check logs in: {summary.log_dir}", err=True)
            raise SystemExit(1)


def _extract_session_zips(session_dir: Path, cleanup: bool = True, quiet: bool = False) -> None:
    """Extract all ZIP files in a session directory.

    Args:
        session_dir: Path to session directory containing ZIPs
        cleanup: Remove ZIPs after successful extraction
        quiet: Suppress progress output
    """
    import zipfile

    zip_files = list(session_dir.glob("*.zip"))
    if not zip_files:
        return

    for zip_path in zip_files:
        extract_dir = session_dir / zip_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)

        if not quiet:
            click.echo(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            if cleanup:
                zip_path.unlink()
                if not quiet:
                    click.echo(f"  Removed {zip_path.name}")
        except zipfile.BadZipFile:
            print_error(f"Invalid ZIP file: {zip_path.name}")


# =============================================================================
# Local Commands (for offline processing)
# =============================================================================


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
def local_extract(input_dir: str, cleanup: bool, recursive: bool, dry_run: bool) -> None:
    """Extract downloaded XNAT session ZIPs.

    This command extracts ZIP files from previously downloaded sessions,
    creating organized subdirectories. Use after downloading without --unzip,
    or to re-process existing downloads.

    Example:
        # Extract a single session directory
        xnatctl local extract ./data/XNAT_E00001

        # Extract all sessions, keeping ZIPs
        xnatctl local extract ./data --recursive --no-cleanup

        # Preview extraction
        xnatctl local extract ./data --recursive --dry-run
    """
    import zipfile

    input_path = Path(input_dir)

    # Find ZIP files
    if recursive:
        zip_files = list(input_path.rglob("*.zip"))
    else:
        zip_files = list(input_path.glob("*.zip"))

    if not zip_files:
        click.echo("No ZIP files found.")
        return

    click.echo(f"Found {len(zip_files)} ZIP file(s)")

    if dry_run:
        click.echo("\n[DRY-RUN] Would extract:")
        for zip_file in zip_files:
            extract_dir = zip_file.parent / zip_file.stem
            click.echo(f"  {zip_file} -> {extract_dir}/")
            if cleanup:
                click.echo(f"    (would remove {zip_file.name})")
        return

    extracted = 0
    failed = 0

    for zip_path in zip_files:
        extract_dir = zip_path.parent / zip_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
            extracted += 1

            if cleanup:
                zip_path.unlink()
                click.echo(f"  Removed {zip_path.name}")
        except zipfile.BadZipFile:
            print_error(f"Invalid ZIP file: {zip_path.name}")
            failed += 1
        except Exception as e:
            print_error(f"Failed to extract {zip_path.name}: {e}")
            failed += 1

    if failed:
        click.echo(f"\nExtracted: {extracted}, Failed: {failed}")
    else:
        print_success(f"Extracted {extracted} ZIP file(s)")
