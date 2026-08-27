"""Anonymization script commands for xnatctl."""

from __future__ import annotations

import difflib
import sys
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
)
from xnatctl.core.output import print_output, print_success, print_warning
from xnatctl.core.validation import validate_project_id
from xnatctl.services.anon import AnonymizeService

F = TypeVar("F", bound=Callable[..., Any])


def read_script_argument(f: F) -> F:
    """Add a ``FILE`` positional argument, read as raw text.

    ``FILE`` is a path to a DicomEdit anonymization script, or ``-`` for
    stdin. Mirrors ``xnatctl.cli.payload.read_payload_argument`` exactly,
    except the file content is injected unparsed (as ``script``) rather than
    JSON-decoded -- an anonymization script is DicomEdit text, not JSON.
    Kept local to this module for the same reason ``payload.py`` documents
    for itself: this is a single feature's ``FILE|-`` reader, not a shared
    concern.

    Placed **above** ``@confirm_destructive`` in the decorator stack (so it
    runs first and stdin is fully consumed here, before any confirmation
    prompt would also try to read it) -- see
    ``xnatctl.cli.payload.read_payload_argument`` for the identical
    stdin/prompt-conflict reasoning, which applies unchanged here.
    """

    @click.argument("file", type=click.Path(exists=True, dir_okay=False, allow_dash=True))
    @wraps(f)
    def wrapper(*args: Any, file: str, **kwargs: Any) -> Any:
        """Read FILE (or stdin) and inject its raw text into kwargs."""
        if file == "-":
            if sys.stdin.isatty():
                raise click.UsageError(
                    "FILE - reads the script from stdin, but stdin is an "
                    "interactive terminal. Pipe a script in (e.g. 'cat "
                    "script.dicomedit | xnatctl anon set -') or pass a file "
                    "path instead."
                )
            if not kwargs.get("yes") and not kwargs.get("dry_run"):
                raise click.UsageError(
                    "FILE - requires --yes or --dry-run (stdin is consumed by the script)"
                )
            text = sys.stdin.read()
        else:
            with open(file, encoding="utf-8") as fh:
                text = fh.read()
        kwargs["script"] = text
        kwargs["script_source"] = file
        return f(*args, **kwargs)

    return wrapper  # type: ignore


def _text_diff(before: str, after: str, *, label: str) -> str:
    """Render a unified diff between two anonymization scripts, for ``--dry-run`` previews.

    Args:
        before: The script currently on the server (``""`` if none is set).
        after: The script that would be sent.
        label: Used to name both sides of the diff (e.g. ``"site script"``).

    Returns:
        A unified diff as a single string (empty when there is no change).
    """
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} (current)",
        tofile=f"{label} (new)",
    )
    return "".join(diff)


def _scope_label(project: str | None) -> str:
    """Human-readable label for the site scope or a project scope."""
    return f"project {project}'s override" if project is not None else "site-wide"


@click.group()
def anon() -> None:
    """Manage XNAT anonymization scripts."""
    pass


@anon.command("show")
@click.option(
    "--project",
    "-P",
    help="Show this project's override script instead of the site-wide script",
)
@global_options
@handle_errors
@require_auth
def anon_show(ctx: Context, project: str | None) -> None:
    """Show the site-wide anonymization script, or one project's override.

    A project with no override script prints a note and its ``script``
    field is empty -- it inherits the site-wide script (see ``anon set -P``).

    \b
    Example:
        xnatctl anon show
        xnatctl anon show -P MYPROJ
        xnatctl anon show -P MYPROJ -o json
    """
    service = AnonymizeService(ctx.get_client())

    if project is None:
        data = {
            "id": "site",
            "scope": "site",
            "project": None,
            "enabled": service.site_enabled(),
            "script": service.get_site_script(),
        }
    else:
        project = validate_project_id(project)
        script = service.get_project_script(project)
        if script is None:
            # 204 here means either "never configured" or "configured but
            # currently disabled" -- both look identical to a plain GET (see
            # AnonymizeService's module docstring), so distinguish them for
            # the user with the one route that can tell them apart.
            if service.project_has_script(project):
                print_warning(
                    f"Project {project} has a script set but it is disabled; "
                    "it currently inherits the site-wide script. "
                    f"Run `anon enable -P {project}` to activate it."
                )
            else:
                print_warning(
                    f"Project {project} has no override script; it inherits the site-wide script."
                )
        data = {
            "id": project,
            "scope": "project",
            "project": project,
            "enabled": service.project_enabled(project),
            "script": script or "",
        }

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@anon.command("set")
@read_script_argument
@click.option(
    "--project",
    "-P",
    help="Set this project's override script instead of the site-wide script",
)
@confirm_destructive("Replace this anonymization script?")
@global_options
@handle_errors
@require_auth
def anon_set(
    ctx: Context,
    script: str,
    script_source: str,
    project: str | None,
    dry_run: bool,
) -> None:
    """Replace an anonymization script from FILE.

    FILE is a path to a DicomEdit script, or ``-`` to read it from stdin.
    Without ``-P``, replaces the site-wide script; with ``-P``, replaces
    that project's override. This script is what stands between PHI and the
    archive for its scope, so ``--dry-run`` prints a unified diff against
    what is currently on the server rather than a generic preview line.

    \b
    Example:
        xnatctl anon set script.dicomedit --yes
        xnatctl anon set script.dicomedit -P MYPROJ --yes
        cat script.dicomedit | xnatctl anon set - --yes
        xnatctl anon set script.dicomedit --dry-run
    """
    service = AnonymizeService(ctx.get_client())
    label = _scope_label(project)

    if project is not None:
        project = validate_project_id(project)
        # Same read set_project_script's own preflight performs (project
        # existence) -- calling it here means dry-run refuses a typo'd -P
        # exactly as execution would, and gives the diff its "current" side.
        current = service.get_project_script(project) or ""
    else:
        current = service.get_site_script()

    if dry_run:
        diff = _text_diff(current, script, label=f"{label} anonymization script")
        click.echo(
            f"[DRY-RUN] Would replace the {label} anonymization script from {script_source}:",
            err=True,
        )
        click.echo(diff if diff else "(no changes)", err=True)
        return

    if project is not None:
        service.set_project_script(project, script)
    else:
        service.set_site_script(script)
    print_success(f"Replaced the {label} anonymization script from {script_source}")
    # The matching GET is eventually consistent -- measured live, a read
    # issued right after this write still serves the OLD script for about
    # a second. Without this line, the obvious next thing a user does
    # (`anon show`, to check) reports the previous script and reads as a
    # silent failure. See AnonymizeService.set_site_script.
    print_warning("`anon show` may report the previous script for a moment; the write has landed.")


@anon.command("enable")
@click.option(
    "--project",
    "-P",
    help="Enable this project's override script instead of the site-wide script",
)
@confirm_destructive("Enable this anonymization script?")
@global_options
@handle_errors
@require_auth
def anon_enable(ctx: Context, project: str | None, dry_run: bool) -> None:
    """Enable the site-wide anonymization script, or one project's override.

    \b
    Example:
        xnatctl anon enable --yes
        xnatctl anon enable -P MYPROJ --yes
        xnatctl anon enable -P MYPROJ --dry-run
    """
    service = AnonymizeService(ctx.get_client())
    label = _scope_label(project)

    if project is not None:
        project = validate_project_id(project)

    if dry_run:
        if project is not None:
            # Same preflight set_project_enabled() runs before its PUT: a
            # dry run that skipped this would report success for a project
            # whose real PUT would 500 (no override script set yet).
            service.check_project_enable_scope(project)
        click.echo(f"[DRY-RUN] Would enable the {label} anonymization script", err=True)
        return

    if project is not None:
        service.set_project_enabled(project, True)
    else:
        service.set_site_enabled(True)
    print_success(f"Enabled the {label} anonymization script")


@anon.command("disable")
@click.option(
    "--project",
    "-P",
    help="Disable this project's override script instead of the site-wide script",
)
@confirm_destructive("Disable this anonymization script?")
@global_options
@handle_errors
@require_auth
def anon_disable(ctx: Context, project: str | None, dry_run: bool) -> None:
    """Disable the site-wide anonymization script, or one project's override.

    \b
    Example:
        xnatctl anon disable --yes
        xnatctl anon disable -P MYPROJ --yes
        xnatctl anon disable -P MYPROJ --dry-run
    """
    service = AnonymizeService(ctx.get_client())
    label = _scope_label(project)

    if project is not None:
        project = validate_project_id(project)

    if dry_run:
        if project is not None:
            service.check_project_enable_scope(project)
        click.echo(f"[DRY-RUN] Would disable the {label} anonymization script", err=True)
        return

    if project is not None:
        service.set_project_enabled(project, False)
    else:
        service.set_site_enabled(False)
    print_success(f"Disabled the {label} anonymization script")
