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
from xnatctl.core.timeouts import DEFAULT_HTTP_TIMEOUT_SECONDS


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


def _download_session_fast(
    client,
    session_project: str,
    subject: str,
    resolved_session_id: str,
    session_dir: Path,
    quiet: bool = False,
) -> None:
    """Download session scans in parallel and extract to standard structure.

    Produces the XNAT compressed-uploader layout:
        {session_dir}/scans/{scan_id}/resources/DICOM/files/{files...}
    """
    import tempfile
    import zipfile
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import httpx

    scans_resp = client.get_json(f"/data/experiments/{resolved_session_id}/scans")
    results = scans_resp.get("ResultSet", {}).get("Result", [])
    scan_ids = [r.get("ID") for r in results if r.get("ID")]

    if not scan_ids:
        if not quiet:
            click.echo("No scans found in session")
        return

    if not quiet:
        click.echo(f"Downloading {len(scan_ids)} scans in parallel...")

    base_url = client.base_url
    session_token = client.session_token
    verify_ssl = client.verify_ssl
    timeout = client.timeout

    def download_and_extract_scan(scan_id: str) -> tuple[str, bool, str]:
        """Download a single scan ZIP and extract into standard layout."""
        scan_url = f"/data/projects/{session_project}/subjects/{subject}/experiments/{resolved_session_id}/scans/{scan_id}/resources/DICOM/files"
        try:
            with httpx.Client(base_url=base_url, timeout=timeout, verify=verify_ssl) as http:
                cookies = {"JSESSIONID": session_token} if session_token else {}
                with http.stream(
                    "GET", scan_url, params={"format": "zip"}, cookies=cookies
                ) as resp:
                    resp.raise_for_status()
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            tmp.write(chunk)

            # Build target directory in standard layout
            target_dir = session_dir / "scans" / scan_id / "resources" / "DICOM" / "files"
            target_dir.mkdir(parents=True, exist_ok=True)

            # Extract files from ZIP, flattening XNAT's internal path
            with zipfile.ZipFile(tmp_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    # Extract just the filename, discarding XNAT's internal path
                    filename = Path(member.filename).name
                    if not filename or filename.startswith("."):
                        continue
                    dest = target_dir / filename
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())

            tmp_path.unlink(missing_ok=True)
            return scan_id, True, ""
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return scan_id, True, "no DICOM"
            return scan_id, False, str(e)
        except Exception as e:
            return scan_id, False, str(e)

    succeeded = []
    failed = []

    workers = min(len(scan_ids), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_and_extract_scan, sid): sid for sid in scan_ids}
        for future in as_completed(futures):
            scan_id, success, error = future.result()
            if success:
                succeeded.append(scan_id)
                if not quiet:
                    status = f" ({error})" if error else ""
                    click.echo(f"  Scan {scan_id} done{status}")
            else:
                failed.append((scan_id, error))
                if not quiet:
                    click.echo(f"  Scan {scan_id} FAILED: {error}")

    if failed and not quiet:
        click.echo(f"Warning: {len(failed)}/{len(scan_ids)} scans failed")


@session.command("download")
@click.argument("session_id")
@click.option(
    "--project", "-P", help="Project ID (required when using session label instead of XNAT ID)"
)
@click.option("--out", type=click.Path(), default=".", show_default=True, help="Output directory")
@click.option("--name", help="Output directory name (defaults to session ID)")
@click.option("--fast", is_flag=True, help="Parallel per-scan download (one worker per scan)")
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
    project: str | None,
    out: str,
    name: str | None,
    fast: bool,
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

    SESSION_ID can be either an XNAT internal ID (e.g., XNAT_E00001) or a
    session label. When using a label, provide --project/-P to resolve it.

    Example:
        xnatctl session download XNAT_E00001
        xnatctl session download XNAT_E00001 --out ./data
        xnatctl session download CLM01_UCA_00134_01_SE01_MR -P CLM01_UCA_4 --out ./data
        xnatctl session download CLM01_UCA_00134_01_SE01_MR -P CLM01_UCA_4 --fast --out ./data
        xnatctl session download XNAT_E00001 --name CLM01_CAMH_0041 --out ./data
        xnatctl session download XNAT_E00001 --out ./data --include-resources
        xnatctl session download XNAT_E00001 --out ./data --unzip --cleanup
        xnatctl session download XNAT_E00001 --out ./data --dry-run
    """
    from xnatctl.core.validation import validate_path_writable

    out_path = Path(out)

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

    # Validate output path
    if not out_path.exists():
        out_path.mkdir(parents=True, exist_ok=True)
    validate_path_writable(out_path)

    client = ctx.get_client()

    # Resolve session and get session info
    resolved_session_id = session_id
    session_project = project
    subject = None

    if project:
        # Use project-scoped endpoint (works with labels)
        resp = client.get_json(f"/data/projects/{project}/experiments/{session_id}")
        results = resp.get("ResultSet", {}).get("Result", [])
        if results:
            resolved_session_id = results[0].get("ID", session_id)
            session_project = results[0].get("project", project)
            subject = results[0].get("subject_ID", "") or results[0].get("subject_label", "")
        else:
            # Try items format (XNAT returns this for single experiment lookups)
            items = resp.get("items", [])
            if items:
                data_fields = items[0].get("data_fields", {})
                resolved_session_id = data_fields.get("ID", session_id)
                session_project = data_fields.get("project", project)
                subject = data_fields.get("subject_ID", "")
            else:
                print_error(f"Session '{session_id}' not found in project '{project}'")
                raise SystemExit(1)
    else:
        # Direct experiment lookup (requires XNAT ID)
        resp = client.get_json(f"/data/experiments/{session_id}")
        results = resp.get("ResultSet", {}).get("Result", [])
        if results:
            resolved_session_id = results[0].get("ID", session_id)
            session_project = results[0].get("project", "")
            subject = results[0].get("subject_ID", "") or results[0].get("subject_label", "")
        else:
            items = resp.get("items", [])
            if items:
                data_fields = items[0].get("data_fields", {})
                resolved_session_id = data_fields.get("ID", session_id)
                session_project = data_fields.get("project", "")
                subject = data_fields.get("subject_ID", "")
            else:
                print_error(f"Session not found: {session_id}")
                raise SystemExit(1)

    if not subject:
        print_error(f"Could not determine subject for session: {session_id}")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would download session {session_id}")
        if resolved_session_id != session_id:
            click.echo(f"  Resolved ID: {resolved_session_id}")
        click.echo(f"  Project: {session_project}")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  Output: {out_path / (name or session_id)}")
        click.echo(f"  Include resources: {include_resources}")
        click.echo(f"  Include assessors: {include_assessors}")
        return

    # Create session directory
    session_dir = out_path / (name or session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    from xnatctl.core.output import create_progress

    if fast:
        _download_session_fast(
            client=client,
            session_project=session_project,
            subject=subject,
            resolved_session_id=resolved_session_id,
            session_dir=session_dir,
            quiet=ctx.quiet,
        )
    else:
        with create_progress() as progress:
            task = progress.add_task("Downloading scans...", total=100)

            scans_url = f"/data/projects/{session_project}/subjects/{subject}/experiments/{resolved_session_id}/scans/ALL/files"
            scans_zip = session_dir / "scans.zip"

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

            if include_resources:
                task2 = progress.add_task("Downloading resources...", total=None)
                try:
                    res_url = f"/data/projects/{session_project}/subjects/{subject}/experiments/{resolved_session_id}/resources"
                    res_resp = client.get_json(res_url)
                    resources = res_resp.get("ResultSet", {}).get("Result", [])

                    for res in resources:
                        label = res.get("label", "resource")
                        res_zip = session_dir / f"resources_{label}.zip"
                        files_url = f"{res_url}/{label}/files"

                        with client._get_client().stream(
                            "GET",
                            files_url,
                            params={"format": "zip"},
                            cookies=client._get_cookies(),
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
    type=click.Choice(["rest", "gradual-dicom", "dicom-store"]),
    default="rest",
    help="Upload transport: rest (batch ZIP), gradual-dicom (parallel per-file), dicom-store (network)",
)
# REST upload options
@click.option(
    "--archive-format",
    type=click.Choice(["tar", "zip"]),
    default="tar",
    help="Archive format for REST upload (default: tar)",
)
@click.option(
    "--zip-to-tar/--no-zip-to-tar",
    default=False,
    help="Convert ZIP archive to TAR before REST upload",
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
    zip_to_tar: bool,
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

    # Gradual-DICOM transport (parallel per-file upload)
    if transport == "gradual-dicom":
        _upload_gradual_dicom(
            ctx=ctx,
            source_path=source_path,
            project=project,
            subject=subject,
            session=session,
            workers=upload_workers,
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
            zip_to_tar=zip_to_tar,
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


def _upload_gradual_dicom(
    ctx: Context,
    source_path: Path,
    project: str,
    subject: str,
    session: str,
    workers: int = 4,
) -> None:
    """Upload DICOM files using gradual-DICOM handler (parallel per-file)."""
    import tempfile
    import zipfile
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import httpx

    client = ctx.get_client()
    base_url = client.base_url
    session_token = client.session_token
    verify_ssl = client.verify_ssl

    dcm_files: list[Path] = []
    temp_dir = None

    if source_path.is_file() and source_path.suffix.lower() == ".zip":
        temp_dir = tempfile.mkdtemp(prefix="xnatctl_gradual_")
        temp_path = Path(temp_dir)
        if not ctx.quiet:
            click.echo(f"Extracting {source_path.name}...")
        with zipfile.ZipFile(source_path, "r") as zf:
            zf.extractall(temp_path)
        dcm_files = [f for f in temp_path.rglob("*") if f.is_file() and not f.name.startswith(".")]
    elif source_path.is_dir():
        dcm_files = [
            f for f in source_path.rglob("*") if f.is_file() and not f.name.startswith(".")
        ]
    else:
        print_error("gradual-dicom requires a directory or ZIP file")
        raise SystemExit(1)

    if not dcm_files:
        print_error("No files found to upload")
        if temp_dir:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit(1)

    if not ctx.quiet:
        click.echo(f"Uploading {len(dcm_files)} files using gradual-DICOM ({workers} workers)...")

    def upload_file(file_path: Path) -> tuple[str, bool, str]:
        from xnatctl.services.uploads import upload_with_retry

        try:
            with httpx.Client(base_url=base_url, timeout=120.0, verify=verify_ssl) as http:
                cookies = {"JSESSIONID": session_token} if session_token else {}

                def _attempt():
                    with open(file_path, "rb") as f:
                        return http.post(
                            "/data/services/import",
                            params={
                                "inbody": "true",
                                "import-handler": "gradual-DICOM",
                                "PROJECT_ID": project,
                                "SUBJECT_ID": subject,
                                "EXPT_LABEL": session,
                            },
                            content=f.read(),
                            headers={"Content-Type": "application/dicom"},
                            cookies=cookies,
                        )

                resp = upload_with_retry(_attempt, label=f"gradual-DICOM {file_path.name}")
                if resp.status_code in (200, 201):
                    return file_path.name, True, ""
                return file_path.name, False, f"HTTP {resp.status_code}"
        except Exception as e:
            return file_path.name, False, str(e)

    succeeded = 0
    failed = 0
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(upload_file, f): f for f in dcm_files}
        for i, future in enumerate(as_completed(futures), 1):
            name, success, error = future.result()
            if success:
                succeeded += 1
            else:
                failed += 1
                errors.append(f"{name}: {error}")
            if not ctx.quiet and i % 100 == 0:
                click.echo(f"  Progress: {i}/{len(dcm_files)} ({succeeded} ok, {failed} failed)")

    if temp_dir:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "success": failed == 0,
                "total": len(dcm_files),
                "succeeded": succeeded,
                "failed": failed,
                "errors": errors[:10],
            },
            format=OutputFormat.JSON,
        )
    else:
        if failed == 0:
            print_success(f"Uploaded {succeeded} files via gradual-DICOM")
        else:
            print_error(f"Uploaded {succeeded}/{len(dcm_files)} files ({failed} failed)")
            for err in errors[:5]:
                click.echo(f"  - {err}", err=True)
            if len(errors) > 5:
                click.echo(f"  ... and {len(errors) - 5} more errors", err=True)
            raise SystemExit(1)


def _upload_single_archive(
    ctx: Context,
    archive_path: Path,
    project: str,
    subject: str,
    session: str,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
    zip_to_tar: bool,
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
                zip_to_tar,
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
            zip_to_tar,
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
    zip_to_tar: bool,
) -> None:
    """Execute the actual upload with retry on transient errors."""
    from xnatctl.services.uploads import upload_with_retry

    params = {
        "import-handler": "DICOM-zip",
        "project": project,
        "subject": subject,
        "session": session,
        "overwrite": overwrite,
        "overwrite_files": "true",
        "quarantine": "false",
        "triggerPipelines": "true",
        "rename": "false",
        "Direct-Archive": "true" if direct_archive else "false",
        "Ignore-Unparsable": "true" if ignore_unparsable else "false",
        "inbody": "true",
    }

    with _maybe_zip_to_tar(archive_path, zip_to_tar) as (upload_path, content_type):

        def _attempt():
            with open(upload_path, "rb") as f:
                return client.post(
                    "/data/services/import",
                    params=params,
                    data=f,
                    headers={"Content-Type": content_type},
                    timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
                )

        resp = upload_with_retry(_attempt, label=f"REST upload {archive_path.name}")

    if not (200 <= resp.status_code < 300):
        print_error(f"Upload failed (HTTP {resp.status_code}): {resp.text[:500]}")
        raise SystemExit(1)


def _safe_mtime(date_time: tuple) -> float:
    """Convert ZIP date_time tuple to timestamp safely.

    Args:
        date_time: 6-tuple (year, month, day, hour, minute, second)

    Returns:
        Unix timestamp, defaulting to 0 if conversion fails or date is invalid.
    """
    import time

    try:
        year = date_time[0]
        # Validate year is in reasonable range (ZIP format supports 1980-2107)
        if year < 1980 or year > 2107:
            return 0.0
        # Use 0 for DST (let system figure it out) instead of -1
        return time.mktime(date_time + (0, 0, 0))
    except (ValueError, OverflowError, OSError):
        # Invalid date - use epoch
        return 0.0


def _zip_to_tar(archive_path: Path, tar_path: Path) -> None:
    """Convert ZIP archive to TAR format.

    Args:
        archive_path: Source ZIP file
        tar_path: Destination TAR file

    Raises:
        zipfile.BadZipFile: If ZIP is corrupted
        OSError: If file operations fail
    """
    import tarfile
    import zipfile

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Validate ZIP integrity first
            bad_file = zf.testzip()
            if bad_file:
                raise zipfile.BadZipFile(f"Corrupted file in archive: {bad_file}")

            with tarfile.open(tar_path, "w") as tf:
                for info in zf.infolist():
                    name = info.filename
                    if info.is_dir():
                        tarinfo = tarfile.TarInfo(name.rstrip("/") + "/")
                        tarinfo.type = tarfile.DIRTYPE
                        tarinfo.mtime = _safe_mtime(info.date_time)
                        tarinfo.size = 0
                        tf.addfile(tarinfo)
                        continue

                    tarinfo = tarfile.TarInfo(name)
                    tarinfo.size = info.file_size
                    tarinfo.mtime = _safe_mtime(info.date_time)
                    with zf.open(info, "r") as src:
                        tf.addfile(tarinfo, fileobj=src)
    except zipfile.BadZipFile:
        raise
    except Exception as e:
        raise OSError(f"Failed to convert ZIP to TAR: {e}") from e


def _default_content_type(archive_path: Path) -> str:
    return "application/zip" if archive_path.suffix.lower() == ".zip" else "application/x-tar"


def _should_zip_to_tar(archive_path: Path, zip_to_tar: bool) -> bool:
    return zip_to_tar and archive_path.suffix.lower() == ".zip"


def _maybe_zip_to_tar(archive_path: Path, zip_to_tar: bool):
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _converter():
        if _should_zip_to_tar(archive_path, zip_to_tar):
            with tempfile.TemporaryDirectory() as temp_dir:
                tar_path = Path(temp_dir) / f"{archive_path.stem}.tar"
                _zip_to_tar(archive_path, tar_path)
                yield tar_path, "application/x-tar"
        else:
            yield archive_path, _default_content_type(archive_path)

    return _converter()


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
    from xnatctl.models.progress import UploadProgress
    from xnatctl.services.uploads import UploadService

    client = ctx.get_client()

    if not client.session_token:
        env_username, env_password = get_credentials()
        username = username or env_username
        password = password or env_password

        if not username:
            username = click.prompt("Username")
        if not password:
            password = click.prompt("Password", hide_input=True)

    service = UploadService(client)
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
                progress.update(task_id, description=f"[{p.phase.value}] {p.message}")

            summary = service.upload_dicom_parallel(
                source_dir=source_dir,
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
                progress_callback=progress_callback,
            )
    else:
        summary = service.upload_dicom_parallel(
            source_dir=source_dir,
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
            for error in summary.errors[:5]:
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

    try:
        from xnatctl.services.uploads import UploadService
    except ImportError as e:
        print_error(
            "DICOM C-STORE requires pydicom and pynetdicom. "
            "Install with: pip install xnatctl[dicom]"
        )
        raise SystemExit(1) from e

    if not ctx.quiet:
        click.echo(f"Sending DICOM files to {dicom_host}:{dicom_port} ({called_aet})...")

    client = ctx.get_client()
    service = UploadService(client)

    summary = service.upload_dicom_store(
        dicom_root=source_path,
        host=dicom_host,
        called_aet=called_aet,
        port=dicom_port,
        calling_aet=calling_aet,
        workers=dicom_workers,
        cleanup=True,
    )

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
        if not quiet:
            click.echo(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue

                    member_path = Path(member)
                    if any(part.startswith(".") for part in member_path.parts):
                        continue

                    parts = member_path.parts
                    if len(parts) > 1:
                        stripped_path = Path(*parts[1:])
                    else:
                        stripped_path = member_path

                    target_path = session_dir / stripped_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(target_path, "wb") as target:
                        target.write(source.read())

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
        click.echo(f"Extracting {zip_path.name}...")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    if member.filename.startswith(".") or "/.." in member.filename:
                        continue

                    parts = Path(member.filename).parts
                    if len(parts) < 2:
                        continue

                    stripped_path = Path(*parts[1:])
                    output_path = zip_path.parent / stripped_path
                    output_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(output_path, "wb") as target:
                        target.write(source.read())

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
