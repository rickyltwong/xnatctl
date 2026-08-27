"""Admin commands for xnatctl."""

from __future__ import annotations

import json
import re
from concurrent.futures import as_completed

import click

from xnatctl.cli.common import (
    Context,
    _make_forwarding_alias_cb,
    apply_filter,
    apply_sort_limit,
    confirm_destructive,
    confirm_destructive_when,
    global_options,
    handle_errors,
    list_options,
    parallel_options,
    require_auth,
    resolve_columns,
    resolve_workers_from_context,
)
from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.exceptions import XNATCtlError
from xnatctl.core.output import (
    OutputFormat,
    create_progress,
    print_error,
    print_output,
    print_success,
    print_warning,
)
from xnatctl.core.validation import validate_project_id
from xnatctl.services.admin import AdminService
from xnatctl.services.docker_admin import DockerAdminService


@click.group()
def admin() -> None:
    """Administrative operations."""
    pass


@admin.command("refresh-catalogs")
@click.argument("project")
@click.option(
    "--option",
    "-O",
    multiple=True,
    type=click.Choice(["checksum", "delete", "append", "populateStats"]),
    help="Refresh options (can repeat)",
)
@click.option(
    "--experiment",
    "-E",
    multiple=True,
    # Eager so it is processed before the deprecated -e alias, which merges
    # into this option's value (see _make_forwarding_alias_cb).
    is_eager=True,
    help="Specific experiment IDs (can repeat)",
)
@click.option(
    "-e",
    "legacy_experiment_e",
    multiple=True,
    hidden=True,
    expose_value=False,
    callback=_make_forwarding_alias_cb("-e", "experiment"),
)
@click.option("--limit", type=int, help="Limit number of experiments")
@parallel_options
@global_options
@handle_errors
@require_auth
def admin_refresh_catalogs(  # noqa: C901  # pre-existing; see pyproject
    ctx: Context,
    project: str,
    option: tuple[str, ...],
    experiment: tuple[str, ...],
    limit: int | None,
    workers: int | None,
) -> None:
    """Refresh catalog XMLs for project experiments.

    \b
    Options:
    - checksum: Generate missing checksums
    - delete: Remove entries without files
    - append: Add entries for new files
    - populateStats: Update resource statistics

    \b
    Example:
        xnatctl admin refresh-catalogs MYPROJ
        xnatctl admin refresh-catalogs MYPROJ --option checksum --option delete
        xnatctl admin refresh-catalogs MYPROJ --experiment XNAT_E00001 --experiment XNAT_E00002
    """
    project = validate_project_id(project)
    service = AdminService(ctx.get_client())
    options = list(option) if option else None
    experiment_ids = list(experiment) if experiment else None

    # Get experiments
    results = service.list_experiments_for_refresh(project)

    experiments = []
    for entry in results:
        exp_id = entry.get("ID", "").strip()
        subject_id = entry.get("subject_ID", "").strip()

        if exp_id and subject_id:
            experiments.append((subject_id, exp_id))

    if not experiments:
        click.echo(f"No experiments found for project {project}", err=True)
        return

    # Filter by specific IDs
    if experiment_ids:
        targets = set(experiment_ids)
        experiments = [exp for exp in experiments if exp[1] in targets]

    # Apply limit. `is not None`, not truthy: `--limit 0` must mean "process
    # zero experiments", not "no limit" -- the CLI does its own PUT-per-
    # experiment fan-out here (see the comment on AdminService above), so
    # this check is separate from the service layer's own limit handling.
    if limit is not None:
        experiments = experiments[:limit]

    if not experiments:
        click.echo("No experiments matched selection", err=True)
        return

    workers = resolve_workers_from_context(ctx, workers)

    # Prepare options parameter
    options_param = ",".join(options) if options else None

    refreshed = []
    failed = []

    def refresh_one(exp: tuple[str, str]) -> tuple[str, bool, str]:
        """Refresh a single experiment catalog and return status."""
        subject_id, exp_id = exp
        resource_path = f"/archive/projects/{project}/subjects/{subject_id}/experiments/{exp_id}"
        try:
            resp = service.refresh_catalog(resource_path, options_param)
            return exp_id, resp.status_code == 200, ""
        except Exception as e:  # noqa: BLE001 -- per-experiment isolation in catalog-refresh batch
            return exp_id, False, str(e)

    with create_progress() as progress:
        task = progress.add_task("Refreshing catalogs...", total=len(experiments))

        if workers > 1 and len(experiments) > 1:
            with cancellable_pool(min(workers, len(experiments))) as (executor, _token):
                futures = {executor.submit(refresh_one, exp): exp for exp in experiments}
                for future in as_completed(futures):
                    exp_id, success, error = future.result()
                    if success:
                        refreshed.append(exp_id)
                    else:
                        failed.append((exp_id, error))
                    progress.advance(task)
        else:
            for exp in experiments:
                exp_id, success, error = refresh_one(exp)
                if success:
                    refreshed.append(exp_id)
                else:
                    failed.append((exp_id, error))
                progress.advance(task)

    if ctx.output_format == OutputFormat.JSON:
        print_output(
            {
                "project": project,
                "refreshed": refreshed,
                "failed": [{"id": eid, "error": err} for eid, err in failed],
                "count": len(refreshed),
            },
            format=OutputFormat.JSON,
        )
    else:
        if refreshed:
            print_success(f"Refreshed {len(refreshed)} experiments")
        if failed:
            print_error(f"Failed to refresh {len(failed)} experiments")
            for exp_id, error in failed[:5]:
                click.echo(f"  - {exp_id}: {error}", err=True)


@admin.command("audit")
@click.option("--project", "-P", help="Filter by project")
@click.option("--user", "-u", "username", help="Filter by user")
@click.option("--action", help="Filter by action type")
@click.option("--since", help="Time range (e.g., '7d', '2024-01-01')")
@click.option(
    "--limit", type=click.IntRange(min=0), default=50, help="Max results (sent to the server)"
)
@list_options(include_limit=False)
@global_options
@handle_errors
@require_auth
def admin_audit(
    ctx: Context,
    project: str | None,
    username: str | None,
    action: str | None,
    since: str | None,
    limit: int,
    filter_expr: str | None,
    sort_by: str | None,
    columns: str | None,
) -> None:
    """View audit log (if available).

    Note: Audit log availability depends on XNAT server configuration.

    \b
    Example:
        xnatctl admin audit --project MYPROJ --limit 20
        xnatctl admin audit --user admin --since 7d
        xnatctl admin audit --filter 'action:DELETE' --sort-by timestamp:desc
    """
    service = AdminService(ctx.get_client())

    # A client-side --filter/--sort-by must see the full result set it is
    # filtering/sorting over, not just whatever fits in the server's
    # small default --limit window (50) -- fetching that window FIRST and
    # filtering/sorting second would silently drop matches outside it. Only
    # the network round-trip below is allowed to fail as "audit log not
    # available"; option validation (bad --filter/--sort-by/--columns) must
    # raise its own error, not be swallowed by that except clause.
    client_side_controls = bool(filter_expr or sort_by)
    fetch_limit = None if client_side_controls else limit

    try:
        # Try the audit API endpoint
        resp = service.get_xapi_audit(
            fetch_limit, project=project, username=username, action=action
        )
    except (XNATCtlError, json.JSONDecodeError) as e:
        # Audit API may not be available
        print_error(f"Audit log not available: {e}")
        click.echo("Note: Audit logging may not be enabled on this XNAT server", err=True)
        raise SystemExit(1) from e

    if isinstance(resp, list):
        entries = resp
    else:
        entries = resp.get("ResultSet", {}).get("Result", resp.get("items", []))

    if client_side_controls:
        entries = apply_filter(entries, filter_expr)
        entries = apply_sort_limit(entries, sort_by, limit)
    else:
        entries = entries[:limit]

    default_columns = ["timestamp", "user", "action", "resource", "project"]
    resolved_columns = resolve_columns(default_columns, columns)

    if not entries:
        click.echo("No audit entries found", err=True)
        return

    print_output(
        entries,
        format=ctx.output_format,
        columns=resolved_columns,
        quiet=ctx.quiet,
    )


_SECRET_KEY_PATTERN = re.compile(r"password|secret|token|key|private|captcha", re.IGNORECASE)


def _looks_like_secret_key(key: str) -> bool:
    """Whether a site-config KEY name looks secret-shaped.

    Site-config keys are free-form and server-specific (``emailSmtpPassword``,
    ``oauthClientSecret``, ...) -- there is no fixed allowlist to check
    against, so this is a best-effort substring match on the key name itself.
    """
    return bool(_SECRET_KEY_PATTERN.search(key))


@admin.group("site-config")
def site_config() -> None:
    """Site configuration commands."""
    pass


@site_config.command("get")
@click.argument("key", required=False)
@global_options
@handle_errors
@require_auth
def site_config_get(ctx: Context, key: str | None) -> None:
    """Get site configuration.

    Without KEY, dumps the entire site configuration -- a large payload, so
    consider ``-o json`` piped into ``jq`` if you only need one field.

    \b
    Example:
        xnatctl admin site-config get siteId
        xnatctl admin site-config get -o json
    """
    service = AdminService(ctx.get_client())

    if key is None:
        print_warning("Dumping the entire site configuration; pass a KEY for a single value.")
        print_output(service.get_site_config(), format=ctx.output_format, quiet=ctx.quiet)
        return

    print_output(service.get_site_config(key=key), format=ctx.output_format, quiet=ctx.quiet)


@site_config.command("set")
@click.argument("key")
@click.argument("value")
@confirm_destructive("Update this site configuration value?")
@global_options
@handle_errors
@require_auth
def site_config_set(ctx: Context, key: str, value: str, dry_run: bool) -> None:
    """Set a site configuration value.

    \b
    Example:
        xnatctl admin site-config set siteId MyXNAT --yes
        xnatctl admin site-config set siteId MyXNAT --dry-run
    """
    service = AdminService(ctx.get_client())

    if dry_run:
        current: object
        try:
            current = service.get_site_config(key=key)
        except (XNATCtlError, json.JSONDecodeError):
            current = "<unknown>"
        if _looks_like_secret_key(key):
            current, display_value = "***", "***"
        else:
            display_value = value
        click.echo(f"[DRY-RUN] {key}: {current} -> {display_value}", err=True)
        return

    service.set_site_config(key, value)
    display_value = "***" if _looks_like_secret_key(key) else value
    print_success(f"Set {key} = {display_value}")


@admin.group("plugins", invoke_without_command=True)
@global_options
@handle_errors
@require_auth
def plugins(ctx: Context) -> None:
    """List installed XNAT plugins, or inspect one with `plugins show ID`.

    \b
    Example:
        xnatctl admin plugins
        xnatctl admin plugins -o json
        xnatctl admin plugins show containers
    """
    click_ctx = click.get_current_context()
    if click_ctx.invoked_subcommand is not None:
        return

    service = AdminService(ctx.get_client())
    rows = service.list_plugins()

    print_output(
        rows,
        format=ctx.output_format,
        columns=["id", "name", "version"],
        column_labels={"id": "ID", "name": "Name", "version": "Version"},
        quiet=ctx.quiet,
        id_field="id",
    )


@plugins.command("show")
@click.argument("plugin_id")
@global_options
@handle_errors
@require_auth
def plugin_show(ctx: Context, plugin_id: str) -> None:
    """Show details for one installed plugin.

    \b
    Example:
        xnatctl admin plugins show containers
    """
    service = AdminService(ctx.get_client())
    data = service.get_plugin(plugin_id)

    if not data:
        print_error(f"Plugin not found: {plugin_id}")
        raise SystemExit(1)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


@admin.command("version")
@global_options
@handle_errors
@require_auth
def admin_version(ctx: Context) -> None:
    """Show XNAT server build/version information.

    \b
    Example:
        xnatctl admin version
        xnatctl admin version -q
    """
    service = AdminService(ctx.get_client())
    info = service.get_server_info()

    if ctx.quiet:
        click.echo(info.get("version", ""))
        return

    print_output(info, format=ctx.output_format, quiet=ctx.quiet, id_field="version")


@admin.group("docker")
def docker() -> None:
    """Container Service docker daemon administration."""
    pass


@docker.command("images")
@global_options
@handle_errors
@require_auth
def docker_images(ctx: Context) -> None:
    """List docker images known to the configured daemon.

    \b
    Example:
        xnatctl admin docker images
        xnatctl admin docker images -o json
    """
    service = DockerAdminService(ctx.get_client())
    rows = service.images()

    # image-id and tags are the verified DockerImage @JsonProperty names
    # (see DockerAdminService.images).
    print_output(
        rows,
        format=ctx.output_format,
        columns=["image-id", "tags"],
        column_labels={"image-id": "Image ID", "tags": "Tags"},
        quiet=ctx.quiet,
        id_field="image-id",
    )


@docker.command("hubs")
@global_options
@handle_errors
@require_auth
def docker_hubs(ctx: Context) -> None:
    """List configured docker hubs.

    \b
    Example:
        xnatctl admin docker hubs
        xnatctl admin docker hubs -o json
    """
    service = DockerAdminService(ctx.get_client())
    rows = service.hubs()

    print_output(
        rows,
        format=ctx.output_format,
        columns=["id", "name", "url", "default"],
        column_labels={"id": "ID", "name": "Name", "url": "URL", "default": "Default"},
        quiet=ctx.quiet,
        id_field="id",
    )


@docker.command("pull")
@click.argument("image")
@click.option(
    "--save-commands/--no-save-commands",
    default=True,
    help="Also register any commands embedded in the image's label metadata (default: yes)",
)
@confirm_destructive("Pull this docker image?")
@global_options
@handle_errors
@require_auth
def docker_pull(ctx: Context, image: str, save_commands: bool, dry_run: bool) -> None:
    """Pull a docker image from the default hub.

    Requires a Docker daemon reachable from the XNAT server -- with none
    configured, this fails the same way ``admin docker images``/``server``
    do (an actionable message, not a raw Java exception).

    \b
    Example:
        xnatctl admin docker pull xnat/dcm2niix:v1.2 --yes
        xnatctl admin docker pull xnat/dcm2niix:v1.2 --no-save-commands --yes
        xnatctl admin docker pull xnat/dcm2niix:v1.2 --dry-run
    """
    if dry_run:
        click.echo(f"[DRY-RUN] Would pull {image} (save-commands={save_commands})", err=True)
        return

    service = DockerAdminService(ctx.get_client())
    service.pull_image(image, save_commands=save_commands)
    print_success(f"Pulled {image}")


@docker.command("server")
@click.option("--set-host", help="Set the Docker daemon host/socket URL")
@confirm_destructive_when(
    lambda kw: kw.get("set_host") is not None,
    "Change the Docker daemon connection?",
)
@global_options
@handle_errors
@require_auth
def docker_server(ctx: Context, set_host: str | None, dry_run: bool) -> None:
    """Get or set the configured docker daemon connection.

    Without ``--set-host``, this is a plain read and never prompts.

    \b
    Example:
        xnatctl admin docker server
        xnatctl admin docker server -o json
        xnatctl admin docker server --set-host unix:///var/run/docker.sock --yes
        xnatctl admin docker server --set-host tcp://localhost:2376 --dry-run
    """
    service = DockerAdminService(ctx.get_client())

    if set_host is None:
        data = service.get_server()
        # No explicit `columns`: this shows every key the server returned,
        # rather than a fixed subset -- so `ping` (present on
        # DockerServerWithPing, the shape a live daemon connection returns)
        # surfaces automatically. A dead daemon is the common real-world
        # state here, and `ping` is the one field that tells the reader
        # that at a glance.
        print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="host")
        return

    if dry_run:
        # Runs the same read-and-merge preflight `set_server` does (an
        # unreadable or malformed current configuration must refuse here
        # too, not just on real execution) without sending the POST.
        service.build_set_server_body(set_host)
        click.echo(f"[DRY-RUN] Would set the Docker daemon host to {set_host}", err=True)
        return

    result = service.set_server(set_host)
    print_success(f"Set Docker daemon host to {set_host}")
    print_output(result, format=ctx.output_format, quiet=ctx.quiet, id_field="host")
