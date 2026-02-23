"""Session commands for xnatctl."""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    global_options,
    handle_errors,
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
@require_auth
@handle_errors
def session_show(ctx: Context, session_id: str, project: str | None) -> None:
    """Show session details including scans and resources.

    Example:
        xnatctl session show -E XNAT_E00001
        xnatctl session show -E SESSION_LABEL -P MYPROJ
    """
    from xnatctl.core.output import print_table
    from xnatctl.core.validation import validate_session_id

    session_id = validate_session_id(session_id)
    client = ctx.get_client()

    # Resolve project from profile default if not provided
    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None

    # Get session details
    base = (
        f"/data/projects/{project}/experiments/{session_id}"
        if project
        else f"/data/experiments/{session_id}"
    )
    resp = client.get_json(base)
    results = resp.get("ResultSet", {}).get("Result", [])

    if not results:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1)

    session_data = results[0]

    # Get scans
    try:
        scans_resp = client.get_json(f"{base}/scans")
        scans = scans_resp.get("ResultSet", {}).get("Result", [])
    except Exception:
        scans = []

    # Get resources
    try:
        res_resp = client.get_json(f"{base}/resources")
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
    workers: int = 8,
    quiet: bool = False,
) -> None:
    """Download session scans in parallel and extract to standard structure.

    Args:
        client: Authenticated XNATClient.
        session_project: Project ID.
        subject: Subject ID.
        resolved_session_id: Resolved XNAT experiment ID.
        session_dir: Output directory for session data.
        workers: Maximum parallel download workers.
        quiet: Suppress progress output.

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
        import shutil

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

            try:
                # Build target directory in standard layout
                target_dir = session_dir / "scans" / scan_id / "resources" / "DICOM" / "files"
                target_dir.mkdir(parents=True, exist_ok=True)

                # Extract files from ZIP, preserving (safe) relative paths.
                #
                # Avoid flattening to basename only: some XNAT ZIPs can contain repeated
                # filenames under different directories, and flattening would overwrite
                # earlier files silently (leading to missing DICOMs on re-upload).
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    resolved_root = target_dir.resolve()
                    renamed = 0
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        member_path = Path(member.filename)
                        if any(part.startswith(".") for part in member_path.parts):
                            continue

                        parts = member_path.parts
                        # Prefer stripping up to "files/" if present; otherwise strip
                        # the top-level folder to avoid deep XNAT zip prefixes.
                        if "files" in parts:
                            idx = parts.index("files")
                            rel_parts = parts[idx + 1 :]
                            if not rel_parts:
                                continue
                            rel = Path(*rel_parts)
                        elif len(parts) > 1:
                            rel = Path(*parts[1:])
                        else:
                            rel = member_path

                        if not rel.name or rel.name.startswith("."):
                            continue

                        dest = (target_dir / rel).resolve()
                        if not dest.is_relative_to(resolved_root):
                            continue

                        dest.parent.mkdir(parents=True, exist_ok=True)

                        final_dest = dest
                        if final_dest.exists():
                            renamed += 1
                            stem = final_dest.stem
                            suffix = final_dest.suffix
                            i = 1
                            while True:
                                candidate = final_dest.with_name(f"{stem}__dup{i}{suffix}")
                                if not candidate.exists():
                                    final_dest = candidate
                                    break
                                i += 1

                        with zf.open(member) as src, open(final_dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            finally:
                tmp_path.unlink(missing_ok=True)

            status = f"renamed {renamed} duplicate filenames" if renamed else ""
            return scan_id, True, status
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return scan_id, True, "no DICOM"
            return scan_id, False, str(e)
        except Exception as e:
            return scan_id, False, str(e)

    succeeded = []
    failed = []

    pool_size = min(len(scan_ids), workers)
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
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
@click.option("--name", help="Output directory name (defaults to session ID)")
@click.option(
    "--workers",
    "-w",
    type=int,
    default=1,
    show_default=True,
    help="Parallel download workers (1 = sequential single-ZIP, >1 = parallel per-scan)",
)
@click.option("--include-resources", is_flag=True, help="Include session-level resources")
@click.option("--unzip/--no-unzip", default=False, help="Extract downloaded ZIPs")
@click.option(
    "--cleanup/--no-cleanup",
    default=True,
    help="Remove ZIPs after successful extraction (with --unzip)",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@global_options
@require_auth
@handle_errors
def session_download(
    ctx: Context,
    session_id: str,
    project: str | None,
    out: str,
    name: str | None,
    workers: int,
    include_resources: bool,
    unzip: bool,
    cleanup: bool,
    dry_run: bool,
) -> None:
    """Download session data.

    -E accepts an XNAT experiment ID (accession #) or a session label.
    When using a label, -P is required (or set default_project in your profile).

    Example:
        xnatctl session download -E XNAT_E00001
        xnatctl session download -E XNAT_E00001 --out ./data
        xnatctl session download -E XNAT_E00001 --out ./data --workers 8
        xnatctl session download -E SESSION_LABEL -P MYPROJECT --out ./data
        xnatctl session download -E XNAT_E00001 --name CLM01_CAMH_0041 --out ./data
        xnatctl session download -E XNAT_E00001 --out ./data --include-resources
        xnatctl session download -E XNAT_E00001 --out ./data --unzip --cleanup
        xnatctl session download -E XNAT_E00001 --out ./data --dry-run
    """
    from xnatctl.core.validation import validate_path_writable

    out_path = Path(out)

    if name and ("/" in name or "\\" in name):
        raise click.ClickException("--name cannot contain path separators")

    # Resolve project from profile default if not provided
    if not project:
        profile = ctx.config.get_profile(ctx.profile_name) if ctx.config else None
        project = profile.default_project if profile else None

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
        click.echo(f"  Workers: {workers}")
        click.echo(f"  Include resources: {include_resources}")
        return

    # Create session directory
    session_dir = out_path / (name or session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    from xnatctl.core.output import create_progress

    if workers > 1:
        _download_session_fast(
            client=client,
            session_project=session_project,
            subject=subject,
            resolved_session_id=resolved_session_id,
            session_dir=session_dir,
            workers=workers,
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
@click.option("--gradual", is_flag=True, help="Use per-file upload instead of batch archive")
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
    "--workers",
    "-w",
    type=int,
    default=4,
    show_default=True,
    help="Parallel workers",
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
    gradual: bool,
    archive_format: str,
    zip_to_tar: bool,
    workers: int,
    overwrite: str,
    direct_archive: bool,
    ignore_unparsable: bool,
    dry_run: bool,
) -> None:
    """Upload DICOM session via REST import.

    Supports both single archive files and directories of DICOM files.
    For directories, files are split into N batches where N = workers.

    For DICOM C-STORE network transfer, use `session upload-dicom` instead.

    Example:
        xnatctl session upload ./archive.zip -P MYPROJ -S SUB001 -E SESS001
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --workers 16
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --gradual --workers 40
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
        click.echo(f"  Mode: {'gradual' if gradual else 'rest'}")
        click.echo(f"  Workers: {workers}")
        if not gradual:
            click.echo(f"  Archive format: {archive_format}")
            click.echo(f"  Overwrite: {overwrite}")
            click.echo(f"  Direct archive: {direct_archive}")
        return

    # Gradual per-file upload
    if gradual:
        _upload_gradual_dicom(
            ctx=ctx,
            source_path=source_path,
            project=project,
            subject=subject,
            session=session,
            workers=workers,
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
            upload_workers=workers,
            archive_workers=workers,
            archive_format=archive_format,
            overwrite=overwrite,
            direct_archive=direct_archive,
            ignore_unparsable=ignore_unparsable,
        )


@session.command("upload-exam")
@click.argument("exam_root", type=click.Path(exists=True, file_okay=False))
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", required=True, help="Subject ID")
@click.option("--session", "-E", required=True, help="Session label")
@click.option(
    "--workers",
    "-w",
    type=int,
    default=4,
    show_default=True,
    help="Parallel workers",
)
@click.option(
    "--misc-label",
    default="MISC",
    show_default=True,
    help="Resource label to use for top-level misc files",
)
@click.option(
    "--skip-resources",
    is_flag=True,
    help="Skip attaching top-level resource dirs and misc files",
)
@click.option(
    "--attach-only",
    is_flag=True,
    help="Attach resources only (skip DICOM upload)",
)
@click.option("--dry-run", is_flag=True, help="Preview without uploading")
@global_options
@require_auth
@handle_errors
def session_upload_exam(
    ctx: Context,
    exam_root: str,
    project: str | None,
    subject: str,
    session: str,
    workers: int,
    misc_label: str,
    skip_resources: bool,
    attach_only: bool,
    dry_run: bool,
) -> None:
    """Upload an exam root (DICOM + session resources).

    Exam roots follow a common folder convention:
    - DICOM files may appear anywhere under the root (recursive)
    - Top-level directories without DICOM-like files are treated as session-level
      resources (label = directory name)
    - Top-level non-DICOM files are treated as misc attachments under --misc-label
    """

    from xnatctl.core.exam import classify_exam_root
    from xnatctl.core.validation import (
        validate_project_id,
        validate_resource_label,
        validate_session_id,
        validate_subject_id,
    )
    from xnatctl.services.resources import ResourceService
    from xnatctl.services.uploads import UploadService

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

    misc_label = validate_resource_label(misc_label)

    exam_root_path = Path(exam_root)
    classification = classify_exam_root(exam_root_path)

    resource_labels: list[str] = []
    for resource_dir in classification.resource_dirs:
        resource_labels.append(validate_resource_label(resource_dir.name))

    if dry_run:
        click.echo("[DRY-RUN] Would upload exam with the following settings:")
        click.echo(f"  Exam root: {exam_root_path}")
        click.echo(f"  Project: {project}")
        click.echo(f"  Subject: {subject}")
        click.echo(f"  Session: {session}")
        click.echo(f"  Workers: {workers}")
        click.echo(f"  Resource dirs ({len(resource_labels)}):")
        for label in resource_labels:
            click.echo(f"    - {label}")
        click.echo(f"  Misc label: {misc_label}")
        return

    client = ctx.get_client()

    if not attach_only:
        if not classification.dicom_files:
            raise click.ClickException(f"No DICOM files found under: {exam_root_path}")

        upload_service = UploadService(client)
        summary = upload_service.upload_dicom_gradual_files(
            files=classification.dicom_files,
            project=project,
            subject=subject,
            session=session,
            workers=workers,
        )
        if not summary.success:
            errors = "; ".join(summary.errors[:3])
            raise click.ClickException(
                f"DICOM upload failed ({summary.succeeded}/{summary.total} succeeded): {errors}"
            )

    if skip_resources:
        return

    def _resolve_experiment_id() -> str | None:
        resp = client.get_json(f"/data/projects/{project}/experiments/{session}")
        results = resp.get("ResultSet", {}).get("Result", [])
        if results:
            return results[0].get("ID") or session

        items = resp.get("items", [])
        if items:
            data_fields = items[0].get("data_fields", {})
            return data_fields.get("ID") or session

        return None

    resolved_experiment_id = _resolve_experiment_id()
    if not resolved_experiment_id:
        raise click.ClickException(
            "Could not resolve archived experiment ID for session "
            f"'{session}' in project '{project}'. If the DICOM import is still in "
            "prearchive (not yet archived), archive it and re-run with --attach-only."
        )

    resource_service = ResourceService(client)

    for resource_dir in classification.resource_dirs:
        label = validate_resource_label(resource_dir.name)
        resource_service.create(
            session_id=resolved_experiment_id,
            resource_label=label,
            project=project,
        )
        resource_service.upload_directory(
            session_id=resolved_experiment_id,
            resource_label=label,
            directory_path=resource_dir,
            project=project,
        )

    if classification.misc_files:
        resource_service.create(
            session_id=resolved_experiment_id,
            resource_label=misc_label,
            project=project,
        )
        for misc_file in classification.misc_files:
            resource_service.upload_file(
                session_id=resolved_experiment_id,
                resource_label=misc_label,
                file_path=misc_file,
                project=project,
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
    from xnatctl.models.progress import UploadProgress
    from xnatctl.services.uploads import UploadService

    client = ctx.get_client()
    service = UploadService(client)

    progress_counter = 0

    def progress_callback(p: UploadProgress) -> None:
        nonlocal progress_counter
        progress_counter += 1
        if p.phase.value == "uploading" and progress_counter % 100 != 0:
            return
        click.echo(f"  [{p.phase.value}] {p.message}")

    try:
        summary = service.upload_dicom_gradual(
            source_path=source_path,
            project=project,
            subject=subject,
            session=session,
            workers=workers,
            progress_callback=progress_callback if not ctx.quiet else None,
        )
    except (ValueError, FileNotFoundError) as e:
        print_error(str(e))
        raise SystemExit(1) from e

    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "success": summary.success,
                "total": summary.total,
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "errors": summary.errors[:10],
            },
            format=OutputFormat.JSON,
        )
    else:
        if summary.success:
            print_success(f"Uploaded {summary.succeeded} files via gradual-DICOM")
        else:
            print_error(
                f"Uploaded {summary.succeeded}/{summary.total} files ({summary.failed} failed)"
            )
            for err in summary.errors[:5]:
                click.echo(f"  - {err}", err=True)
            if len(summary.errors) > 5:
                click.echo(f"  ... and {len(summary.errors) - 5} more errors", err=True)
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


@session.command("upload-dicom")
@click.argument("input_path", type=click.Path(exists=True))
@click.option(
    "--host",
    envvar="XNAT_DICOM_HOST",
    required=True,
    help="DICOM SCP host (env: XNAT_DICOM_HOST)",
)
@click.option(
    "--called-aet",
    envvar="XNAT_DICOM_CALLED_AET",
    required=True,
    help="Called AE Title (env: XNAT_DICOM_CALLED_AET)",
)
@click.option(
    "--port",
    type=int,
    default=104,
    show_default=True,
    envvar="XNAT_DICOM_PORT",
    help="DICOM SCP port (env: XNAT_DICOM_PORT)",
)
@click.option(
    "--calling-aet",
    default="XNATCTL",
    show_default=True,
    envvar="XNAT_DICOM_CALLING_AET",
    help="Calling AE Title (env: XNAT_DICOM_CALLING_AET)",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=4,
    show_default=True,
    help="Parallel DICOM C-STORE associations",
)
@click.option("--dry-run", is_flag=True, help="Preview without sending")
@global_options
@require_auth
@handle_errors
def session_upload_dicom(
    ctx: Context,
    input_path: str,
    host: str,
    called_aet: str,
    port: int,
    calling_aet: str,
    workers: int,
    dry_run: bool,
) -> None:
    """Upload DICOM files via C-STORE network protocol.

    Requires pydicom and pynetdicom (install with: pip install xnatctl[dicom]).

    Example:
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT --port 8104
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT -w 8
    """
    source_path = Path(input_path)

    if dry_run:
        click.echo("[DRY-RUN] Would send DICOM files via C-STORE:")
        click.echo(f"  Source: {source_path}")
        click.echo(f"  Host: {host}:{port}")
        click.echo(f"  Called AET: {called_aet}")
        click.echo(f"  Calling AET: {calling_aet}")
        click.echo(f"  Workers: {workers}")
        return

    _upload_dicom_store(
        ctx=ctx,
        source_path=source_path,
        dicom_host=host,
        dicom_port=port,
        called_aet=called_aet,
        calling_aet=calling_aet,
        dicom_workers=workers,
    )


def _extract_session_zips(session_dir: Path, cleanup: bool = True, quiet: bool = False) -> None:
    """Extract all ZIP files in a session directory.

    Args:
        session_dir: Path to session directory containing ZIPs
        cleanup: Remove ZIPs after successful extraction
        quiet: Suppress progress output
    """
    import shutil
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
                    # Guard against ZipSlip path traversal
                    if not target_path.resolve().is_relative_to(session_dir.resolve()):
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)

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
    import shutil
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
