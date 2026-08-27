"""Scan commands for xnatctl."""

from __future__ import annotations

from concurrent.futures import as_completed
from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    _make_forwarding_alias_cb,
    apply_filter,
    apply_sort_limit,
    batch_option,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    list_options,
    parallel_options,
    require_auth,
    resolve_columns,
    resolve_workers_from_context,
)
from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.core.output import (
    err_console,
    print_error,
    print_output,
)
from xnatctl.core.validation import (
    validate_scan_id,
    validate_scan_ids_input,
    validate_session_id,
)
from xnatctl.models.hierarchy import ExperimentRef, ScanRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.resources import ResourceService
from xnatctl.services.scans import ScanService


def _inspect_experiment(
    hierarchy: HierarchyService, experiment_ref: ExperimentRef
) -> tuple[ExperimentRef, str | None]:
    """Inspect the parent experiment and derive canonical ID, subject and xsiType.

    Nested scan endpoints are most reliable when addressed with the canonical
    experiment ID. This helper also extracts the experiment xsiType so scan list
    queries can request the correct non-imaging scan subtype when needed.

    The owning subject is carried forward too, because XNAT does not route
    sub-resource suffixes on ``/data/projects/{P}/experiments/{E}``: it answers
    ``/scans`` -- or any other suffix -- with the parent experiment document
    instead of the sub-resource. Inserting the subject segment yields
    ``/data/projects/{P}/subjects/{S}/experiments/{E}/scans``, which routes
    correctly and still enforces the project ACL.

    Args:
        hierarchy: Hierarchy service.
        experiment_ref: Parent experiment reference.

    Returns:
        Tuple of (canonical experiment ref, experiment xsiType).
    """
    try:
        data = hierarchy.get_experiment_json(experiment_ref)
    except ResourceNotFoundError:
        return experiment_ref, None
    if not isinstance(data, dict):
        return experiment_ref, None

    resolved_id: str | None = None
    resolved_subject: str | None = None
    session_xsi: str | None = None

    def _looks_like_experiment(fields: dict[str, Any]) -> bool:
        return any(
            key in fields
            for key in (
                "project",
                "subject_ID",
                "subject_label",
                "date",
                "xsiType",
            )
        )

    item = hierarchy.extract_first_item(data)
    if item is not None:
        fields, meta = item
        session_xsi = str(fields.get("xsiType") or meta.get("xsi:type") or "") or None
        if _looks_like_experiment(fields) or "xsi:type" in meta:
            resolved_id = str(fields.get("ID") or fields.get("id") or "") or None
            resolved_subject = str(fields.get("subject_ID") or "") or None
    else:
        rows = hierarchy.extract_rows(data)
        if rows:
            session_xsi = str(rows[0].get("xsiType") or "") or None
            if _looks_like_experiment(rows[0]):
                resolved_id = str(rows[0].get("ID") or "") or None
                resolved_subject = str(rows[0].get("subject_ID") or "") or None

    # Carry project/subject scope forward so nested scan/resource calls stay
    # on project-scoped URLs (respects project ACLs, matches old behavior).
    # Use the resolved accession ID as the experiment with experiment_is_label=False.
    subject = experiment_ref.subject
    subject_is_label = experiment_ref.subject_is_label
    # The subject segment is what makes the nested suffix route at all (see the
    # docstring), but XNAT only accepts it under a project.
    if experiment_ref.project_id and not subject and resolved_subject:
        subject = resolved_subject
        subject_is_label = False

    if resolved_id or subject != experiment_ref.subject:
        canonical_ref = ExperimentRef(
            experiment=resolved_id or experiment_ref.experiment,
            project_id=experiment_ref.project_id,
            subject=subject,
            experiment_is_label=(False if resolved_id else experiment_ref.experiment_is_label),
            subject_is_label=subject_is_label,
        )
    else:
        canonical_ref = experiment_ref
    return canonical_ref, session_xsi


def _run_scan_deletes(
    scan_svc: ScanService,
    experiment_ref: ExperimentRef,
    scan_ids: list[str],
    *,
    workers: int,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Delete every scan in ``scan_ids``, returning (deleted, failed).

    Per-scan failures are collected rather than raised so one bad scan
    cannot abandon the rest of the list. That isolation applies only when
    there IS a rest of the list: with a single scan the exception
    propagates, because ``@handle_errors`` needs to see the typed error to
    pick the right exit code (permission denied is not a generic failure)
    and to record the real exception class in the audit trail.

    Args:
        scan_svc: Bound scan service.
        experiment_ref: Parent experiment the scans hang off.
        scan_ids: Scans to delete; must be non-empty.
        workers: Parallel worker ceiling. A single scan, or ``workers <= 1``,
            runs serially.

    Returns:
        ``(deleted_ids, [(scan_id, error_message), ...])``.
    """
    isolate_failures = len(scan_ids) > 1

    def delete_scan(scan_id: str) -> tuple[str, bool, str]:
        """Delete a scan and return status and error message."""
        try:
            resp = scan_svc.delete_scan_ref(ScanRef(experiment=experiment_ref, scan_id=scan_id))
            return scan_id, resp.status_code in (200, 204), ""
        except Exception as e:  # noqa: BLE001  # per-scan isolation across a multi-scan list
            if not isolate_failures:
                raise
            return scan_id, False, str(e)

    deleted: list[str] = []
    failed: list[tuple[str, str]] = []

    def record(outcome: tuple[str, bool, str]) -> None:
        scan_id, success, error = outcome
        if success:
            deleted.append(scan_id)
        else:
            failed.append((scan_id, error))

    if workers > 1 and len(scan_ids) > 1:
        with cancellable_pool(min(workers, len(scan_ids))) as (executor, _token):
            futures = [executor.submit(delete_scan, sid) for sid in scan_ids]
            for future in as_completed(futures):
                record(future.result())
    else:
        for scan_id in scan_ids:
            record(delete_scan(scan_id))

    return deleted, failed


def _require_scan_addressable(ref: ExperimentRef) -> None:
    """Refuse to issue a scan request that XNAT would apply to the experiment.

    ``HierarchyService.routable_scan_parent`` rewrites a project-scoped
    experiment *ID* to the flat form so its ``/scans`` suffix routes. A genuine
    *label* has no such escape: it cannot drop the project, so the URL stays on
    the prefix XNAT answers with the experiment document. Failing here beats
    reporting an empty scan list, or aiming a DELETE at the session.

    Reached only when inspection could not resolve the label to an accession
    ID -- on the happy path ``_inspect_experiment`` has already done so.
    """
    # Unroutable exactly when the builder could not rewrite it to a form
    # that addresses a scan.
    if ref.project_id and not ref.subject and HierarchyService.routable_scan_parent(ref) == ref:
        raise click.ClickException(
            f"Could not resolve experiment '{ref.experiment}' in project "
            f"'{ref.project_id}' to an accession ID, and scans cannot be "
            "addressed by experiment label alone. Retry with -S/--subject, or "
            "pass the accession ID to -E."
        )


def _build_experiment_ref(
    project: str | None, subject: str | None, session_id: str
) -> ExperimentRef:
    """Build an experiment reference for scan operations.

    Args:
        project: Project ID (optional).
        subject: Subject ID/label (optional, requires project).
        session_id: Experiment ID or label.

    Returns:
        Experiment reference.

    Raises:
        click.ClickException: If subject is given without a resolved project.
    """
    if subject and not project:
        raise click.ClickException(
            "-S/--subject requires -P/--project (or default_project in profile)"
        )
    return ExperimentRef(
        experiment=session_id,
        project_id=project,
        subject=subject,
        experiment_is_label=project is not None,
        subject_is_label=subject is not None,
    )


@click.group()
def scan() -> None:
    """Manage XNAT scans."""
    pass


@scan.command("list")
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
@click.option(
    "--subject",
    "-S",
    help="Subject ID/label (narrows experiment lookup, requires -P)",
)
@list_options
@global_options
@handle_errors
@require_auth
def scan_list(
    ctx: Context,
    session_id: str,
    project: str | None,
    subject: str | None,
    filter_expr: str | None,
    limit: int | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """List scans in a session.

    \b
    Example:
        xnatctl scan list -E XNAT_E00001
        xnatctl scan list -E XNAT_E00001 -o json
        xnatctl scan list -E XNAT_E00001 -q  # IDs only
        xnatctl scan list -E SESSION_LABEL -P MYPROJ
        xnatctl scan list -P MYPROJ -S SUB001 -E SESSION_LABEL
        xnatctl scan list -E XNAT_E00001 --filter 'quality:usable' --sort-by id
    """
    session_id = validate_session_id(session_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)

    results = hierarchy.list_scan_rows(experiment_ref, session_xsi)

    # Transform for output
    scans = []
    for r in results:
        scans.append(
            {
                "id": r.get("ID", ""),
                "type": r.get("type", ""),
                "series_description": r.get("series_description", ""),
                "quality": r.get("quality", ""),
                "frames": r.get("frames", ""),
                "note": r.get("note", ""),
            }
        )

    scans = apply_filter(scans, filter_expr)
    scans = apply_sort_limit(scans, sort_by, limit)

    default_columns = ["id", "type", "series_description", "quality", "frames"]
    print_output(
        scans,
        format=ctx.output_format,
        columns=resolve_columns(default_columns, columns),
        column_labels={
            "id": "ID",
            "type": "Type",
            "series_description": "Series Description",
            "quality": "Quality",
            "frames": "Frames",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@scan.command("show")
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
@click.option(
    "--subject",
    "-S",
    help="Subject ID/label (narrows experiment lookup, requires -P)",
)
@click.argument("scan_id")
@global_options
@handle_errors
@require_auth
def scan_show(
    ctx: Context, session_id: str, project: str | None, subject: str | None, scan_id: str
) -> None:
    """Show scan details.

    \b
    Example:
        xnatctl scan show -E XNAT_E00001 1
        xnatctl scan show -E SESSION_LABEL 1 -P MYPROJ
        xnatctl scan show -P MYPROJ -S SUB001 -E SESSION_LABEL 1
    """
    session_id = validate_session_id(session_id)
    scan_id = validate_scan_id(scan_id)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, _session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)
    scan_ref = ScanRef(experiment=experiment_ref, scan_id=scan_id)
    scan_data = ScanService(client).get_scan_document(scan_ref)

    if not scan_data:
        print_error(f"Scan not found: {scan_id}")
        raise SystemExit(1)

    # Get resources
    try:
        resources = ResourceService(client).list_rows(scan_ref)
    except (XNATCtlError, ValueError) as exc:
        click.echo(f"Warning: could not list resources: {exc}", err=True)
        resources = []

    output = {
        "id": scan_data.get("ID", ""),
        "type": scan_data.get("type", ""),
        "series_description": scan_data.get("series_description", ""),
        "quality": scan_data.get("quality", ""),
        "frames": scan_data.get("frames", ""),
        "note": scan_data.get("note", ""),
        "resources": [r.get("label", "") for r in resources],
    }

    print_output(
        output,
        format=ctx.output_format,
        quiet=ctx.quiet,
        id_field="id",
    )


def _resolve_scan_ids(scans: tuple[str, ...], batch_ids: list[str] | None) -> list[str] | None:
    """Resolve the scan IDs to delete from ``--scans`` or ``--batch``.

    The two are mutually exclusive; ``--batch`` supplies explicit scan IDs
    (no ``'*'`` wildcard). ``--scans`` is repeatable (``--scans 1 --scans
    2``) and each occurrence also accepts a comma-separated list (``--scans
    1,2,3``) or ``'*'`` for all -- the two forms combine freely.

    Returns:
        Explicit scan IDs, or ``None`` for "all scans" (``--scans '*'`` only).

    Raises:
        click.UsageError: Neither or both of ``--scans``/``--batch`` were given.
    """
    # `batch_ids is not None` is presence ("--batch was given"), not
    # truthiness of the parsed list -- @batch_option already refuses an
    # empty-but-given batch, but these checks stay presence-based rather
    # than relying on that guarantee, so `--scans '*'` can never slip past
    # mutual exclusion (and fall through to the wildcard) just because the
    # batch happened to be empty.
    scans_value = ",".join(scans) if scans else None
    if scans_value and batch_ids is not None:
        raise click.UsageError("provide --scans or --batch, not both")
    if batch_ids is not None:
        return [validate_scan_id(s) for s in batch_ids]
    if scans_value:
        return validate_scan_ids_input(scans_value)
    raise click.UsageError("provide --scans or --batch")


@scan.command("delete")
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
@click.option(
    "--subject",
    "-S",
    help="Subject ID/label (narrows experiment lookup, requires -P)",
)
@click.option(
    "--scans",
    multiple=True,
    required=False,
    # Eager so it is processed before the deprecated -s alias, which merges
    # into this option's value (see _make_forwarding_alias_cb).
    is_eager=True,
    help="Scan IDs: repeatable (--scans 1 --scans 2), comma-separated "
    "(--scans 1,2,3), or '*' for all.",
)
@click.option(
    "-s",
    "legacy_scans_s",
    multiple=True,
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("-s", "scans"),
)
@batch_option
@confirm_destructive("Delete these scans?")
@parallel_options
@global_options
@handle_errors
@require_auth
def scan_delete(
    ctx: Context,
    session_id: str,
    project: str | None,
    subject: str | None,
    scans: tuple[str, ...],
    batch_ids: list[str] | None,
    dry_run: bool,
    workers: int | None,
) -> None:
    """Delete scans from a session.

    \b
    Example:
        xnatctl scan delete -E XNAT_E00001 --scans 1,2,3
        xnatctl scan delete -E XNAT_E00001 --scans 1 --scans 2 --scans 3
        xnatctl scan delete -E XNAT_E00001 --scans '*'  # Delete all
        xnatctl scan delete -E XNAT_E00001 --scans 1,2 --dry-run
        xnatctl scan delete -E SESSION_LABEL --scans 1,2,3 -P MYPROJ
        xnatctl scan list -E XNAT_E00001 -q | xnatctl scan delete -E XNAT_E00001 --batch - --yes
    """
    session_id = validate_session_id(session_id)
    scan_ids = _resolve_scan_ids(scans, batch_ids)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    hierarchy = HierarchyService(client)
    source_ref = _build_experiment_ref(project, subject, session_id)
    experiment_ref, session_xsi = _inspect_experiment(hierarchy, source_ref)
    _require_scan_addressable(experiment_ref)

    # If wildcard, get all scan IDs
    if scan_ids is None:
        results = hierarchy.list_scan_rows(experiment_ref, session_xsi)
        scan_ids = [r.get("ID", "") for r in results if r.get("ID")]

    if not scan_ids:
        print_error("No scans to delete")
        raise SystemExit(1)

    if dry_run:
        click.echo(f"[DRY-RUN] Would delete {len(scan_ids)} scans:", err=True)
        for sid in scan_ids:
            click.echo(f"  - {sid}", err=True)
        return

    deleted, failed = _run_scan_deletes(
        ScanService(client),
        experiment_ref,
        scan_ids,
        workers=resolve_workers_from_context(ctx, workers),
    )

    if deleted:
        noun = "scan" if len(deleted) == 1 else "scans"
        # Use highlight=False so Rich does not turn the integer into a
        # styled token; tests assert on the raw "Deleted N scan(s)" text.
        # Stderr: status commentary, not data.
        err_console.print(f"[green]✓[/green] Deleted {len(deleted)} {noun}", highlight=False)

    if failed:
        noun = "scan" if len(failed) == 1 else "scans"
        print_error(f"Failed to delete {len(failed)} {noun}:")
        for scan_id, error in failed:
            click.echo(f"  - {scan_id}: {error}", err=True)
        raise SystemExit(1)
