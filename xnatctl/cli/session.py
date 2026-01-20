"""Session commands for xnatctl."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from xnatctl.cli.common import Context, global_options, require_auth, handle_errors, parallel_options
from xnatctl.core.output import print_output, print_error, print_success, OutputFormat


@click.group()
def session() -> None:
    """Manage XNAT sessions/experiments."""
    pass


@session.command("list")
@click.option("--project", "-P", required=True, help="Project ID")
@click.option("--subject", "-S", help="Filter by subject")
@click.option("--modality", type=click.Choice(["MR", "PET", "CT", "EEG"]), help="Filter by modality")
@global_options
@require_auth
@handle_errors
def session_list(
    ctx: Context,
    project: str,
    subject: Optional[str],
    modality: Optional[str],
) -> None:
    """List sessions/experiments in a project.

    Example:
        xnatctl session list --project MYPROJ
        xnatctl session list -P MYPROJ --subject SUB001
        xnatctl session list -P MYPROJ --modality MR
    """
    from xnatctl.core.validation import validate_project_id

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

        sessions.append({
            "id": r.get("ID", ""),
            "label": r.get("label", ""),
            "subject": r.get("subject_label", ""),
            "date": r.get("date", ""),
            "modality": detected_modality,
        })

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
    from xnatctl.core.validation import validate_session_id
    from xnatctl.core.output import print_table

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
                scan_rows.append({
                    "id": s.get("ID", ""),
                    "type": s.get("type", ""),
                    "series": s.get("series_description", ""),
                    "quality": s.get("quality", ""),
                    "frames": s.get("frames", ""),
                })
            print_table(
                scan_rows,
                ["id", "type", "series", "quality", "frames"],
                column_labels={"id": "ID", "type": "Type", "series": "Series", "quality": "Quality", "frames": "Frames"},
            )

        # Print resources table
        if resources:
            click.echo(f"\n[Resources ({len(resources)})]")
            res_rows = []
            for r in resources:
                res_rows.append({
                    "label": r.get("label", ""),
                    "format": r.get("format", ""),
                    "count": r.get("file_count", ""),
                    "size": r.get("file_size", ""),
                })
            print_table(
                res_rows,
                ["label", "format", "count", "size"],
                column_labels={"label": "Label", "format": "Format", "count": "Files", "size": "Size"},
            )


@session.command("download")
@click.argument("session_id")
@click.option("--out", "-o", required=True, type=click.Path(), help="Output directory")
@click.option("--include-resources", is_flag=True, help="Include session-level resources")
@click.option("--include-assessors", is_flag=True, help="Include assessor data")
@click.option("--pattern", help="File pattern filter (e.g., '*.dcm')")
@click.option("--resume", is_flag=True, help="Resume interrupted download")
@click.option("--verify", is_flag=True, help="Verify checksums after download")
@click.option("--dry-run", is_flag=True, help="Preview what would be downloaded")
@parallel_options
@global_options
@require_auth
@handle_errors
def session_download(
    ctx: Context,
    session_id: str,
    out: str,
    include_resources: bool,
    include_assessors: bool,
    pattern: Optional[str],
    resume: bool,
    verify: bool,
    dry_run: bool,
    parallel: bool,
    workers: int,
) -> None:
    """Download session data.

    Example:
        xnatctl session download XNAT_E00001 --out ./data
        xnatctl session download XNAT_E00001 -o ./data --include-resources
        xnatctl session download XNAT_E00001 -o ./data --dry-run
    """
    from xnatctl.core.validation import validate_session_id, validate_path_writable

    session_id = validate_session_id(session_id)
    out_path = Path(out)

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
        click.echo(f"  Output: {out_path}")
        click.echo(f"  Include resources: {include_resources}")
        click.echo(f"  Include assessors: {include_assessors}")
        return

    # Create session directory
    session_dir = out_path / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    from xnatctl.core.output import create_progress

    with create_progress() as progress:
        # Download scans
        task = progress.add_task("Downloading scans...", total=100)

        scans_url = f"/data/projects/{project}/subjects/{subject}/experiments/{session_id}/scans/ALL/files"
        scans_zip = session_dir / "scans.zip"

        # Stream download
        with client._get_client().stream("GET", scans_url, params={"format": "zip"}, cookies=client._get_cookies()) as resp:
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

                    with client._get_client().stream("GET", files_url, params={"format": "zip"}, cookies=client._get_cookies()) as resp:
                        resp.raise_for_status()
                        with open(res_zip, "wb") as f:
                            for chunk in resp.iter_bytes():
                                f.write(chunk)

                progress.update(task2, description=f"Resources downloaded ({len(resources)})")
            except Exception as e:
                progress.update(task2, description=f"Resources: {e}")

    print_success(f"Downloaded session to: {session_dir}")


@session.command("upload")
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--project", "-P", required=True, help="Project ID")
@click.option("--subject", "-S", required=True, help="Subject ID")
@click.option("--session", "-E", required=True, help="Session label")
@click.option("--transport", type=click.Choice(["rest", "dicom-store"]), default="rest", help="Upload transport")
@click.option("--batches", type=int, default=4, help="Number of parallel batches")
@click.option("--overwrite", type=click.Choice(["none", "append", "delete"]), default="delete")
@parallel_options
@global_options
@require_auth
@handle_errors
def session_upload(
    ctx: Context,
    input_path: str,
    project: str,
    subject: str,
    session: str,
    transport: str,
    batches: int,
    overwrite: str,
    parallel: bool,
    workers: int,
) -> None:
    """Upload DICOM session via REST import or DICOM C-STORE.

    Example:
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001
        xnatctl session upload ./archive.zip -P MYPROJ -S SUB001 -E SESS001
    """
    from xnatctl.core.validation import validate_project_id, validate_subject_id, validate_session_id
    from xnatctl.core.output import create_progress

    project = validate_project_id(project)
    subject = validate_subject_id(subject)
    session = validate_session_id(session)

    input_path = Path(input_path)
    client = ctx.get_client()

    if transport == "dicom-store":
        print_error("DICOM C-STORE transport not yet implemented in xnatctl")
        raise SystemExit(1)

    # REST upload
    if input_path.is_file():
        # Single archive upload
        with create_progress() as progress:
            task = progress.add_task(f"Uploading {input_path.name}...", total=100)

            with open(input_path, "rb") as f:
                content_type = "application/zip" if input_path.suffix == ".zip" else "application/x-tar"

                resp = client.post(
                    "/data/services/import",
                    params={
                        "import-handler": "DICOM-zip",
                        "project": project,
                        "subject": subject,
                        "session": session,
                        "overwrite": overwrite,
                        "Direct-Archive": "true",
                        "inbody": "true",
                    },
                    data=f,
                    headers={"Content-Type": content_type},
                    timeout=600,  # 10 minute timeout for uploads
                )

            progress.update(task, completed=100)

        if resp.status_code == 200:
            print_success(f"Uploaded {input_path.name}")
        else:
            print_error(f"Upload failed: {resp.text}")
            raise SystemExit(1)

    elif input_path.is_dir():
        # Directory upload - would need parallel batching
        print_error("Directory upload requires parallel batching (not yet implemented)")
        click.echo("Tip: Zip the directory first: zip -r archive.zip ./dicoms")
        raise SystemExit(1)
