"""Custom-variable ("xnat-varput") commands for xnatctl.

Registers ``subject vars``/``subject vars set`` onto the ``subject`` group
defined in :mod:`xnatctl.cli.subject`, and ``session vars``/``session vars
set`` onto the ``session`` group defined in :mod:`xnatctl.cli.session`,
mirroring how :mod:`xnatctl.cli.session_query` registers ``list``/``show``
onto the same ``session`` group.

XNAT stores per-resource, project-defined custom variables (the "field"
elements a project admin adds under Manage -> custom variables). Every
route below was verified against a live XNAT 1.9.2.1 -- see the docstrings
on ``SubjectService``/``SessionService``'s ``list_vars``/``set_vars`` for
what was actually observed, including a real silent-no-op trap on the
session side (a plain PUT to the flat experiment route returns 200 but
writes nothing). Scan-level custom variables are NOT shipped here: every
form tried against the live server (flat and fully-scoped paths, prefixed
and unprefixed field keys, with and without an explicit ``xsiType`` param)
returned 200 without persisting anything, unlike a scan's own documented
fields (``note``, ``quality``), which do persist. Since there's no
verified-working request shape, ``scan vars`` is left unimplemented rather
than shipped pretending to work.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    batch_option,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
)
from xnatctl.cli.session import session
from xnatctl.cli.subject import subject
from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.core.output import print_error, print_output, print_success
from xnatctl.core.validation import validate_project_id, validate_session_id, validate_subject_id
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.services.hierarchy import HierarchyService
from xnatctl.services.sessions import SessionService
from xnatctl.services.sessions import _validate_field_name as _validate_session_field_name
from xnatctl.services.subjects import SubjectService
from xnatctl.services.subjects import _validate_field_name as _validate_subject_field_name

_VARS_COLUMNS = ["name", "value"]
_VARS_COLUMN_LABELS = {"name": "Name", "value": "Value"}


def _parse_pairs(pairs: tuple[str, ...]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` arguments into a dict, in order.

    Raises:
        click.UsageError: A pair has no ``=``, an empty key, or a key that
            repeats one already seen in this call -- collapsing ``key=one
            key=two`` to a silent ``{"key": "two"}`` would send only the
            last value with no indication the first was dropped.
    """
    fields: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise click.UsageError(f"invalid KEY=VALUE pair: {pair!r}")
        if key in fields:
            raise click.UsageError(f"duplicate key {key!r} in KEY=VALUE arguments")
        fields[key] = value
    return fields


def _print_vars(ctx: Context, rows: list[dict[str, str]]) -> None:
    """Render custom-variable rows the same way for subject and session."""
    print_output(
        rows,
        format=ctx.output_format,
        columns=_VARS_COLUMNS,
        column_labels=_VARS_COLUMN_LABELS,
        quiet=ctx.quiet,
        id_field="name",
    )


# =============================================================================
# subject vars / subject vars set
#
# ``subject vars SUBJECT_ID`` (read) and ``subject vars set SUBJECT_ID
# KEY=VALUE...`` (write) share one leading positional argument. Click's
# Group.parse_args parses a group's own declared Argument params against the
# FULL token stream before subcommand resolution even runs -- so a
# `subject_id` argument declared directly on the `vars` group would consume
# the literal token "set" as its value on `subject vars set SUB001 K=V`,
# leaving "SUB001" to fail subcommand lookup ("No such command 'SUB001'").
# Declaring no argument on the group side-steps that: the read behavior
# lives in a hidden "_read" subcommand with its own SUBJECT_ID argument, and
# _DefaultGroup below falls back to it whenever the leading token isn't a
# real subcommand name. (Session vars doesn't need this: -E is an option,
# not a positional, so option parsing doesn't collide with subcommand
# dispatch -- see the plain ``invoke_without_command=True`` group below.)
# =============================================================================


class _DefaultGroup(click.Group):
    """A click.Group that falls back to one subcommand when dispatch would fail.

    Only used where a group's primary action takes a positional argument
    that would otherwise be swallowed by Click's own param parsing before
    subcommand resolution runs (see the module comment above).
    """

    default_cmd_name = "_read"

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Dispatch normally; fall back to the default command on a bad name."""
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            cmd = self.get_command(ctx, self.default_cmd_name)
            if cmd is None:
                raise
            return self.default_cmd_name, cmd, args


@subject.group("vars", cls=_DefaultGroup)
def subject_vars() -> None:
    """List a subject's custom variables, or set them with `vars set`.

    \b
    Example:
        xnatctl subject vars SUB001 -P MYPROJ
        xnatctl subject vars SUB001 -P MYPROJ -o json
        xnatctl subject vars set SUB001 studytag=phase1 -P MYPROJ --yes
    """


@subject_vars.command("_read", hidden=True)
@click.argument("subject_id")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@global_options
@handle_errors
@require_auth
def subject_vars_read(ctx: Context, subject_id: str, project: str | None) -> None:
    """List a subject's custom variables."""
    project = validate_project_id(require_project_from_context(ctx, project))
    subject_id = validate_subject_id(subject_id)
    service = SubjectService(ctx.get_client())

    try:
        rows = service.list_vars(subject_id, project=project)
    except ResourceNotFoundError:
        print_error(f"Subject not found: {subject_id}")
        raise SystemExit(1) from None

    _print_vars(ctx, rows)


@subject_vars.command("set")
@click.argument("args", nargs=-1)
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@batch_option
@confirm_destructive("Set these custom variables?")
@global_options
@handle_errors
@require_auth
def subject_vars_set(
    ctx: Context,
    args: tuple[str, ...],
    project: str | None,
    batch_ids: list[str] | None,
    dry_run: bool,
) -> None:
    """Set one or more custom variables on a subject.

    \b
    Example:
        xnatctl subject vars set SUB001 studytag=phase1 cohort=A -P MYPROJ --yes
        xnatctl subject list -P MYPROJ -q | xnatctl subject vars set \\
            --batch - studytag=phase1 -P MYPROJ --yes
    """
    # A single `nargs=-1` ARGS, split here into subject_id + pairs, rather
    # than two separate Click Arguments (one optional SUBJECT_ID, one
    # nargs=-1 for the pairs): Click distributes tokens across positional
    # Arguments by declared order regardless of content, so with two
    # Arguments `--batch file.txt studytag=phase1` assigns the string
    # "studytag=phase1" to the optional SUBJECT_ID slot (it's declared
    # first) rather than to the pairs slot -- verified directly against
    # Click's parser. One Argument sidesteps that entirely: with --batch,
    # every token in ARGS is a KEY=VALUE pair (SUBJECT_ID came from the
    # batch file instead); without it, the first token is SUBJECT_ID and
    # the rest are pairs.
    ids: list[str] | None
    if batch_ids is not None:
        ids = batch_ids
        pairs: tuple[str, ...] = args
    elif args:
        subject_id, *rest = args
        ids = [subject_id]
        pairs = tuple(rest)
    else:
        ids = None
        pairs = ()

    if not ids:
        raise click.UsageError("provide SUBJECT_ID or --batch")
    if not pairs:
        raise click.UsageError("provide at least one KEY=VALUE pair")

    fields = _parse_pairs(pairs)
    # Validate every field name exactly as SubjectService.set_vars will --
    # dry-run must reject what execution rejects, not report success for
    # input the real command later refuses.
    for name in fields:
        _validate_subject_field_name(name)
    project = validate_project_id(require_project_from_context(ctx, project))
    validated_ids = [validate_subject_id(raw_id) for raw_id in ids]
    service = SubjectService(ctx.get_client())
    names = ", ".join(fields)

    # Every subject must still be CONFIRMED TO EXIST under --dry-run --
    # skipping only the final `set_vars` call -- so a dry run reports the
    # same not-found failure execution would, instead of approving a
    # typo'd subject ID. `SubjectService.set_vars` writes through the same
    # create-or-update route `SubjectService.create` uses, and a
    # nonexistent subject answers 201 there, not 404 (verified live
    # against XNAT 1.9.2.1) -- a plain PUT silently creates an empty
    # subject instead of failing, and a dry run that skipped this check
    # would "approve" that typo too.
    #
    # Isolation only matters when there IS a rest of the batch to protect --
    # see subject_delete for the identical reasoning.
    isolate_failures = len(validated_ids) > 1
    failed: list[tuple[str, str]] = []
    for sid in validated_ids:
        try:
            if dry_run:
                service.get(sid, project=project)
            else:
                service.set_vars(sid, fields, project=project)
        except Exception as e:  # noqa: BLE001  # per-subject isolation across a multi-subject batch
            if not isolate_failures:
                raise
            failed.append((sid, str(e)))
            continue
        if dry_run:
            click.echo(
                f"[DRY-RUN] Would set {len(fields)} variable(s) on subject {sid}: {names}",
                err=True,
            )
        else:
            print_success(f"Set {len(fields)} variable(s) on subject {sid}")

    if failed:
        noun = "subject" if len(failed) == 1 else "subjects"
        print_error(f"Failed to set variables on {len(failed)} {noun}:")
        for sid, error in failed:
            click.echo(f"  - {sid}: {error}", err=True)
        raise SystemExit(1)


# =============================================================================
# session vars / session vars set
#
# -E is an option, not a positional argument, so there's no dispatch
# conflict here -- the plain `admin plugins`-style default-listing group
# works unmodified.
# =============================================================================


@session.group("vars", invoke_without_command=True)
@click.option(
    "--experiment",
    "-E",
    "session_id",
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
def session_vars(ctx: Context, session_id: str | None, project: str | None) -> None:
    """List a session's custom variables, or set them with `vars set`.

    \b
    Example:
        xnatctl session vars -E XNAT_E00001
        xnatctl session vars -E SESSION_LABEL -P MYPROJ -o json
        xnatctl session vars set -E XNAT_E00001 studytag=phase1 --yes
    """
    click_ctx = click.get_current_context()
    if click_ctx.invoked_subcommand is not None:
        return
    if not session_id:
        raise click.UsageError("Missing option '--experiment' / '-E'.")

    session_id = validate_session_id(session_id)
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

    rows = SessionService(client).list_vars(resolved.experiment_id)
    _print_vars(ctx, rows)


def _resolve_session_for_vars(
    hierarchy: HierarchyService, session_id: str, project: str | None
) -> tuple[str, str, str, str] | None:
    """Resolve one session to (project, subject, experiment_id, xsi_type).

    Returns ``None`` (rather than raising) when the session isn't found, so
    a multi-ID batch can isolate the failure the same way
    ``_run_scan_deletes`` does -- see ``session_vars_set`` below.

    Raises:
        click.ClickException: The session resolved, but its project or
            subject could not be determined -- ``SessionService.set_vars``
            cannot build a working write path without both (see its
            docstring for why the flat route silently no-ops).
    """
    ref = ExperimentRef(
        experiment=session_id, project_id=project, experiment_is_label=project is not None
    )
    try:
        resolved = hierarchy.resolve_experiment(ref)
    except ResourceNotFoundError:
        return None

    if not resolved.project_id or not resolved.subject_id or not resolved.xsi_type:
        raise click.ClickException(
            f"Could not resolve project/subject/type for session '{session_id}' -- "
            "custom variables need all three to write reliably (see "
            "SessionService.set_vars). Retry with -P/--project."
        )
    return resolved.project_id, resolved.subject_id, resolved.experiment_id, resolved.xsi_type


@session_vars.command("set")
@click.option(
    "--experiment",
    "-E",
    "session_id",
    metavar="ID_OR_LABEL",
    help="Experiment ID (accession #), or label when -P is provided",
)
@click.option(
    "--project",
    "-P",
    help="Project ID (enables lookup by label; defaults to profile default_project)",
)
@click.argument("pairs", nargs=-1)
@batch_option
@confirm_destructive("Set these custom variables?")
@global_options
@handle_errors
@require_auth
def session_vars_set(
    ctx: Context,
    session_id: str | None,
    project: str | None,
    pairs: tuple[str, ...],
    batch_ids: list[str] | None,
    dry_run: bool,
) -> None:
    """Set one or more custom variables on a session.

    \b
    Example:
        xnatctl session vars set -E XNAT_E00001 studytag=phase1 cohort=A --yes
        xnatctl session list -P MYPROJ -q | xnatctl session vars set \\
            --batch - studytag=phase1 -P MYPROJ --yes
    """
    if session_id and batch_ids is not None:
        raise click.UsageError("provide -E/--experiment or --batch, not both")
    ids = batch_ids if batch_ids is not None else ([session_id] if session_id else None)
    if not ids:
        raise click.UsageError("provide -E/--experiment or --batch")
    if not pairs:
        raise click.UsageError("provide at least one KEY=VALUE pair")

    fields = _parse_pairs(pairs)
    # Validate every field name exactly as SessionService.set_vars will --
    # dry-run must reject what execution rejects, not report success for
    # input the real command later refuses.
    for name in fields:
        _validate_session_field_name(name)
    validated_ids = [validate_session_id(raw_id) for raw_id in ids]
    client = ctx.get_client()
    project = default_project_from_context(ctx) if project is None else project
    if project is not None:
        project = validate_project_id(project)
    hierarchy = HierarchyService(client)
    service = SessionService(client)
    names = ", ".join(fields)

    # Every session must still be RESOLVED under --dry-run -- skipping only
    # the final `set_vars` call -- so a dry run reports the same refusal
    # execution would for a session that doesn't exist or can't be resolved,
    # instead of returning before resolution ever ran and reporting success
    # for input the real command later refuses.
    isolate_failures = len(validated_ids) > 1
    failed: list[tuple[str, str]] = []
    for sid in validated_ids:
        try:
            resolved = _resolve_session_for_vars(hierarchy, sid, project)
            if resolved is None:
                raise ResourceNotFoundError("session", sid)
            resolved_project, resolved_subject, experiment_id, xsi_type = resolved
            if not dry_run:
                service.set_vars(
                    project=resolved_project,
                    subject=resolved_subject,
                    experiment_id=experiment_id,
                    xsi_type=xsi_type,
                    fields=fields,
                )
        except Exception as e:  # noqa: BLE001  # per-session isolation across a multi-session batch
            if not isolate_failures:
                raise
            failed.append((sid, str(e)))
            continue
        if dry_run:
            click.echo(
                f"[DRY-RUN] Would set {len(fields)} variable(s) on session {sid}: {names}",
                err=True,
            )
        else:
            print_success(f"Set {len(fields)} variable(s) on session {sid}")

    if failed:
        noun = "session" if len(failed) == 1 else "sessions"
        print_error(f"Failed to set variables on {len(failed)} {noun}:")
        for sid, error in failed:
            click.echo(f"  - {sid}: {error}", err=True)
        raise SystemExit(1)
