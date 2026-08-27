"""``session normalize-labels`` -- standardize experiment labels for a project.

Split out from ``session.py`` for the same reason as ``session_query.py``/
``session_upload.py``: importing this module registers the command on the
``session`` group via decorator side effect; see ``xnatctl/cli/main.py`` for
the import that triggers it.
"""

from __future__ import annotations

import click

from xnatctl.cli.common import (
    Context,
    confirm_destructive,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
)
from xnatctl.cli.session import session
from xnatctl.core.output import print_success
from xnatctl.core.validation import validate_project_id
from xnatctl.services.session_labels import SessionLabelService


@session.command("normalize-labels")
@click.option("--project", "-P", help="Project ID (defaults to profile default_project)")
@confirm_destructive("Rename experiment labels to the standardized convention?")
@global_options
@handle_errors
@require_auth
def session_normalize_labels(ctx: Context, project: str | None, dry_run: bool) -> None:
    """Normalize experiment labels to {SUBJECT}_{VISIT:02d}_SE{SESSION:02d}_{MODALITY}.

    Recomputes each experiment's target label from its subject, imaging
    date, same-day time ordering, and modality (derived from its
    ``xsiType``), then renames any experiment whose current label does not
    already match. An experiment already at its target label is left alone
    (no rewrite). Two experiments that would compute the same target label
    are both refused rather than either one being renamed silently, and an
    experiment whose target collides with another experiment's *current*
    label (outside this run's rename set) is refused the same way.

    Run ``subject rename`` first if this project's subjects still need
    their own labels normalized -- this command only touches experiment
    labels, using subjects' current labels as-is.

    \b
    Example:
        xnatctl session normalize-labels -P MYPROJ --dry-run
        xnatctl session normalize-labels -P MYPROJ --yes
    """
    project = validate_project_id(require_project_from_context(ctx, project))
    service = SessionLabelService(ctx.get_client())
    plan = service.plan_label_normalization(project)

    renames = plan["renames"]
    skipped = plan["skipped"]
    prefix = "[DRY-RUN] " if dry_run else ""

    if renames:
        click.echo(f"{prefix}Renames ({len(renames)}):")
        for item in renames:
            click.echo(f"  {item['old_label']} -> {item['new_label']}")

    if skipped:
        click.echo(f"\nSkipped ({len(skipped)}):")
        for item in skipped:
            click.echo(f"  {item['label']}: {item['reason']}")

    if dry_run:
        return

    result = service.apply_label_normalization(plan)
    if result["failed"]:
        click.echo(f"\nFailed ({len(result['failed'])}):", err=True)
        for f in result["failed"]:
            click.echo(f"  {f['old_label']} -> {f['new_label']}: {f['error']}", err=True)
        raise SystemExit(1)

    print_success(f"Renamed {result['renamed']} experiment label(s), skipped {len(skipped)}")
