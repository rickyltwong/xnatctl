"""Session upload commands for xnatctl (upload / upload-exam / upload-dicom)."""

from __future__ import annotations

import time
from pathlib import Path

import click

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
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_output,
    print_success,
    print_warning,
)
from xnatctl.core.timeouts import DEFAULT_ARCHIVE_WAIT_SECONDS
from xnatctl.models.progress import TransferItemResult, TransferSummary, transfer_status
from xnatctl.services.exam_upload import (
    ExamOutcome,
    ExamUploadResult,
    ExamUploadService,
)


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
@click.option("--experiment", "-E", required=True, help="Session/experiment label")
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
    experiment: str,
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
    from xnatctl.core.validation import (
        validate_project_id,
        validate_session_id,
        validate_subject_id,
    )

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


def _render_exam_upload_result(ctx: Context, result: ExamUploadResult) -> None:
    """Render an exam-upload result to JSON or the table summary, unchanged."""
    if ctx.output_format == OutputFormat.JSON:
        print_output(result.to_json_dict(), format=OutputFormat.JSON)
        return

    dicom_msg = (
        "DICOM skipped"
        if result.attach_only
        else f"DICOM uploaded {result.dicom_uploaded}/{result.dicom_total}"
    )

    if result.outcome is ExamOutcome.NOT_ARCHIVED:
        waited = f" after waiting {result.wait_timeout}s" if result.wait_for_archive else ""
        print_warning(
            f"{dicom_msg}; session '{result.session}' not archived yet{waited}. "
            f"{result.pending} resource item(s) not attached -- re-run once archived:"
            f"\n  {result.rerun}"
        )
        return

    if result.outcome is ExamOutcome.NO_RESOURCES:
        resources_msg = (
            "resources skipped" if result.skip_resources else "resources attached 0 dirs + 0 files"
        )
    else:  # COMPLETE
        resources_msg = (
            f"resources attached {result.attached_resource_dirs} dirs "
            f"+ {result.attached_misc_files} files"
        )
    print_success(f"Upload-exam complete: {dicom_msg}; {resources_msg}")


@session.command("upload-exam")
@click.argument("exam_root", type=click.Path(exists=True, file_okay=False))
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@click.option("--subject", "-S", required=True, help="Subject ID")
@click.option("--experiment", "-E", required=True, help="Session/experiment label")
@click.option(
    "--session",
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("--session", "experiment"),
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
    "--misc-label",
    default="MISC",
    show_default=True,
    hidden=True,
    help="Resource label to use for top-level misc files",
)
@click.option(
    "--skip-resources",
    is_flag=True,
    hidden=True,
    help="Skip attaching top-level resource dirs and misc files",
)
@click.option(
    "--attach-only",
    is_flag=True,
    hidden=True,
    help="Attach resources only (skip DICOM upload)",
)
@click.option(
    "--direct-archive/--prearchive",
    default=None,
    help="Direct archive or route to prearchive (default: direct). Note: --prearchive is best-effort; projects with auto-archive enabled will still auto-archive after receive.",
)
@click.option(
    "--wait",
    type=click.IntRange(min=0),
    default=DEFAULT_ARCHIVE_WAIT_SECONDS,
    show_default=True,
    help="Seconds to wait for archiving before attaching resources (0 = skip)",
)
@click.option(
    "--wait-interval",
    type=click.IntRange(min=1),
    default=5,
    hidden=True,
    help="Seconds between archive checks",
)
@click.option(
    "--wait-timeout",
    type=click.IntRange(min=0),
    hidden=True,
    expose_value=False,
    callback=lambda ctx, param, value: (ctx.params.update({"wait": value}) or value)
    if value is not None
    and param.name
    and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
    else value,
)
@click.option(
    "--wait-for-archive/--no-wait-for-archive",
    default=None,
    hidden=True,
    expose_value=False,
    callback=lambda ctx, param, value: (
        ctx.params.update({"wait": DEFAULT_ARCHIVE_WAIT_SECONDS if value else 0}) or value
    )
    if value is not None
    and param.name
    and ctx.get_parameter_source(param.name) == click.core.ParameterSource.COMMANDLINE
    else value,
)
@click.option("--dry-run", is_flag=True, help="Preview without uploading")
@global_options
@handle_errors
@require_auth
def session_upload_exam(
    ctx: Context,
    exam_root: str,
    project: str | None,
    subject: str,
    experiment: str,
    workers: int | None,
    misc_label: str,
    skip_resources: bool,
    attach_only: bool,
    direct_archive: bool | None,
    wait: int,
    wait_interval: int,
    dry_run: bool,
) -> None:
    """Upload an exam root (DICOM + session resources).

    \b
    Exam roots follow a common folder convention:
    - DICOM files may appear anywhere under the root (recursive)
    - Top-level directories without DICOM-like files are treated as session-level
      resources (label = directory name)
    - Top-level non-DICOM files are treated as misc attachments under --misc-label
    """
    from xnatctl.core.validation import (
        validate_project_id,
        validate_session_id,
        validate_subject_id,
    )

    project = require_project_from_context(ctx, project)
    workers = resolve_workers_from_context(ctx, workers)
    direct_archive = resolve_direct_archive_from_context(ctx, direct_archive)

    session = experiment
    project = validate_project_id(project)
    subject = validate_subject_id(subject)
    session = validate_session_id(session)

    service = ExamUploadService(ctx.get_client())
    plan = service.plan(Path(exam_root), misc_label)

    if dry_run:
        click.echo("[DRY-RUN] Would upload exam with the following settings:", err=True)
        click.echo(f"  Exam root: {plan.exam_root}", err=True)
        click.echo(f"  Project: {project}", err=True)
        click.echo(f"  Subject: {subject}", err=True)
        click.echo(f"  Session: {session}", err=True)
        click.echo(f"  Workers: {workers}", err=True)
        click.echo(f"  Direct archive: {direct_archive}", err=True)
        click.echo(f"  Resource dirs ({len(plan.resource_labels)}):", err=True)
        for label in plan.resource_labels:
            click.echo(f"    - {label}", err=True)
        click.echo(f"  Misc label: {plan.misc_label}", err=True)
        return

    result = service.upload_exam(
        plan,
        project=project,
        subject=subject,
        session=session,
        workers=workers,
        direct_archive=direct_archive,
        skip_resources=skip_resources,
        attach_only=attach_only,
        wait=wait,
        wait_interval=wait_interval,
    )

    if result.error_message is not None:
        # NO_DICOM / DICOM_FAILED surface as the same ClickException (exit 1)
        # the inline command has always raised.
        raise click.ClickException(result.error_message)

    _render_exam_upload_result(ctx, result)


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
    from xnatctl.models.progress import UploadProgress
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
    except Exception as e:
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
    from xnatctl.core.output import create_progress
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
    except Exception as exc:
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
    from xnatctl.core.config import get_credentials
    from xnatctl.models.progress import UploadProgress
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
    except Exception as exc:
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


def _upload_dicom_store(
    ctx: Context,
    source_path: Path,
    dicom_host: str | None,
    dicom_port: int,
    called_aet: str | None,
    calling_aet: str,
    dicom_workers: int,
    tls: bool = False,
    tls_ca_bundle: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    """Upload via DICOM C-STORE protocol."""

    def _emit_preflight_json(message: str) -> None:
        # These guards run before the upload_dicom_store try/except below,
        # so without this they'd exit with no JSON at all in JSON mode --
        # the same silent-failure shape the try/except fixes for everything
        # downstream of it. Only the JSON side: each guard below prints its
        # own human-mode text (a plain `print_error` for the ones that raise
        # SystemExit directly, or Click's own usage-error rendering for the
        # ones that raise UsageError -- printing both here would double it).
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                source=str(source_path),
                files=None,
                duration_seconds=0.0,
                status="failed",
                items=[TransferItemResult(id=str(source_path), status="failed", error=message)],
            ).emit()

    if not dicom_host:
        message = "DICOM C-STORE requires --dicom-host or XNAT_DICOM_HOST environment variable"
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    if not called_aet:
        message = (
            "DICOM C-STORE requires --called-aet or XNAT_DICOM_CALLED_AET environment variable"
        )
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    if not source_path.is_dir():
        message = "DICOM C-STORE requires a directory of DICOM files, not an archive"
        _emit_preflight_json(message)
        print_error(message)
        raise SystemExit(1)

    from xnatctl.services.upload import UploadService

    if tls_key and not tls_cert:
        message = "--tls-key requires --tls-cert"
        _emit_preflight_json(message)
        raise click.UsageError(message)
    if (tls_ca_bundle or tls_cert) and not tls:
        message = "--tls-ca-bundle/--tls-cert/--tls-key have no effect without --tls"
        _emit_preflight_json(message)
        raise click.UsageError(message)

    if not ctx.quiet:
        scheme = "TLS" if tls else "plaintext"
        click.echo(
            f"Sending DICOM files to {dicom_host}:{dicom_port} ({called_aet}) over {scheme}...",
            err=True,
        )
        if not tls:
            # Said once, plainly, on stderr. Sites that mean to do this are not
            # helped by a warning; sites that did not know need to be told.
            click.echo(
                "  Note: DICOM C-STORE is unencrypted. Use --tls if the server supports it.",
                err=True,
            )

    client = ctx.get_client()
    service = UploadService(client)

    # No -P/-S/-E on this command: C-STORE is native DICOM network transfer,
    # and where each file lands is decided by the receiver's own routing
    # rules, not by anything xnatctl passes -- there is no session/project to
    # report (see the C-STORE section of docs/uploading.rst).
    upload_start = time.time()
    try:
        summary = service.upload_dicom_store(
            dicom_root=source_path,
            host=dicom_host,
            called_aet=called_aet,
            port=dicom_port,
            calling_aet=calling_aet,
            workers=dicom_workers,
            cleanup=True,
            tls=tls,
            tls_ca_bundle=tls_ca_bundle,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
    except Exception as exc:
        if ctx.output_format == OutputFormat.JSON:
            TransferSummary(
                operation="upload",
                source=str(source_path),
                files=None,
                duration_seconds=round(time.time() - upload_start, 3),
                status="failed",
                items=[
                    TransferItemResult(
                        id=f"{dicom_host}:{dicom_port}", status="failed", error=str(exc)
                    )
                ],
            ).emit()
        raise

    if ctx.output_format == OutputFormat.JSON:
        TransferSummary(
            operation="upload",
            source=str(source_path),
            # `sent` is the service's own successfully-transferred count,
            # distinct from `total_files` (everything scanned, sent or not).
            files=summary.sent,
            duration_seconds=round(time.time() - upload_start, 3),
            status=transfer_status(
                succeeded=summary.sent, failed=summary.failed, success=summary.success
            ),
            items=[
                TransferItemResult(
                    id=f"{dicom_host}:{dicom_port}",
                    status="success" if summary.success else "failed",
                    error=None
                    if summary.success
                    else f"{summary.failed}/{summary.total_files} files failed",
                )
            ],
        ).emit()
    else:
        if summary.success:
            print_success(f"Sent {summary.sent}/{summary.total_files} DICOM files")
        else:
            print_error(
                f"DICOM C-STORE completed with errors: "
                f"{summary.failed}/{summary.total_files} files failed"
            )
            click.echo(f"Check logs in: {summary.log_dir}", err=True)

    # Format-independent failure exit: -o json must also return nonzero.
    if not summary.success:
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
    hidden=True,
    envvar="XNAT_DICOM_CALLING_AET",
    help="Calling AE Title (env: XNAT_DICOM_CALLING_AET)",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=None,
    show_default="4 (or profile)",
    help="Parallel DICOM C-STORE associations",
)
@click.option(
    "--tls",
    is_flag=True,
    envvar="XNAT_DICOM_TLS",
    help="Encrypt the DICOM association (env: XNAT_DICOM_TLS)",
)
@click.option(
    "--tls-ca-bundle",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_CA_BUNDLE",
    help="PEM file of CAs to trust for --tls (default: system store)",
)
@click.option(
    "--tls-cert",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_CERT",
    help="Client certificate, for SCPs requiring mutual TLS",
)
@click.option(
    "--tls-key",
    type=click.Path(exists=True, dir_okay=False),
    envvar="XNAT_DICOM_TLS_KEY",
    help="Private key for --tls-cert",
)
@click.option("--dry-run", is_flag=True, help="Preview without sending")
@global_options
@handle_errors
@require_auth
def session_upload_dicom(
    ctx: Context,
    input_path: str,
    host: str,
    called_aet: str,
    port: int,
    calling_aet: str,
    workers: int | None,
    tls: bool,
    tls_ca_bundle: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    dry_run: bool,
) -> None:
    """Upload DICOM files via C-STORE network protocol.

    \b
    Example:
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT --port 8104
        xnatctl session upload-dicom ./dicoms --host xnat.example.org --called-aet XNAT -w 8
    """
    source_path = Path(input_path)

    workers = resolve_workers_from_context(ctx, workers)

    if dry_run:
        if ctx.output_format == OutputFormat.JSON:
            print_output(
                {
                    "operation": "upload",
                    "dry_run": True,
                    "source": str(source_path),
                    "host": host,
                    "port": port,
                    "called_aet": called_aet,
                    "calling_aet": calling_aet,
                    "workers": workers,
                    "transport": "tls" if tls else "plaintext",
                },
                format=OutputFormat.JSON,
            )
            return
        click.echo("[DRY-RUN] Would send DICOM files via C-STORE:", err=True)
        click.echo(f"  Source: {source_path}", err=True)
        click.echo(f"  Host: {host}:{port}", err=True)
        click.echo(f"  Called AET: {called_aet}", err=True)
        click.echo(f"  Calling AET: {calling_aet}", err=True)
        click.echo(f"  Workers: {workers}", err=True)
        click.echo(f"  Transport: {'TLS' if tls else 'plaintext'}", err=True)
        return

    _upload_dicom_store(
        ctx=ctx,
        source_path=source_path,
        dicom_host=host,
        dicom_port=port,
        called_aet=called_aet,
        calling_aet=calling_aet,
        dicom_workers=workers,
        tls=tls,
        tls_ca_bundle=tls_ca_bundle,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
