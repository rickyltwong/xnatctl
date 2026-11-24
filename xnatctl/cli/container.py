"""Container Service `container` commands for xnatctl."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import IO, Any

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
from xnatctl.core.exceptions import InputValidationError, XNATCtlError
from xnatctl.core.output import OutputFormat, print_output, print_success
from xnatctl.services.containers import ContainerService

#: Placeholder shown when no started-time field is present on a container
#: row -- see _started_display().
_UNKNOWN = "-"

#: Container statuses treated as terminal by `container launch --wait` and
#: `container logs --follow`. NOT verified live -- this integration stack's
#: Docker-less server never lets a container reach any of these (see
#: services/containers.py's module docstring). Chosen from XNAT Container
#: Service's publicly documented status vocabulary, following the same
#: unverified-but-necessary precedent as PipelineService.wait's own
#: hardcoded terminal_states set (xnatctl/services/pipelines.py).
_TERMINAL_CONTAINER_STATUSES = frozenset({"Complete", "Failed", "Killed", "Died"})

#: Poll interval for `--wait` and `--follow`, in seconds.
_POLL_INTERVAL_SECONDS = 5.0


def _parsed_instant(raw: str) -> datetime | None:
    """Parse a Container Service timestamp, or ``None`` if it is not one.

    The DTO serializes ``yyyy-MM-dd'T'HH:mm:ss.SSSZ`` -- an ISO-ish string
    whose UTC offset is written without a colon (``-0400``).
    ``fromisoformat`` accepts that form on the Python versions this project
    supports; ``strptime`` is the backstop for anything it rejects.
    """
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
    """Poll the plain logs endpoint and print only newly-appeared bytes.

    Container Service 3.7.2 does have a ``logSince/{file}`` route (verified
    live: it reaches its handler and 404s cleanly for an unknown
    container), but its ``since`` parameter's CONTENT semantics -- what
    subset of a real, running container's log it actually returns -- could
    not be verified: this integration stack has no reachable Docker daemon
    (see ``services/containers.py``'s module docstring), so no container
    here has ever produced log output to probe against. What WAS verified
    live about ``since``: it takes an ISO-8601 timestamp with a ``Z`` or
    colon-delimited offset suffix (Python's ``datetime.isoformat()`` on a
    tz-aware value works directly), and rejects both epoch numbers and the
    compact, colon-less offset the DTOs themselves use for ``status-time``/
    ``time-recorded`` -- but that says nothing about what log content comes
    back for a given ``since``. Rather than build ``--follow`` on that
    unverified assumption, this re-fetches the full log each interval via
    the already-verified plain ``logs/{stream}`` endpoint and writes out
    only the suffix bytes not already printed -- a documented simpler
    mechanism instead of a guessed clever one.

    Stops when the container reaches a terminal status (see
    ``_TERMINAL_CONTAINER_STATUSES``) or on ``KeyboardInterrupt``.
    """
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
    "--interval",
    # Strictly positive: the follow loop issues two requests per pass (log
    # fetch + status), so `--interval 0` is not "poll fast", it is an
    # unthrottled loop against XNAT for as long as the container runs.
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
    """Parse repeated ``--param KEY=VALUE`` options into a dict.

    Args:
        pairs: Raw ``--param`` values, one per occurrence.

    Returns:
        Dict of key to value. A value may itself contain ``=`` -- only the
        first ``=`` in each raw string is treated as the separator.

    Raises:
        InputValidationError: If a value has no ``=``, an empty key, or a
            key repeated across more than one ``--param``. Silently letting
            the last one win would hide a launch-parameter typo -- exactly
            the sort of mistake this flag exists to make explicit.
    """
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


_PLACEHOLDER_WORKFLOW_ID = "To be assigned"


def _real_workflow_id(value: Any) -> str | None:
    """A workflow-id that identifies one workflow, as text; ``None`` for anything else.

    The launch response's placeholder, a missing or JSON-null value, an empty
    string and a boolean all name no workflow. Stringifying them blindly
    would make ``"None"`` or ``"To be assigned"`` look like an id that some
    older container row also carries.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip() and value != _PLACEHOLDER_WORKFLOW_ID:
        return value
    return None


def _raw_inputs(row: dict[str, Any]) -> dict[str, Any] | None:
    """A container row's ``raw`` inputs as ``{name: value}``: the params its launch was sent.

    Verified live on Container Service 3.7.3: a container row's ``inputs``
    is a list of ``{"type", "name", "value"}`` objects, and the ``raw``
    entries carry the launch request's own keys and values (a session
    launched with ``{"session": "/archive/experiments/E1"}`` lists exactly
    that). Returns ``None`` when the row does not say what it was sent:
    ``inputs`` missing or not a list, or a ``raw`` entry whose name is not
    a string. ``{}`` means the row records an empty input list.
    """
    inputs = row.get("inputs")
    if not isinstance(inputs, list):
        return None
    raw: dict[str, Any] = {}
    for entry in inputs:
        if not isinstance(entry, dict) or entry.get("type") != "raw":
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            return None
        raw[name] = entry.get("value")
    return raw


def _launch_candidates(
    service: ContainerService,
    *,
    project: str,
    wrapper_id: int,
    workflow_id: str | None,
    launch_params: dict[str, str],
    launcher: str | None,
    known_ids: set[str],
) -> tuple[list[str], list[str]]:
    """List the containers that could be this launch's, and the other new ones of its wrapper.

    See :func:`_wait_for_launch` for the correlation strategy and its
    verified-vs-assumed parts.

    With a real workflow-id (see :func:`_real_workflow_id`) a container
    matches when it runs the wrapper we launched and its own real
    workflow-id is the same.

    Without one, a container matches when it is not in the pre-launch
    snapshot, runs the wrapper we launched, was started by ``launcher``
    (its ``user-id`` equals the server-confirmed login exactly, as Container
    Service itself compares them), and its ``raw`` inputs
    equal the params we sent -- all of them and nothing more (see
    :func:`_raw_inputs`). Container Service records the request's own
    params as raw inputs, so a launch from this CLI is compared exactly.
    The user and the inputs are what tell our container from a
    neighbour's when several people launch the same wrapper in one project
    at once. The one case left open is the same user launching the same
    wrapper with identical params twice at once: either container ran
    that request. The caller refuses to guess when both are visible.

    Returns:
        ``(matching ids, other new ids of this wrapper)``. The second list is
        only for explaining a timeout: containers that appeared but did not
        record this launch's inputs.
    """
    matches: list[str] = []
    others: list[str] = []
    for row in service.list(project=project):
        if not isinstance(row, dict) or row.get("wrapper-id") != wrapper_id:
            continue
        row_id = str(row.get("id"))
        if workflow_id is not None:
            if _real_workflow_id(row.get("workflow-id")) == workflow_id:
                matches.append(row_id)
            continue
        if row_id in known_ids:
            continue
        user = row.get("user-id")
        if isinstance(user, str) and user == launcher and _raw_inputs(row) == launch_params:
            matches.append(row_id)
        else:
            others.append(row_id)
    return matches, others


def _confirmed_login(ctx: Context, project: str) -> str:
    """Return the server-confirmed login, which ``--wait``'s fallback matches ``user-id`` on.

    Asked only after the launch and only when its response has no real
    workflow-id, so a launch that does not need it never depends on it.
    A configured or cached username is no substitute: it can name another
    account than the session's. Neither is a login read on a session the
    lookup itself had to re-establish (a 401 re-login can use another
    account's configured credentials), so a changed session token counts as
    unconfirmed.

    Raises:
        click.ClickException: When the server does not confirm the login.
        XNATCtlError: When the lookup fails, re-raised so its exit code holds,
            after saying on stderr that the launch was queued.
    """
    client = ctx.get_client()
    unconfirmed = (
        "The launch was queued, but --wait cannot tie a container to it: the server "
        "did not confirm which user is logged in. Find it with "
        f"'xnatctl container list -P {project}'."
    )
    token = client.session_token
    try:
        login = client.current_username()
    except XNATCtlError:
        click.echo(unconfirmed, err=True)
        raise
    if not login or client.session_token != token:
        raise click.ClickException(unconfirmed)
    return login


def _wait_for_launch(
    ctx: Context,
    service: ContainerService,
    *,
    project: str,
    wrapper_id: int,
    launch_params: dict[str, str],
    launch_result: dict[str, Any],
    known_ids: set[str],
    timeout: int,
) -> dict[str, Any]:
    """Poll for the container a launch produced, then wait for a terminal status.

    There is no container id in the launch response: verified from the
    plugin's own ``LaunchReport.Success`` DTO (see
    ``services/containers.py``'s module docstring) that it carries
    ``status``/``params``/``command-id``/``wrapper-id``/``workflow-id`` and
    nothing else. This correlates on that response's ``workflow-id``
    against each listed container's own ``workflow-id`` field (a real,
    bytecode-verified Container DTO field -- see the same module
    docstring). When the response instead carries the literal placeholder
    ``"To be assigned"`` -- verified live to happen whenever the async
    launch has not resolved by response time, which is the common case --
    there is no id to match on, so this falls back to the one NEW
    container for this wrapper whose raw inputs equal ``launch_params``
    (see :func:`_launch_candidates`) and that was not present in
    ``known_ids``, a snapshot the caller must take immediately before calling
    ``service.launch()``. Taking the snapshot after the launch instead
    breaks the fallback: the launched container has often already appeared
    in the listing by then, landing it in ``known_ids`` and permanently
    excluding it from its own "new" match -- and since a container's real,
    resolved ``workflow-id`` never equals the literal string ``"To be
    assigned"``, the workflow-id path never fires either, so ``--wait``
    polls to timeout on a container that is sitting in every listing the
    whole time.

    Neither path could be exercised against a real daemon-backed launch in
    this project's integration stack: it has no reachable Docker daemon
    (see AGENTS.md), so ``GET /xapi/containers`` never showed a resulting
    container no matter how long this project waited after a launch. This
    is a best-effort, code-grounded mechanism, not a verified one -- a
    surprise here on a real server is expected until proven otherwise.

    Raises:
        click.ClickException: On a ``timeout`` with no matching container
            found, or with one found but stuck at a non-terminal status; or
            when more than one container matches, which ``--wait`` will not
            guess between; or when the fallback needs the server-confirmed
            login (see :func:`_confirmed_login`) and cannot get it.
    """
    deadline = time.monotonic() + timeout
    workflow_id = _real_workflow_id(launch_result.get("workflow-id"))
    launcher = None if workflow_id is not None else _confirmed_login(ctx, project)

    if not ctx.quiet:
        target = (
            f"workflow-id {workflow_id}"
            if workflow_id is not None
            else f"wrapper {wrapper_id} and this launch's inputs"
        )
        click.echo(f"Waiting for a container matching {target}...", err=True)

    unmatched: dict[str, None] = {}  # every poll's, in first-seen order

    def timed_out() -> click.ClickException:
        seen = (
            f" {len(unmatched)} new container(s) for wrapper {wrapper_id} appeared "
            f"({', '.join(unmatched)}), but none could be tied to this launch "
            f"(started by {launcher}, with exactly its inputs)."
            if unmatched
            else ""
        )
        return click.ClickException(
            f"--wait timed out after {timeout}s: no container matching this launch "
            f"appeared under project {project!r}.{seen} The launch itself was queued."
        )

    if time.monotonic() >= deadline:  # the login lookup counts against --timeout
        raise timed_out()
    while True:
        matches, others = _launch_candidates(
            service,
            project=project,
            wrapper_id=wrapper_id,
            workflow_id=workflow_id,
            launch_params=launch_params,
            launcher=launcher,
            known_ids=known_ids,
        )
        unmatched.update(dict.fromkeys(others))
        if len(matches) == 1:
            container_id = matches[0]
            break
        if len(matches) > 1:
            basis = (
                f"workflow-id {workflow_id}"
                if workflow_id is not None
                else "this user and these inputs"
            )
            raise click.ClickException(
                f"The launch was queued, but containers {', '.join(matches)} all match it "
                f"({basis}), so --wait cannot tell which one it started. Follow one with "
                "'xnatctl container show <id>'."
            )
        if time.monotonic() >= deadline:
            raise timed_out()
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
    "external input. Requires the wrapper to have exactly one -- see --param otherwise. "
    "With --scan, the session that holds the scan instead.",
)
@click.option(
    "--scan",
    "scan",
    metavar="SCAN_ID",
    help="Run a scan-level pipeline on this scan of the -E session. WRAPPER is then the "
    "name shown in XNAT's Run menu, matched among pipelines enabled in the project.",
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
    scan: str | None,
    params_raw: tuple[str, ...],
    wait: bool,
    timeout: int,
    dry_run: bool,
) -> None:
    """Launch a container from a command wrapper.

    WRAPPER is a numeric wrapper ID or a wrapper name (see
    ``xnatctl wrapper list``); a name matched by wrappers on more than one
    command is rejected -- pass the numeric ID instead.

    With ``--scan``, WRAPPER is the pipeline's name as XNAT's Run menu
    shows it (its label), its wrapper name, or its ID, matched only among
    the pipelines the project has enabled for that scan's type. The scan's
    address and the wrapper's input name are filled in from the session
    and scan, so nothing from ``wrapper list`` is needed.

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
        xnatctl container launch "DICOM to NIfTI" -P MYPROJ -E MYPROJ_S01_MR --scan 5
        xnatctl container launch 34 -P MYPROJ --param greeting=hello
        xnatctl container launch 34 -P MYPROJ -E XNAT_E00001 --wait
        xnatctl container launch 34 -P MYPROJ --dry-run
    """
    if scan is not None and experiment is None:
        raise click.UsageError("--scan needs -E/--experiment: the session that holds the scan.")
    resolved_project = require_project_from_context(ctx, project)
    params = _parse_params(params_raw)

    service = ContainerService(ctx.get_client())
    if scan is not None and experiment is not None:
        plan = service.plan_scan_launch(
            resolved_project, wrapper_ref, params, experiment=experiment, scan=scan
        )
        wrapper_id, wrapper_name, launch_params = plan
    else:
        wrapper_id, wrapper = service.resolve_wrapper(wrapper_ref)
        wrapper_name = str(wrapper.get("name"))
        launch_params = service.preflight_launch(
            resolved_project, wrapper, params, experiment=experiment
        )

    if dry_run:
        click.echo(
            f"[DRY-RUN] Would launch wrapper {wrapper_id} ({wrapper_name}) in "
            f"project {resolved_project} with params: {launch_params}",
            err=True,
        )
        return

    if not ctx.quiet:
        click.echo(
            f"Launching wrapper {wrapper_id} ({wrapper_name}) in project {resolved_project}...",
            err=True,
        )

    # The --wait correlation snapshot has to be taken before the launch
    # fires, not after: the launched container is often already present in
    # the listing by the time a post-launch snapshot would run, which used
    # to poison the "new container" fallback by pre-marking it known.
    # See _wait_for_launch's docstring.
    known_ids = (
        {
            str(row.get("id"))
            for row in service.list(project=resolved_project)
            if isinstance(row, dict)
        }
        if wait
        else set()
    )
    result = service.launch(resolved_project, wrapper_id, launch_params)

    # LaunchReport.Failure is declared on the plugin's own DTO (see
    # services/containers.py's module docstring) but was never observed
    # live -- every launch this project sent queued as "success" regardless
    # of validity. Still, treating a declared failure as a queued launch
    # would exit 0 and, with --wait, poll forever for a workflow-id of
    # "None".
    if str(result.get("status", "")).lower() == "failure":
        message = result.get("message") or "no message returned"
        raise click.ClickException(f"Launch failed: {message}")

    if wait:
        container = _wait_for_launch(
            ctx,
            service,
            project=resolved_project,
            wrapper_id=wrapper_id,
            launch_params=launch_params,
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
        # The same existence check `kill_container`'s own POST would
        # surface as a 404 -- run here too so `--dry-run` reports the
        # identical refusal for an unknown container id that execution
        # would (verified live: POST .../kill 404s cleanly for a bad id,
        # see ContainerService.kill_container).
        service.get(container_id)
        click.echo(f"[DRY-RUN] Would kill container {container_id}", err=True)
        return

    service.kill_container(container_id)
    print_success(f"Killed container {container_id}")
