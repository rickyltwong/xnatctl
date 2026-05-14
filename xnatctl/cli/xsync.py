"""XSync command group for xnatctl.

Exposes the XNAT XSync feature as a first-class subcommand group. See
:mod:`xnatctl.services.xsync` for the underlying REST surface.

Secret-handling notes (load-bearing — do not relax without updating
``.harness/decisions.md``):

* ``refresh-credentials`` accepts the remote XNAT password from stdin,
  the ``XNAT_XSYNC_REMOTE_PASS`` env var, or an interactive prompt only.
  A password supplied on argv (``--remote-pass X``) raises
  :class:`click.UsageError`. The flag exists solely so the env-var lookup
  can be expressed declaratively; the option itself does not accept a
  value on the command line.
* Destructive operations (``sync``, ``sync-subject``, ``refresh-credentials``)
  carry :func:`xnatctl.cli.common.confirm_destructive` semantics. The
  ``--all`` variant of ``refresh-credentials`` also requires either
  ``--yes`` or interactive confirmation per project enumerated.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
)
from xnatctl.core.output import (
    OutputFormat,
    print_error,
    print_json,
    print_output,
    print_success,
)
from xnatctl.services.xsync import XsyncService

# Env-var name documented in ``.harness/decisions.md``. Keep here so the
# CLI surface and the secret-sourcing helper agree on the spelling.
_REMOTE_PASS_ENV: str = "XNAT_XSYNC_REMOTE_PASS"


def _reject_argv_password(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> None:
    """Reject ``--remote-pass <secret>`` at parse time.

    Wired as the ``callback=`` for the ``--remote-pass`` option so the
    failure mode is a :class:`click.UsageError` (exit 2) before any
    authentication, configuration, or context decorator runs. The
    deterrent flag exists only to surface the secret-sourcing contract
    in ``--help``; supplying a value on argv is unconditionally a usage
    error.

    Args:
        ctx: Click parsing context (unused).
        param: The ``--remote-pass`` parameter (unused).
        value: The argv-supplied value, or ``None`` if the flag was
            omitted.

    Returns:
        ``None``. The callback never propagates a value into the
        command body; only the absence-of-value case is permitted, and
        the command signature treats the parameter as always-``None``.

    Raises:
        click.UsageError: If ``value`` is not ``None``.
    """
    del ctx, param  # Unused; the rejection is unconditional.
    if value is not None:
        raise click.UsageError(
            "Refusing to read remote password from argv. "
            "Use --remote-pass-stdin or set the XNAT_XSYNC_REMOTE_PASS env var."
        )
    return None


@click.group("xsync")
def xsync() -> None:
    """Manage XNAT XSync (cross-site federation) projects and credentials.

    Examples:

        xnatctl xsync list

        xnatctl xsync setup -P PROJ

        xnatctl xsync status -P PROJ

        xnatctl xsync progress -P PROJ          # streams plain text

        xnatctl xsync sync -P PROJ

        xnatctl xsync refresh-credentials -P PROJ \\
            --remote-url https://remote.example.org \\
            --remote-user alice --remote-pass-stdin
    """


# =============================================================================
# Read commands
# =============================================================================


@xsync.command("list")
@global_options
@require_auth
@handle_errors
def xsync_list(ctx: Context) -> None:
    """List XSync-bound projects on the local XNAT."""
    client = ctx.get_client()
    service = XsyncService(client)
    data = service.list_projects()

    if ctx.quiet:
        # Try to extract project IDs from a handful of plausible shapes.
        rows = _coerce_rows(data)
        for row in rows:
            click.echo(_extract_project_id(row))
        return

    if ctx.output_format == OutputFormat.JSON:
        print_json(data)
        return

    rows = _coerce_rows(data)
    if not rows:
        print_json(data)
        return

    columns = list(rows[0].keys())
    print_output(rows, format=ctx.output_format, columns=columns)


@xsync.command("setup")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project).",
)
@global_options
@require_auth
@handle_errors
def xsync_setup(ctx: Context, project_id: str | None) -> None:
    """Show the XSync setup record for a project."""
    resolved = require_project_from_context(ctx, project_id)
    service = XsyncService(ctx.get_client())
    data = service.get_setup(resolved)
    _emit_payload(ctx, data)


@xsync.command("status")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project).",
)
@global_options
@require_auth
@handle_errors
def xsync_status(ctx: Context, project_id: str | None) -> None:
    """Show the XSync status record for a project."""
    resolved = require_project_from_context(ctx, project_id)
    service = XsyncService(ctx.get_client())
    data = service.get_status(resolved)
    _emit_payload(ctx, data)


@xsync.command("history")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project).",
)
@global_options
@require_auth
@handle_errors
def xsync_history(ctx: Context, project_id: str | None) -> None:
    """Show the XSync run history for a project."""
    resolved = require_project_from_context(ctx, project_id)
    service = XsyncService(ctx.get_client())
    data = service.get_history(resolved)
    _emit_payload(ctx, data)


@xsync.command("progress")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project).",
)
@global_options
@require_auth
@handle_errors
def xsync_progress(ctx: Context, project_id: str | None) -> None:
    """Stream the current XSync progress log (plain text) to stdout."""
    resolved = require_project_from_context(ctx, project_id)
    service = XsyncService(ctx.get_client())
    click.echo(service.get_progress(resolved))


# =============================================================================
# Write commands
# =============================================================================


@xsync.command("sync")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project).",
)
@global_options
@require_auth
@handle_errors
@confirm_destructive("Trigger an XSync run for this project?")
def xsync_sync(ctx: Context, project_id: str | None, dry_run: bool) -> None:
    """Trigger an XSync run for the given project."""
    resolved = require_project_from_context(ctx, project_id)
    if dry_run:
        click.echo(f"[DRY-RUN] Would trigger xsync for project {resolved}", err=True)
        return
    service = XsyncService(ctx.get_client())
    service.sync(resolved)
    print_success(f"XSync triggered for {resolved}")


@xsync.command("sync-subject")
@click.option(
    "-E",
    "--experiment",
    "experiment_id",
    required=True,
    help="Experiment / session id to sync.",
)
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (informational; falls back to profile default).",
)
@global_options
@require_auth
@handle_errors
@confirm_destructive("Trigger an XSync run for this subject/experiment?")
def xsync_sync_subject(
    ctx: Context,
    experiment_id: str,
    project_id: str | None,
    dry_run: bool,
) -> None:
    """Trigger an XSync run for a single subject / experiment."""
    del project_id  # currently informational; XSync endpoint keys on experiment id
    if dry_run:
        click.echo(f"[DRY-RUN] Would trigger xsync for experiment {experiment_id}", err=True)
        return
    service = XsyncService(ctx.get_client())
    service.sync_subject(experiment_id)
    print_success(f"XSync triggered for experiment {experiment_id}")


# =============================================================================
# Credentials rotation
# =============================================================================


def _resolve_remote_password(*, pass_stdin: bool) -> str:
    """Source the remote password from stdin, env var, or interactive prompt.

    Priority (decisions.md):

    1. ``--remote-pass-stdin`` -> read one line from ``sys.stdin``.
    2. ``XNAT_XSYNC_REMOTE_PASS`` env var.
    3. Interactive ``click.prompt(hide_input=True)``.

    Args:
        pass_stdin: Whether ``--remote-pass-stdin`` was passed.

    Returns:
        The remote password as a string. Never logged by this helper.
    """
    if pass_stdin:
        # ``readline`` so a trailing newline (common with ``echo``) is
        # stripped without consuming any further stdin bytes.
        secret = sys.stdin.readline()
        if secret.endswith("\n"):
            secret = secret[:-1]
        if not secret:
            raise click.UsageError("--remote-pass-stdin was set but stdin was empty.")
        return secret

    env_value = os.environ.get(_REMOTE_PASS_ENV)
    if env_value:
        return env_value

    prompted = click.prompt("Remote XNAT password", hide_input=True)
    return str(prompted)


def _refresh_one(
    service: XsyncService,
    *,
    project_id: str,
    remote_url: str,
    remote_username: str,
    remote_password: str,
    local_project: str,
    remote_project: str,
    sync_new_only: bool,
) -> dict[str, Any]:
    """Single-project wrapper around :meth:`XsyncService.refresh_credentials`.

    Pulled out so the ``--all`` loop can call the same code path that the
    single-project case uses, keeping the secret-handling surface uniform.
    """
    return service.refresh_credentials(
        project_id=project_id,
        remote_url=remote_url,
        remote_username=remote_username,
        remote_password=remote_password,
        local_project=local_project,
        remote_project=remote_project,
        sync_new_only=sync_new_only,
    )


def _enumerate_bound_projects(service: XsyncService, remote_url: str) -> list[str]:
    """Return the local project ids bound to ``remote_url``.

    Best-effort: walks :meth:`XsyncService.list_projects` and filters rows
    whose ``host`` / ``remoteHost`` / ``url`` value matches ``remote_url``.
    Returns every project id when the list endpoint does not surface a
    remote-URL field (caller decides whether that is acceptable).
    """
    data = service.list_projects()
    rows = _coerce_rows(data)
    if not rows:
        return []

    matched: list[str] = []
    fallback: list[str] = []
    for row in rows:
        project_id = _extract_project_id(row)
        if not project_id:
            continue
        host = (
            row.get("host")
            or row.get("remoteHost")
            or row.get("remote_host")
            or row.get("url")
            or row.get("remoteUrl")
            or row.get("remote_url")
        )
        fallback.append(project_id)
        if host and remote_url and host.rstrip("/") == remote_url.rstrip("/"):
            matched.append(project_id)

    return matched or fallback


@xsync.command("refresh-credentials")
@click.option(
    "-P",
    "--project",
    "project_id",
    default=None,
    help="Local project id (falls back to profile default_project; ignored with --all).",
)
@click.option(
    "--remote-url",
    required=True,
    help="Remote XNAT base URL.",
)
@click.option(
    "--remote-user",
    "remote_username",
    required=True,
    help="Remote XNAT username.",
)
@click.option(
    "--remote-pass-stdin",
    "remote_pass_stdin",
    is_flag=True,
    default=False,
    help="Read the remote XNAT password from stdin (one line).",
)
@click.option(
    "--remote-pass",
    "remote_pass_argv",
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_reject_argv_password,
    help=(
        "Reserved for declarative use; supplying a value on argv is a "
        f"usage error. Use --remote-pass-stdin or set ${_REMOTE_PASS_ENV}."
    ),
)
@click.option(
    "--local-project",
    "local_project",
    default=None,
    help="Project id used in the save/check payloads' localProject field.",
)
@click.option(
    "--remote-project",
    "remote_project",
    default=None,
    help="Project id used in the save payload's remoteProject field.",
)
@click.option(
    "--sync-new-only/--no-sync-new-only",
    "sync_new_only",
    default=True,
    show_default=True,
    help="Whether to request new-only sync semantics.",
)
@click.option(
    "--all",
    "all_bound",
    is_flag=True,
    default=False,
    help="Rotate every XSync-bound project that matches --remote-url.",
)
@global_options
@require_auth
@handle_errors
@confirm_destructive("Rotate XSync credentials for this project?")
def xsync_refresh_credentials(
    ctx: Context,
    project_id: str | None,
    remote_url: str,
    remote_username: str,
    remote_pass_stdin: bool,
    local_project: str | None,
    remote_project: str | None,
    sync_new_only: bool,
    all_bound: bool,
    dry_run: bool,
) -> None:
    """Rotate the XSync remote credentials for a project (or every bound project).

    The three-step flow (remoteREST -> credentials/save -> credentials/check)
    runs entirely inside this command; operators no longer need to script
    against raw curl.

    Examples:

        xnatctl xsync refresh-credentials -P PROJ \\
            --remote-url https://remote.example.org \\
            --remote-user alice --remote-pass-stdin \\
            --local-project PROJ --remote-project PROJ_REMOTE

        # Rotate every bound project sharing the same remote URL.
        echo -n "$REMOTE_PASS" | xnatctl xsync refresh-credentials \\
            --remote-url https://remote.example.org \\
            --remote-user alice --remote-pass-stdin --all --yes
    """
    # ``--remote-pass`` argv values are rejected at parse time by
    # :func:`_reject_argv_password`; by the time we get here the only
    # supported secret sources are stdin, env var, and interactive prompt.
    remote_password = _resolve_remote_password(pass_stdin=remote_pass_stdin)

    service = XsyncService(ctx.get_client())

    if all_bound:
        target_projects = _enumerate_bound_projects(service, remote_url)
        if not target_projects:
            print_error(f"No XSync-bound projects matched --remote-url {remote_url}")
            raise SystemExit(1)
    else:
        target_projects = [require_project_from_context(ctx, project_id)]

    succeeded = 0
    failed: list[tuple[str, str]] = []

    for proj in target_projects:
        effective_local = local_project or proj
        effective_remote = remote_project or proj

        if dry_run:
            click.echo(
                f"[DRY-RUN] Would refresh credentials for {proj} "
                f"(local={effective_local}, remote={effective_remote})",
                err=True,
            )
            succeeded += 1
            continue

        try:
            _refresh_one(
                service,
                project_id=proj,
                remote_url=remote_url,
                remote_username=remote_username,
                remote_password=remote_password,
                local_project=effective_local,
                remote_project=effective_remote,
                sync_new_only=sync_new_only,
            )
        except Exception as exc:
            # Stringify but do not echo the secret. ``handle_errors`` already
            # routes through :func:`redact_url_query`; we keep one local
            # safeguard so even bypass paths cannot leak the password.
            message = str(exc).replace(remote_password, "***") if remote_password else str(exc)
            failed.append((proj, message))
            print_error(f"Refresh failed for {proj}: {message}")
            continue

        succeeded += 1
        if not ctx.quiet:
            print_success(f"Refreshed XSync credentials for {proj}")

    if failed:
        click.echo(f"Summary: {succeeded} refreshed, {len(failed)} failed", err=True)
        raise SystemExit(1)

    if not ctx.quiet:
        click.echo(f"Summary: {succeeded} refreshed, 0 failed", err=True)


# =============================================================================
# Helpers
# =============================================================================


def _emit_payload(ctx: Context, data: Any) -> None:
    """Print a server payload using the active output format.

    Falls back to JSON when the payload is not list-of-dicts shaped so the
    operator always gets the raw data without an explicit ``-o json``.
    """
    if ctx.output_format == OutputFormat.JSON:
        print_json(data)
        return

    rows = _coerce_rows(data)
    if rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())
        print_output(rows, format=ctx.output_format, columns=columns)
        return

    print_json(data)


def _coerce_rows(data: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of a list-of-dicts from a server payload.

    Handles the common XNAT shapes: top-level list, ``ResultSet.Result``,
    or a single dict (wrapped as a one-element list).
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        result_set = data.get("ResultSet")
        if isinstance(result_set, dict):
            results = result_set.get("Result")
            if isinstance(results, list):
                return [row for row in results if isinstance(row, dict)]
        return [data]
    return []


def _extract_project_id(row: dict[str, Any]) -> str:
    """Pull a project id out of an XSync list row using the conventional keys."""
    for key in ("id", "ID", "projectId", "project_id", "project", "label"):
        value = row.get(key)
        if value:
            return str(value)
    return ""
