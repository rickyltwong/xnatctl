"""Cross-project sharing commands for xnatctl (subject share/unshare, session share/unshare).

Registers ``subject share``/``subject unshare`` onto the ``subject`` group
defined in :mod:`xnatctl.cli.subject`, and ``session share``/``session
unshare`` onto the ``session`` group defined in :mod:`xnatctl.cli.session`,
mirroring how :mod:`xnatctl.cli.session_query` registers ``list``/``show``
onto the same ``session`` group.

XNAT lets a subject or experiment be shared into a second project without
moving it -- the resource keeps one primary project and gains visibility in
each project it is shared into, each with its own (optionally different)
label. Every route and response shape below was verified against a live
XNAT 1.9.2.1; see the docstrings on ``SubjectService``/``SessionService``'s
``share``/``unshare``/``list_shares`` for what was actually observed.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
)
from xnatctl.cli.session import session
from xnatctl.cli.subject import subject
from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError
from xnatctl.core.output import print_error, print_success
from xnatctl.core.validation import (
    validate_project_id,
    validate_session_id,
    validate_subject_id,
    validate_xnat_label,
)
from xnatctl.models.hierarchy import ExperimentRef, SubjectRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.sessions import SessionService
from xnatctl.services.subjects import SubjectService

# =============================================================================
# subject share / unshare
# =============================================================================


@subject.command("share")
@click.argument("subject_id")
@click.option("--into", "target_project", required=True, help="Project to share the subject into")
@click.option("--project", "-P", help="Source project ID (defaults to profile default_project)")
@click.option(
    "--label",
    help=(
        "Label the subject should carry in the target project. Omit and XNAT "
        "defaults it to the subject's accession ID, not its primary label."
    ),
)
@confirm_destructive("Share this subject into another project?")
@global_options
@handle_errors
@require_auth
def subject_share(
    ctx: Context,
    subject_id: str,
    target_project: str,
    project: str | None,
    label: str | None,
    dry_run: bool,
) -> None:
    """Share a subject into another project without moving it.

    The subject keeps its primary project; the target project gains a
    second reference to the same subject data, optionally under a
    different label.

    \b
    Example:
        xnatctl subject share SUB001 --into OTHERPROJ -P MYPROJ
        xnatctl subject share SUB001 --into OTHERPROJ --label SUB001_SHARED --yes
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    subject_id = validate_subject_id(subject_id)
    target_project = validate_project_id(target_project)
    if label is not None:
        label = validate_xnat_label(label, "subject label")
    client = ctx.get_client()
    hierarchy = HierarchyService(client)

    try:
        resolved = hierarchy.resolve_subject(SubjectRef(subject=subject_id, project_id=project))
    except ResourceNotFoundError:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1) from None

    if dry_run:
        as_label = f" as '{label}'" if label else ""
        click.echo(
            f"[DRY-RUN] Would share subject {resolved.subject_id} into {target_project}{as_label}",
            err=True,
        )
        return

    service = SubjectService(client)
    service.share(resolved.subject_id, target_project, label=label)
    print_success(
        f"Shared subject {resolved.subject_label or resolved.subject_id} into {target_project}"
    )


@subject.command("unshare")
@click.argument("subject_id")
@click.option("--from", "target_project", required=True, help="Project to remove the share from")
@click.option("--project", "-P", help="Source project ID (defaults to profile default_project)")
@confirm_destructive("Remove this subject's share from another project?")
@global_options
@handle_errors
@require_auth
def subject_unshare(
    ctx: Context,
    subject_id: str,
    target_project: str,
    project: str | None,
    dry_run: bool,
) -> None:
    """Remove a subject's share from another project.

    Aiming ``--from`` at the subject's OWN primary project is refused:
    XNAT answers that by deleting the subject and every experiment under
    it, with a response indistinguishable from removing an ordinary share.
    Use ``xnatctl subject delete`` if deletion is what you want. XNAT also
    rejects removing a share that does not exist -- see
    ``SubjectService.unshare`` for both behaviours as observed live.

    \b
    Example:
        xnatctl subject unshare SUB001 --from OTHERPROJ -P MYPROJ --yes
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    subject_id = validate_subject_id(subject_id)
    target_project = validate_project_id(target_project)
    client = ctx.get_client()
    hierarchy = HierarchyService(client)

    try:
        resolved = hierarchy.resolve_subject(SubjectRef(subject=subject_id, project_id=project))
    except ResourceNotFoundError:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1) from None

    primary_project = resolved.project_id or project

    # Same primary-project refusal `SubjectService.unshare` enforces --
    # checked here too, before the dry-run branch, so `--dry-run` reports
    # the same refusal execution would rather than returning early and
    # reporting success for a call the service would reject. Stripped AND
    # casefolded on both sides so the CLI and the service cannot disagree
    # about what counts as a match -- padded input (e.g. a trailing space
    # from a copy-paste) must not slip past this into the service's own
    # check, which compares the same way.
    if target_project.strip().casefold() == primary_project.strip().casefold():
        raise InputValidationError(
            f"refusing to unshare subject {resolved.subject_id} from {target_project}: that is "
            "its primary project, and XNAT answers this by deleting the subject and everything "
            "under it, not by removing a share. Use `xnatctl subject delete` if deletion is "
            "what you want.",
            field="from",
            value=target_project,
        )

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would remove subject {resolved.subject_id}'s share from {target_project}",
            err=True,
        )
        return

    service = SubjectService(client)
    service.unshare(resolved.subject_id, target_project, primary_project=primary_project)
    print_success(
        f"Removed subject {resolved.subject_label or resolved.subject_id}'s "
        f"share from {target_project}"
    )


# =============================================================================
# session share / unshare
# =============================================================================


@session.command("share")
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
@click.option("--into", "target_project", required=True, help="Project to share the session into")
@click.option(
    "--label",
    help=(
        "Label the session should carry in the target project. Omit and XNAT "
        "defaults it to the session's accession ID, not its primary label."
    ),
)
@confirm_destructive("Share this session into another project?")
@global_options
@handle_errors
@require_auth
def session_share(
    ctx: Context,
    session_id: str,
    project: str | None,
    target_project: str,
    label: str | None,
    dry_run: bool,
) -> None:
    """Share a session into another project without moving it.

    The session keeps its primary project; the target project gains a
    second reference to the same session data, optionally under a
    different label.

    \b
    Example:
        xnatctl session share -E XNAT_E00001 --into OTHERPROJ
        xnatctl session share -E SESSION_LABEL -P MYPROJ --into OTHERPROJ --label SESS_SHARED
    """
    session_id = validate_session_id(session_id)
    target_project = validate_project_id(target_project)
    if label is not None:
        label = validate_xnat_label(label, "experiment label")
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    if project is not None:
        project = validate_project_id(project)
    hierarchy = HierarchyService(client)
    ref = ExperimentRef(
        experiment=session_id, project_id=project, experiment_is_label=project is not None
    )

    try:
        resolved = hierarchy.resolve_experiment(ref)
    except ResourceNotFoundError:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1) from None

    if dry_run:
        as_label = f" as '{label}'" if label else ""
        click.echo(
            f"[DRY-RUN] Would share session {resolved.experiment_id} "
            f"into {target_project}{as_label}",
            err=True,
        )
        return

    service = SessionService(client)
    service.share(resolved.experiment_id, target_project, label=label)
    print_success(
        f"Shared session {resolved.experiment_label or resolved.experiment_id} "
        f"into {target_project}"
    )


@session.command("unshare")
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
@click.option("--from", "target_project", required=True, help="Project to remove the share from")
@confirm_destructive("Remove this session's share from another project?")
@global_options
@handle_errors
@require_auth
def session_unshare(
    ctx: Context,
    session_id: str,
    project: str | None,
    target_project: str,
    dry_run: bool,
) -> None:
    """Remove a session's share from another project.

    Aiming ``--from`` at the session's OWN primary project is refused:
    XNAT answers that by deleting the session and its data, with a
    response indistinguishable from removing an ordinary share. Removing a
    share that does not exist, on the other hand, is idempotent here --
    unlike the subject equivalent, which rejects it. See
    ``SessionService.unshare`` for both behaviours as observed live.

    \b
    Example:
        xnatctl session unshare -E XNAT_E00001 --from OTHERPROJ --yes
    """
    session_id = validate_session_id(session_id)
    target_project = validate_project_id(target_project)
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    if project is not None:
        project = validate_project_id(project)
    hierarchy = HierarchyService(client)
    ref = ExperimentRef(
        experiment=session_id, project_id=project, experiment_is_label=project is not None
    )

    try:
        resolved = hierarchy.resolve_experiment(ref)
    except ResourceNotFoundError:
        print_error(f"Session not found: {session_id}")
        raise SystemExit(1) from None

    # Knowing the primary project is a precondition, not a nicety: unsharing
    # a session FROM its primary project makes XNAT delete it outright (200,
    # then 404), so the guard in SessionService.unshare needs a real value to
    # compare against. Refuse rather than guess -- an unguarded unshare is
    # the one failure mode here that destroys data.
    primary_project = resolved.project_id or project
    if not primary_project:
        print_error(
            "Cannot determine which project owns this session, and unsharing "
            "cannot be checked without it. Pass -P, or set default_project on "
            "the profile."
        )
        raise SystemExit(1)

    # Same primary-project refusal `SessionService.unshare` enforces --
    # checked here too, before the dry-run branch, so `--dry-run` reports
    # the same refusal execution would rather than returning early and
    # reporting success for a call the service would reject. Stripped AND
    # casefolded on both sides so the CLI and the service cannot disagree
    # about what counts as a match -- padded input (e.g. a trailing space
    # from a copy-paste) must not slip past this into the service's own
    # check, which compares the same way.
    if target_project.strip().casefold() == primary_project.strip().casefold():
        raise InputValidationError(
            f"refusing to unshare session {resolved.experiment_id} from {target_project}: that "
            "is its primary project, and XNAT answers this by deleting the session and its "
            "data, not by removing a share. Delete the session explicitly if that is what you "
            "want.",
            field="from",
            value=target_project,
        )

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would remove session {resolved.experiment_id}'s share "
            f"from {target_project}",
            err=True,
        )
        return

    service = SessionService(client)
    service.unshare(resolved.experiment_id, target_project, primary_project=primary_project)
    print_success(
        f"Removed session {resolved.experiment_label or resolved.experiment_id}'s "
        f"share from {target_project}"
    )
