"""``session upload`` -- REST/gradual DICOM upload of an archive or directory.

Covers the primary REST import transport: single-archive upload, parallel
directory batching, and gradual per-file upload (``--mode gradual``).
``session upload-dicom`` (C-STORE) and ``session upload-exam`` (exam-root
layout) live in their own sibling modules.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from xnatctl.cli.common import (
    Context,
    _make_alias_cb,
    _make_forwarding_alias_cb,
    global_options,
    handle_errors,
    read_password_stdin,
    reject_argv_password,
    require_auth,
    require_project_from_context,
    resolve_archive_mode_from_context,
    resolve_direct_archive_from_context,
    resolve_overwrite_from_context,
    resolve_workers_from_context,
)
from xnatctl.cli.session import session
from xnatctl.core.config import get_credentials
from xnatctl.core.output import (
    OutputFormat,
    create_progress,
    err_console,
    print_error,
    print_output,
    print_success,
)
from xnatctl.core.validation import validate_project_id, validate_session_id, validate_subject_id
from xnatctl.models.progress import TransferItemResult, TransferSummary, transfer_status

if TYPE_CHECKING:
    # Only for the progress_callback(p: UploadProgress) annotations below --
    # never constructed here -- so it stays under TYPE_CHECKING rather than
    # a real import (which `from __future__ import annotations` makes
    # unnecessary at runtime anyway).
    from xnatctl.models.progress import UploadProgress


def _echo_error_overflow(errors: list[str], *, limit: int = 5) -> None:
    """Echo the first *limit* errors to stderr, then a '... and N more' line."""
    for err in errors[:limit]:
        click.echo(f"  - {err}", err=True)
    if len(errors) > limit:
        click.echo(f"  ... and {len(errors) - limit} more errors", err=True)


@session.command("upload")
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", required=True, help="Subject ID")
@click.option(
    "--experiment",
    "-E",
    # NOT required=True, despite being required in practice: `--session` is a
    # deprecated forwarding alias for this option, and Click enforces an
    # option's own required-ness independently of whether another option's
    # callback already forwarded a value into ctx.params. With required=True
    # here, `--session SESS` alone -- exactly what a pre-deprecation script
    # does -- died with "Missing option '--experiment'" while the alias was
    # still documented as working. The check moved into the command body.
    help="Session/experiment label",
)
@click.option(
    "--session",
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("--session", "experiment"),
)
@click.option("--username", "-u", hidden=True, help="XNAT username (REST upload)")
@click.option(
    "--password",
    hidden=True,
    expose_value=False,
    is_eager=True,
    callback=reject_argv_password(
        "Use --password-stdin, set XNAT_PASS, run 'xnatctl auth login' first, "
        "or let the command prompt."
    ),
    help="REFUSED: use --password-stdin, XNAT_PASS, or the prompt",
)
@click.option(
    "--password-stdin",
    is_flag=True,
    hidden=True,
    help="Read the upload password from stdin (one line)",
)
@click.option(
    "--mode",
    type=click.Choice(["tar", "zip", "gradual"]),
    default=None,
    help="Upload mode (default: tar)",
)
@click.option(
    "--gradual",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("--gradual", "mode", "gradual"),
)
@click.option(
    "--archive-format",
    type=click.Choice(["tar", "zip"]),
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("--archive-format", "mode"),
)
@click.option(
    "--zip-to-tar/--no-zip-to-tar",
    default=False,
    hidden=True,
    help="Convert ZIP archive to TAR before REST upload",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=None,
    show_default="4 (or profile)",
    help="Parallel workers",
)
@click.option(
    "--overwrite",
    type=click.Choice(["none", "append", "delete"]),
    default=None,
    help="Overwrite mode (default: delete)",
)
@click.option(
    "--direct-archive/--prearchive",
    default=None,
    help="Direct archive or route to prearchive (default: direct). Note: --prearchive is best-effort; projects with auto-archive enabled will still auto-archive after receive.",
)
@click.option(
    "--ignore-unparsable/--no-ignore-unparsable",
    default=True,
    hidden=True,
    help="Skip unparsable DICOM files (default: yes)",
)
@click.option("--dry-run", is_flag=True, help="Preview without uploading")
@global_options
@handle_errors
@require_auth
def session_upload(
    ctx: Context,
    input_path: str,
    project: str | None,
    subject: str,
    experiment: str | None,
    username: str | None,
    password_stdin: bool,
    mode: str | None,
    zip_to_tar: bool,
    workers: int | None,
    overwrite: str | None,
    direct_archive: bool | None,
    ignore_unparsable: bool,
    dry_run: bool,
) -> None:
    """Upload DICOM session via REST import.

    Supports both single archive files and directories of DICOM files.
    For directories, files are split into N batches where N = workers.

    For DICOM C-STORE network transfer, use `session upload-dicom` instead.

    \b
    Example:
        xnatctl session upload ./archive.zip -P MYPROJ -S SUB001 -E SESS001
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --workers 16
        xnatctl session upload ./dicoms -P MYPROJ -S SUB001 -E SESS001 --mode gradual -w 40
    """
    # See the --experiment option above: Click cannot enforce this, because
    # the deprecated --session alias forwards into it via a callback.
    if not experiment:
        raise click.UsageError("Missing option '--experiment' / '-E'.")
    # A password value on argv is refused by the --password callback;
    # stdin is the only explicit per-command source. Downstream fallbacks
    # (XNAT_PASS, prompt) live in _upload_directory_parallel.
    password = read_password_stdin("--password-stdin") if password_stdin else None

    project = require_project_from_context(ctx, project)
    mode = resolve_archive_mode_from_context(ctx, mode)
    workers = resolve_workers_from_context(ctx, workers)
    overwrite = resolve_overwrite_from_context(ctx, overwrite)
    direct_archive = resolve_direct_archive_from_context(ctx, direct_archive)

    # Map mode to internal gradual/archive_format
    gradual = mode == "gradual"
    archive_format = mode if mode in ("tar", "zip") else "tar"

    session = experiment
    project = validate_project_id(project)
    subject = validate_subject_id(subject)
    session = validate_session_id(session)

    source_path = Path(input_path)

    # Dry run handling
    if dry_run:
        if ctx.output_format == OutputFormat.JSON:
            plan: dict[str, object] = {
                "operation": "upload",
                "dry_run": True,
                "source": str(source_path),
                "project": project,
                "subject": subject,
                "session_id": session,
                "mode": mode,
                "workers": workers,
            }
            if not gradual:
                plan["overwrite"] = overwrite
                plan["direct_archive"] = direct_archive
            print_output(plan, format=OutputFormat.JSON)
            return
        click.echo("[DRY-RUN] Would upload with the following settings:", err=True)
        click.echo(f"  Source: {source_path}", err=True)
        click.echo(f"  Project: {project}", err=True)
        click.echo(f"  Subject: {subject}", err=True)
        click.echo(f"  Session: {session}", err=True)
        click.echo(f"  Mode: {mode}", err=True)
        click.echo(f"  Workers: {workers}", err=True)
        if not gradual:
            click.echo(f"  Overwrite: {overwrite}", err=True)
            click.echo(f"  Direct archive: {direct_archive}", err=True)
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
            direct_archive=direct_archive,
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


def _upload_gradual_dicom(
    ctx: Context,
    source_path: Path,
    project: str,
    subject: str,
    session: str,
    workers: int = 4,
    direct_archive: bool = True,
) -> None:
    """Upload DICOM files using gradual-DICOM handler (parallel per-file)."""
    # Deferred: tests monkeypatch xnatctl.services.upload.UploadService to
    # intercept the lookup; a module-scope import would bind the real class
    # before the patch runs (see tests/test_session_upload.py).
    from xnatctl.services.upload import UploadService

    gradual_start = time.time()
    client = ctx.get_client()
    service = UploadService(client)

    progress_counter = 0

    def progress_callback(p: UploadProgress) -> None:
        nonlocal progress_counter
        progress_counter += 1
        if p.phase.value == "uploading" and progress_counter % 100 != 0:
            return
        click.echo(f"  [{p.phase.value}] {p.message}", err=True)

    try:
        summary = service.upload_dicom_gradual(
            source_path=source_path,
            project=project,
            subject=subject,
            session=session,
            workers=workers,
            direct_archive=direct_archive,
            progress_callback=progress_callback if not ctx.quiet else None,
        )
    except Exception as e:  # noqa: BLE001  # stays broad on purpose -- corrupt-ZIP/permission errors still produce a JSON summary before propagating (see comment above)
        # Not narrowed to ValueError/FileNotFoundError (the two documented
        # raises): a corrupt ZIP or an unreadable source directory surfaces
        # as BadZipFile/PermissionError, and those must still produce a
        # JSON summary in JSON mode rather than exit silently on stdout.
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                session_id=session,
                project=project,
                source=str(source_path),
                files=None,
                bytes=None,
                duration_seconds=round(time.time() - gradual_start, 3),
                status="failed",
                items=[TransferItemResult(id="gradual-dicom", status="failed", error=str(e))],
            ).emit()
        if isinstance(e, (ValueError, FileNotFoundError)):
            # Byte-identical to this function's pre-existing human-mode
            # behavior for its two documented raises: a plain one-line
            # error, not @handle_errors' "Unexpected error: ..." wrapper.
            print_error(str(e))
            raise SystemExit(1) from e
        # Anything else (BadZipFile, PermissionError, ...) re-raises
        # unchanged so @handle_errors classifies its exit code normally.
        raise

    if ctx.output_format == OutputFormat.JSON:
        TransferSummary(
            operation="upload",
            session_id=session,
            project=project,
            source=str(source_path),
            files=summary.total_files if summary.success else None,
            bytes=round(summary.total_size_mb * 1024 * 1024)
            if summary.success and summary.total_size_mb
            else None,
            duration_seconds=round(summary.duration, 3),
            status=transfer_status(
                succeeded=summary.succeeded, failed=summary.failed, success=summary.success
            ),
            items=[
                TransferItemResult(
                    id="gradual-dicom",
                    status="success" if summary.success else "failed",
                    error=summary.errors[0] if not summary.success and summary.errors else None,
                )
            ],
        ).emit()
    else:
        if summary.success:
            print_success(f"Uploaded {summary.succeeded} files via gradual-DICOM")
        else:
            print_error(
                f"Uploaded {summary.succeeded}/{summary.total} files ({summary.failed} failed)"
            )
            _echo_error_overflow(summary.errors)

    # Exit code must not depend on the output format: a failed upload has to
    # return nonzero under -o json too, or automation reports success.
    if not summary.success:
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
    # Deferred: tests patch xnatctl.services.upload.upload_archive_or_raise
    # to intercept the lookup; a module-scope import would bind the real
    # function before the patch runs (see tests/test_transfer_summary_json.py).
    from xnatctl.services.upload import upload_archive_or_raise

    upload_start = time.time()
    client = ctx.get_client()

    # Only show progress for table output and not quiet
    show_progress = ctx.output_format == OutputFormat.TABLE and not ctx.quiet

    try:
        if show_progress:
            with create_progress() as progress:
                task = progress.add_task(f"Uploading {archive_path.name}...", total=100)

                upload_archive_or_raise(
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
            upload_archive_or_raise(
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
    except Exception as exc:  # noqa: BLE001  # emits JSON failure summary before re-raising unchanged
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                session_id=session,
                project=project,
                source=str(archive_path),
                files=None,
                bytes=None,
                duration_seconds=round(time.time() - upload_start, 3),
                status="failed",
                items=[TransferItemResult(id=archive_path.name, status="failed", error=str(exc))],
            ).emit()
        raise

    if ctx.output_format == OutputFormat.JSON:
        TransferSummary(
            operation="upload",
            session_id=session,
            project=project,
            source=str(archive_path),
            files=1,
            bytes=archive_path.stat().st_size,
            duration_seconds=round(time.time() - upload_start, 3),
            status="success",
            items=[TransferItemResult(id=archive_path.name, status="success")],
        ).emit()
    else:
        print_success(f"Uploaded {archive_path.name}")


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
    # Deferred: see the comment on the UploadService import in
    # _upload_gradual_dicom above (test-monkeypatch seam).
    from xnatctl.services.upload import UploadService

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
    upload_start = time.time()

    def _emit_failed_summary(exc: BaseException) -> None:
        if ctx.output_format != OutputFormat.JSON:
            return
        TransferSummary(
            operation="upload",
            session_id=session,
            project=project,
            source=str(source_dir),
            files=None,
            bytes=None,
            duration_seconds=round(time.time() - upload_start, 3),
            status="failed",
            items=[TransferItemResult(id="batches", status="failed", error=str(exc))],
        ).emit()

    try:
        if show_progress:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                transient=True,
                # A bare Progress() builds its own Console, blind to the
                # --no-color flag and CLICOLOR=0 state on the shared consoles.
                console=err_console,
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
    except Exception as exc:  # noqa: BLE001  # emits JSON failure summary before re-raising unchanged
        _emit_failed_summary(exc)
        raise

    # Output results. `errors` isn't keyed to batch index, so this is one
    # aggregate item for the whole directory upload -- not one fabricated
    # item per batch, which would misattribute which batch each error
    # belongs to.
    if ctx.output_format == OutputFormat.JSON:
        TransferSummary(
            operation="upload",
            session_id=session,
            project=project,
            source=str(source_dir),
            # `total_files`/`total_size_mb` count everything the run
            # attempted across all batches, not only what succeeded -- only
            # trustworthy as "transferred" when every batch did.
            files=summary.total_files if summary.success else None,
            bytes=round(summary.total_size_mb * 1024 * 1024) if summary.success else None,
            duration_seconds=round(summary.duration, 3),
            status=transfer_status(
                succeeded=summary.batches_succeeded,
                failed=summary.batches_failed,
                success=summary.success,
            ),
            items=[
                TransferItemResult(
                    id="batches",
                    status="success" if summary.success else "failed",
                    error="; ".join(summary.errors) if summary.errors else None,
                )
            ],
        ).emit()
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
            _echo_error_overflow(summary.errors)

    # Format-independent failure exit: -o json must also return nonzero.
    if not summary.success:
        raise SystemExit(1)
