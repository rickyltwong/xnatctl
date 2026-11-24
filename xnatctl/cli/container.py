"""Container Service `container` commands for xnatctl."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import IO, Any

import click

from xnatctl.cli.common import (
    Context,
    _make_alias_cb,
    confirm_destructive,
    default_project_from_context,
    global_options,
    handle_errors,
    require_auth,
    require_project_from_context,
)
from xnatctl.core.exceptions import InputValidationError
from xnatctl.core.output import OutputFormat, print_output, print_success
from xnatctl.services.containers import ContainerService

_UNKNOWN = "-"

_TERMINAL_CONTAINER_STATUSES = frozenset({"Complete", "Failed", "Killed", "Died"})

_POLL_INTERVAL_SECONDS = 5.0


def _parsed_instant(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def _started_display(row: dict[str, Any]) -> str:
    """Best-effort "started" display for one container row.

    There is no ``start-time`` field on the Container DTO (verified from the
    plugin's own ``@JsonProperty`` declarations -- see
    ``services/containers.py``'s module docstring). A genuine start time has
    to come from ``history``: its entries are timestamped by
    ``time-recorded``, and the earliest one is when the container started.
    Falls back to the top-level ``status-time`` (the container's current
    status timestamp, not necessarily when it started) when ``history`` is
    empty or absent, then to an explicit placeholder.

    "Earliest" is decided on parsed instants, not on the strings. These
    timestamps carry a UTC offset, and offsets vary within a single server's
    history across a DST boundary -- so ``01:10-0500`` sorts before
    ``01:50-0400`` lexicographically while being the later moment. Entries
    whose timestamp will not parse are still eligible, ordered after every
    parseable one, so a malformed entry cannot win by accident. Whichever
    entry wins, the server's own string is what gets displayed.
    """
    history = row.get("history")
    if isinstance(history, list):
        timestamps = [
            str(entry["time-recorded"])
            for entry in history
            if isinstance(entry, dict) and entry.get("time-recorded")
        ]
        if timestamps:
            unparseable = (True, datetime.max.replace(tzinfo=UTC))

            def sort_key(raw: str) -> tuple[bool, datetime]:
                parsed = _parsed_instant(raw)
                return unparseable if parsed is None else (False, parsed)

            return min(timestamps, key=sort_key)

    value = row.get("status-time")
    if value:
        return str(value)

    return _UNKNOWN


@click.group()
def container() -> None:
    """Manage XNAT Container Service containers."""
    pass


@container.command("list")
@click.option(
    "--project",
    "-P",
    help="Project ID. Defaults to the profile's default_project; falls back to the "
    "site-wide endpoint when neither is set.",
)
@click.option("--status", help="Filter by container status (e.g. Running, Complete)")
@global_options
@handle_errors
@require_auth
def container_list(ctx: Context, project: str | None, status: str | None) -> None:
    """List containers, scoped to a project when one is known.

    Uses ``GET /xapi/projects/{project}/containers`` when a project is
    given via ``-P`` or the profile's ``default_project``; otherwise falls
    back to the site-wide ``GET /xapi/containers``, which every container
    on the server -- not just this project's -- can appear in.

    \b
    Example:
        xnatctl container list
        xnatctl container list -P MYPROJ
        xnatctl container list --status Running
        xnatctl container list -o json
    """
    resolved_project = project if project is not None else default_project_from_context(ctx)
    service = ContainerService(ctx.get_client())
    rows = service.list(project=resolved_project, status=status)

    for row in rows:
        row["started-display"] = _started_display(row)

    print_output(
        rows,
        format=ctx.output_format,
        columns=["id", "status", "command-id", "project", "user-id", "started-display"],
        column_labels={
            "id": "ID",
            "status": "Status",
            "command-id": "Command ID",
            "project": "Project",
            "user-id": "User",
            "started-display": "Started",
        },
        quiet=ctx.quiet,
        id_field="id",
    )


@container.command("show")
@click.argument("container_id")
@global_options
@handle_errors
@require_auth
def container_show(ctx: Context, container_id: str) -> None:
    """Show one container's full record.

    CONTAINER_ID accepts either the numeric database ID or the Docker
    container ID string.

    \b
    Example:
        xnatctl container show 501
        xnatctl container show weu6k9c3f1a2
        xnatctl container show 501 -o json
    """
    service = ContainerService(ctx.get_client())
    data = service.get(container_id)

    print_output(data, format=ctx.output_format, quiet=ctx.quiet, id_field="id")


def _follow_logs(
    service: ContainerService,
    container_id: str,
    stream_name: str,
    sink: IO[bytes],
    interval: float,
) -> None:
    seen = 0
    try:
        while True:
            with service.stream_logs(container_id, stream=stream_name) as response:
                body = response.read()
            if len(body) > seen:
                sink.write(body[seen:])
                sink.flush()
                seen = len(body)
            status = str(service.get(container_id).get("status", ""))
            if status in _TERMINAL_CONTAINER_STATUSES:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        return


@container.command("logs")
@click.argument("container_id")
@click.option("--stderr", "stderr_stream", is_flag=True, help="Show stderr instead of stdout")
@click.option(
    "--follow",
    is_flag=True,
    help="Keep polling for new log output until the container reaches a terminal "
    "status, or Ctrl+C. Re-fetches the full log each interval (see docstring for why).",
)
@click.option(
    "-f",
    "legacy_follow_f",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_make_alias_cb("-f (container logs)", "follow", True),
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0, min_open=True),
    default=_POLL_INTERVAL_SECONDS,
    show_default=True,
    help="--follow poll interval, in seconds.",
)
@global_options
@handle_errors
@require_auth
def container_logs(
    ctx: Context,
    container_id: str,
    stderr_stream: bool,
    follow: bool,
    interval: float,
) -> None:
    """Show a container's captured log output.

    Streams the raw log body byte-for-byte to stdout -- no text decoding,
    no added trailing newline -- since a container log can run to
    multiple gigabytes. Without ``--follow``, re-run to refresh.

    ``--follow`` is poll-and-refetch, not a true incremental tail: each
    poll re-fetches the entire log and prints only the bytes not already
    written. See ``_follow_logs``'s docstring for why this project did not
    build ``--follow`` on the ``logSince`` route instead.

    \b
    Example:
        xnatctl container logs 501
        xnatctl container logs 501 --stderr
        xnatctl container logs 501 --follow
    """
    service = ContainerService(ctx.get_client())
    stream_name = "stderr" if stderr_stream else "stdout"
    sink = click.get_binary_stream("stdout")

    if follow:
        _follow_logs(service, container_id, stream_name, sink, interval)
        return

    with service.stream_logs(container_id, stream=stream_name) as response:
        for chunk in response.iter_bytes():
            sink.write(chunk)


def _parse_params(pairs: tuple[str, ...]) -> dict[str, str]:
    params: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise InputValidationError(
                f"--param {raw!r} is not in KEY=VALUE form.", field="param", value=raw
            )
        key, value = raw.split("=", 1)
        if not key:
            raise InputValidationError(
                f"--param {raw!r} has an empty key.", field="param", value=raw
            )
        if key in params:
            raise InputValidationError(
                f"--param {key!r} was given more than once.", field="param", value=raw
            )
        params[key] = value
    return params


def _locate_launched_container(
    service: ContainerService,
    *,
    project: str,
    wrapper_id: int,
    workflow_id: str,
    known_ids: set[str],
) -> str | None:
    for row in service.list(project=project):
        row_id = str(row.get("id"))
        matches_workflow = str(row.get("workflow-id")) == workflow_id
        matches_new = (
            workflow_id == "To be assigned"
            and row.get("wrapper-id") == wrapper_id
            and row_id not in known_ids
        )
        if matches_workflow or matches_new:
            return row_id
    return None


def _wait_for_launch(
    ctx: Context,
    service: ContainerService,
    *,
    project: str,
    wrapper_id: int,
    launch_result: dict[str, Any],
    known_ids: set[str],
    timeout: int,
) -> dict[str, Any]:
    workflow_id = str(launch_result.get("workflow-id"))
    deadline = time.monotonic() + timeout

    if not ctx.quiet:
        click.echo(f"Waiting for a container matching workflow-id {workflow_id}...", err=True)

    container_id: str | None = None
    while container_id is None:
        container_id = _locate_launched_container(
            service,
            project=project,
            wrapper_id=wrapper_id,
            workflow_id=workflow_id,
            known_ids=known_ids,
        )
        if container_id is not None:
            break
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"--wait timed out after {timeout}s: no container matching this launch "
                f"appeared under project {project!r}."
            )
        time.sleep(_POLL_INTERVAL_SECONDS)

    if not ctx.quiet:
        click.echo(f"Found container {container_id}; waiting for a terminal status...", err=True)

    while True:
        container = service.get(container_id)
        status = str(container.get("status", ""))
        if not ctx.quiet:
            click.echo(f"  Status: {status}", err=True)
        if status in _TERMINAL_CONTAINER_STATUSES:
            return container
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"--wait timed out after {timeout}s: container {container_id} is still {status!r}."
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


@container.command("launch")
@click.argument("wrapper_ref", metavar="WRAPPER")
@click.option(
    "--project",
    "-P",
    help="Project ID to launch in. Falls back to the profile's default_project.",
)
@click.option(
    "--experiment",
    "-E",
    help="Session accession ID or label, mapped to the wrapper's single Session-typed "
    "external input. Requires the wrapper to have exactly one -- see --param otherwise.",
)
@click.option(
    "--param",
    "params_raw",
    multiple=True,
    metavar="KEY=VALUE",
    help="Launch parameter, matching a wrapper input name (repeatable).",
)
@click.option(
    "--wait", is_flag=True, help="Wait for the launched container to reach a terminal status."
)
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=3600,
    show_default=True,
    help="--wait timeout, in seconds.",
)
@click.option("--dry-run", is_flag=True, help="Preview the launch request without sending it.")
@global_options
@handle_errors
@require_auth
def container_launch(
    ctx: Context,
    wrapper_ref: str,
    project: str | None,
    experiment: str | None,
    params_raw: tuple[str, ...],
    wait: bool,
    timeout: int,
    dry_run: bool,
) -> None:
    """Launch a container from a command wrapper.

    WRAPPER is a numeric wrapper ID or a wrapper name (see
    ``xnatctl wrapper list``); a name matched by wrappers on more than one
    command is rejected -- pass the numeric ID instead.

    The Container Service launch route does not validate the wrapper,
    project scope, or required inputs before queueing the launch -- it
    answers "success" even for a wrapper ID that does not exist. This
    command resolves and validates the wrapper client-side first so a
    typo'd WRAPPER fails loudly instead of silently queueing nothing.

    This is the group's normal-use verb (no ``--yes`` confirmation, in
    line with ``pipeline run``'s precedent) -- ``--dry-run`` is still
    available to preview without sending anything.

    \b
    Example:
        xnatctl container launch dcm2niix-scan -P MYPROJ -E XNAT_E00001
        xnatctl container launch 34 -P MYPROJ --param greeting=hello
        xnatctl container launch 34 -P MYPROJ -E XNAT_E00001 --wait
        xnatctl container launch 34 -P MYPROJ --dry-run
    """
    resolved_project = require_project_from_context(ctx, project)
    params = _parse_params(params_raw)

    service = ContainerService(ctx.get_client())
    wrapper_id, wrapper = service.resolve_wrapper(wrapper_ref)
    launch_params = service.preflight_launch(
        resolved_project, wrapper, params, experiment=experiment
    )

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would launch wrapper {wrapper_id} ({wrapper.get('name')}) in "
            f"project {resolved_project} with params: {launch_params}",
            err=True,
        )
        return

    if not ctx.quiet:
        click.echo(
            f"Launching wrapper {wrapper_id} ({wrapper.get('name')}) in project "
            f"{resolved_project}...",
            err=True,
        )

    known_ids = (
        {str(row.get("id")) for row in service.list(project=resolved_project)} if wait else set()
    )

    result = service.launch(resolved_project, wrapper_id, launch_params)

    if str(result.get("status", "")).lower() == "failure":
        message = result.get("message") or "no message returned"
        raise click.ClickException(f"Launch failed: {message}")

    if wait:
        container = _wait_for_launch(
            ctx,
            service,
            project=resolved_project,
            wrapper_id=wrapper_id,
            launch_result=result,
            known_ids=known_ids,
            timeout=timeout,
        )
        print_output(container, format=ctx.output_format, quiet=ctx.quiet, id_field="id")
        return

    if ctx.output_format == OutputFormat.JSON:
        print_output(result, format=OutputFormat.JSON)
    elif not ctx.quiet:
        print_success(
            f"Launch queued: workflow-id={result.get('workflow-id')} "
            f"wrapper-id={result.get('wrapper-id')}"
        )


@container.command("kill")
@click.argument("container_id")
@confirm_destructive("Kill this container?")
@global_options
@handle_errors
@require_auth
def container_kill(ctx: Context, container_id: str, dry_run: bool) -> None:
    """Kill a running container.

    CONTAINER_ID accepts either the numeric database ID or the Docker
    container ID string.

    \b
    Example:
        xnatctl container kill 501 --yes
        xnatctl container kill 501 --dry-run
    """
    service = ContainerService(ctx.get_client())

    if dry_run:
        service.get(container_id)
        click.echo(f"[DRY-RUN] Would kill container {container_id}", err=True)
        return

    service.kill_container(container_id)
    print_success(f"Killed container {container_id}")
