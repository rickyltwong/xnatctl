"""``session upload-exam`` -- upload an exam root (DICOM + session resources).

Handles the exam-root folder convention: DICOM anywhere under the root,
top-level directories become session-level resources, top-level non-DICOM
files become misc attachments. Delegates the actual planning and upload work
to :class:`~xnatctl.services.exam_upload.ExamUploadService`.
"""

from __future__ import annotations

from pathlib import Path

import click

from xnatctl.cli.common import (
    Context,
    _make_forwarding_alias_cb,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
    resolve_direct_archive_from_context,
    resolve_workers_from_context,
)
from xnatctl.cli.session import session
from xnatctl.core.output import (
    OutputFormat,
    print_output,
    print_success,
    print_warning,
)
from xnatctl.core.timeouts import DEFAULT_ARCHIVE_WAIT_SECONDS
from xnatctl.core.validation import validate_project_id, validate_session_id, validate_subject_id
from xnatctl.services.exam_upload import (
    ExamOutcome,
    ExamUploadResult,
    ExamUploadService,
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
    experiment: str | None,
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
    # See the --experiment option above: Click cannot enforce this, because
    # the deprecated --session alias forwards into it via a callback.
    if not experiment:
        raise click.UsageError("Missing option '--experiment' / '-E'.")
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
