"""ContainerService: XNAT Container Service container listing/monitoring/execution.

Every endpoint this module calls was verified live against XNAT 1.9.2.1 +
Container Service 3.7.2. The container object itself could not be observed
over REST -- reaching a running or finished container needs a reachable
Docker daemon, and this repo's integration stack deliberately does not
provide one (mounting the host Docker socket into a test container is a
privilege-escalation risk) -- but its field names are not guesses:
they were read directly out of the shipped ``container-service-3.7.2.jar``'s
compiled DTOs (the Jackson ``@JsonProperty`` values on
``org.nrg.containers.model.container.auto.Container``,
``Container.ContainerHistory``, and
``org.nrg.containers.model.command.auto.LaunchReport``), which is the
plugin's own declaration of its wire format.

Container fields: ``id``, ``command-id``, ``wrapper-id``, ``container-id``,
``container-name``, ``status``, ``status-time``, ``project``, ``user-id``,
``history``, ``image``, ``docker-image``, ``workflow-id``, ``backend``,
``subtype``, ``parent-source-object-name``, ``service-id``, ``task-id``,
``node-id``, ``command-line``, ``working-directory``, ``mounts``,
``inputs``, ``outputs``, ``ports``, ``secrets``, ``restarts``, ``swarm``,
``swarm-constraints``, ``auto-remove``, ``override-entrypoint``,
``container-labels``, ``generic-resources``, ``gpus``, ``ipc-mode``,
``limit-memory``, ``reserve-memory``, ``shm-size``, ``network``,
``runtime``, ``ulimits``. THERE IS NO ``start-time`` -- a genuine start time
comes from the earliest ``history`` entry's ``time-recorded``, not a
top-level key. ``ContainerHistory`` entries: ``status``, ``time-recorded``,
``message``, ``entity-type``, ``external-timestamp``, ``username``, ``user``.

``LaunchReport.Success`` fields (the body :meth:`ContainerService.launch`
returns): ``status``, ``params``, ``command-id``, ``wrapper-id``,
``workflow-id``. THERE IS NO ``container-id`` FIELD ON THIS TYPE -- a launch
response can never hand back a container to poll on directly; see
:meth:`ContainerService.launch` and ``cli/container.py``'s ``--wait``
handling for how this is worked around. ``LaunchReport.Failure`` carries
``message`` instead of ``workflow-id`` but was never observed live -- every
launch this project sent queued as ``"success"`` regardless of validity
(see :meth:`ContainerService.launch`).
"""

from __future__ import annotations

import builtins
import re
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import httpx

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.core.validation import quote_path_segment, validate_scan_id

from .base import BaseService
from .commands import CommandService
from .projects import ProjectService
from .sessions import SessionService

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient


_TERMINAL_STATUS_PREFIXES = ("Complete", "Failed", "Killed")


def is_terminal_status(status: str) -> bool:
    """Tell whether a container status is final, by Container Service's own rule.

    ``ContainerUtils.statusIsTerminal`` is a prefix match, so ``Failed (Upload)``
    and ``Failed (Rejected)`` are final, while a Swarm interim status such as
    ``Waiting (Failed (Rejected))`` is not.
    """
    return status.startswith(_TERMINAL_STATUS_PREFIXES)


def _expect_list(data: Any, path: str) -> builtins.list[dict[str, Any]]:
    if not isinstance(data, list):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON array, "
            f"got {type(data).__name__}."
        )
    return cast(builtins.list[dict[str, Any]], data)


def _expect_dict(data: Any, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON object, "
            f"got {type(data).__name__}."
        )
    return cast(dict[str, Any], data)


def _session_input_name(wrapper: dict[str, Any]) -> str:
    external_inputs = wrapper.get("external-inputs") or []
    session_inputs = [
        i for i in external_inputs if isinstance(i, dict) and i.get("type") == "Session"
    ]
    wrapper_ref = wrapper.get("name") or wrapper.get("id")
    if not session_inputs:
        raise InputValidationError(
            f"Wrapper {wrapper_ref!r} has no external input of type 'Session' -- "
            "-E/--experiment cannot be mapped to a launch parameter. Pass the session "
            "as --param <input-name>=<value> instead.",
            field="experiment",
        )
    if len(session_inputs) > 1:
        names = ", ".join(str(i.get("name")) for i in session_inputs)
        raise InputValidationError(
            f"Wrapper {wrapper_ref!r} has {len(session_inputs)} external inputs of type "
            f"'Session' ({names}) -- -E/--experiment is ambiguous. Pass the session as "
            "--param <input-name>=<value> instead.",
            field="experiment",
        )
    name = session_inputs[0].get("name")
    if not isinstance(name, str) or not name:
        raise XNATCtlError(
            f"Wrapper {wrapper_ref!r}'s Session external input has no usable 'name'."
        )
    return name


class ScanLaunch(NamedTuple):
    """A scan-level launch resolved by :meth:`ContainerService.plan_scan_launch`."""

    wrapper_id: int
    wrapper_label: str
    params: dict[str, str]


# A wrapper ID as typed: ASCII digits only. str.isdigit() also accepts
# characters such as "²", which int() then rejects.
_DECIMAL_ID = re.compile(r"[0-9]+")


def _scan_xsi_type(document: Any) -> str | None:
    """Read ``items[0].meta["xsi:type"]`` from a single-scan ``?format=json`` document.

    Returns ``None`` for any other shape, so the caller raises one
    ``XNATCtlError`` naming the endpoint instead of a ``KeyError`` or a
    model validation error.
    """
    items = document.get("items") if isinstance(document, dict) else None
    first = items[0] if isinstance(items, list) and items else None
    meta = first.get("meta") if isinstance(first, dict) else None
    scan_type = meta.get("xsi:type") if isinstance(meta, dict) else None
    return scan_type if isinstance(scan_type, str) and scan_type else None


def _wrapper_display_name(row: dict[str, Any]) -> str:
    """The name XNAT's Run menu shows for a ``commands/available`` row: its label, else its name."""
    return str(row.get("wrapper-label") or row.get("wrapper-name") or row.get("wrapper-id"))


class ContainerService(BaseService):
    """Service for XNAT Container Service container listing/monitoring/execution.

    Composes :class:`~xnatctl.services.commands.CommandService` by
    constructor injection (defaulting to a fresh instance) rather than
    importing wrapper-resolution logic separately -- :meth:`resolve_wrapper`
    is a thin passthrough to it, and it avoids a circular import since
    ``commands.py`` never imports this module.

    Every method that returns server records returns them as plain
    ``dict``/``list[dict]``; :meth:`plan_scan_launch` returns a small
    :class:`ScanLaunch` of values it derived -- see
    :class:`~xnatctl.services.commands.CommandService`'s docstring for why.
    """

    def __init__(self, client: XNATClient, command_service: CommandService | None = None) -> None:
        super().__init__(client)
        self._commands = command_service or CommandService(client)

    def list(
        self,
        project: str | None = None,
        status: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List containers, optionally scoped to a project and/or status.

        Args:
            project: When given, list only this project's containers via
                ``GET /xapi/projects/{project}/containers`` (verified
                live). Omitted lists site-wide via ``GET /xapi/containers``
                (also verified live) -- a site-wide call, so callers should
                prefer passing a project when one is known.
            status: Client-side status filter (e.g. ``"Running"``,
                ``"Complete"``), applied after the fetch -- there is no
                confirmed server-side status query parameter.

        Returns:
            List of container dicts -- see the module docstring for the
            verified field set.

        Raises:
            XNATCtlError: If ``status`` is given but none of the returned
                rows carry a ``status`` key at all. That is not the same
                thing as the filter legitimately matching zero containers
                (rows present, none with a matching value) -- it means the
                server sent a shape this client does not recognise, and
                silently returning ``[]`` would hide that behind what looks
                like an ordinary empty result.
        """
        if project is not None:
            path = f"/xapi/projects/{quote_path_segment(project)}/containers"
        else:
            path = "/xapi/containers"

        data = self.client.get_json(path)
        rows = _expect_list(data, path)

        if status is not None:
            if rows and not any("status" in r for r in rows):
                raise XNATCtlError(
                    f"Cannot filter by --status {status!r}: none of the {len(rows)} "
                    f"container(s) returned by GET {path} carry a 'status' field."
                )
            rows = [r for r in rows if r.get("status") == status]
        return rows

    def get(self, container_id: str) -> dict[str, Any]:
        """Get one container's full record.

        Args:
            container_id: Container ID -- the numeric database ID or the
                Docker container ID string, both accepted by this route.

        Returns:
            Container dict. The route is verified live (``GET
            /xapi/containers/{id}``); its field set is the one documented
            in the module docstring.

        Raises:
            ResourceNotFoundError: If the container does not exist (a
                nonexistent ID answers 404 with a plain-text body, verified
                live: ``"No container with ID {id}"``).
        """
        path = f"/xapi/containers/{quote_path_segment(container_id)}"
        try:
            data = self.client.get_json(path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("container", container_id) from e
        return _expect_dict(data, path)

    def stream_logs(
        self, container_id: str, stream: str = "stdout"
    ) -> AbstractContextManager[httpx.Response]:
        """Open a container's captured log output as a streaming response.

        A container's log can run to multiple gigabytes, so this hands back
        the open response rather than buffering it into a string -- the
        caller iterates ``response.iter_bytes()`` and writes each chunk out
        directly, preserving the body byte-for-byte (no added trailing
        newline, no decoding).

        Args:
            container_id: Container ID -- numeric database ID or Docker
                container ID string.
            stream: ``"stdout"`` or ``"stderr"``.

        Returns:
            A context manager yielding the open ``httpx.Response``
            (verified live: ``GET /xapi/containers/{id}/logs/{stdout|stderr}``
            returns a plain-text body, not JSON). Closed on context exit.

        Raises:
            ResourceNotFoundError: If the container does not exist. Verified
                live: a nonexistent container's ``logs/stdout`` answers
                **500**, not 404 -- unlike ``GET /xapi/containers/{id}``
                itself and ``logSince/{file}``, which both answer 404.
                A 500 is retried by the idempotent-GET retry ladder, so
                calling the logs route directly for a bad ID would walk the
                whole ladder before surfacing as ``RetryExhaustedError``
                instead of a clean not-found. This checks existence first
                via :meth:`get` (the endpoint that genuinely answers 404)
                and lets that raise instead -- cheaper than the retry
                ladder and gives the honest error.
            ClientRequestError, ServerError: mapped from the error status by
                ``XNATClient.stream()`` for any other log-fetch failure,
                once the container is confirmed to exist.
        """
        self.get(container_id)
        path = (
            f"/xapi/containers/{quote_path_segment(container_id)}/logs/{quote_path_segment(stream)}"
        )
        return self.client.stream("GET", path)

    def resolve_wrapper(self, ref: str) -> tuple[int, dict[str, Any]]:
        """Resolve a wrapper by numeric ID or name. Thin passthrough to ``CommandService``.

        See :meth:`~xnatctl.services.commands.CommandService.resolve_wrapper`
        for the full contract (name ambiguity across commands raises).
        Exists so a caller building a container launch does not need to
        construct its own ``CommandService`` -- see the class docstring.
        """
        return self._commands.resolve_wrapper(ref)

    def build_launch_params(
        self,
        wrapper: dict[str, Any],
        params: dict[str, str],
        *,
        experiment: str | None = None,
    ) -> dict[str, str]:
        """Merge ``-E``/``--experiment`` into ``--param`` values, keyed by the wrapper's own input name.

        Pure and non-mutating -- this is also the exact logic
        ``container launch --dry-run`` runs in ``cli/container.py`` to
        preview the request body without sending it.

        Args:
            wrapper: A wrapper dict, as returned by :meth:`resolve_wrapper`.
            params: Parsed ``--param KEY=VALUE`` values.
            experiment: The ``-E``/``--experiment`` value, if given.

        Returns:
            ``params`` unchanged if ``experiment`` is ``None``; otherwise a
            new dict with the wrapper's single ``Session``-typed external
            input's name mapped to ``experiment``.

        Raises:
            InputValidationError: If ``experiment`` is given and the
                wrapper's session input name collides with a key already in
                ``params`` (an explicit ``--param`` for the same input the
                CLI would also set -- ambiguous which one should win, so
                this refuses rather than guessing), or if the wrapper has
                zero or multiple ``Session``-typed external inputs (see
                :func:`_session_input_name`).
        """
        if experiment is None:
            return dict(params)
        session_key = _session_input_name(wrapper)
        if session_key in params:
            raise InputValidationError(
                f"-E/--experiment conflicts with --param {session_key}=...: the wrapper's "
                f"session input is named {session_key!r}. Use one or the other, not both.",
                field="experiment",
                value=experiment,
            )
        return {**params, session_key: experiment}

    def preflight_launch(
        self,
        project: str,
        wrapper: dict[str, Any],
        params: dict[str, str],
        *,
        experiment: str | None = None,
    ) -> dict[str, str]:
        """Validate a launch request without sending it; return the params :meth:`launch` would send.

        Runs every check :meth:`launch` needs before its ``POST``: that
        ``project`` exists (verified live -- a nonexistent project answers
        a raw, unhelpful 500 from the launch route itself, see
        :meth:`launch`), and that ``-E``/``--experiment`` maps
        unambiguously onto the wrapper's own input names (see
        :meth:`build_launch_params`). This is also the exact preflight
        ``container launch --dry-run`` runs, so a preview and the real
        launch always validate identical things.

        Args:
            project: Project ID the launch would run in.
            wrapper: A wrapper dict, as returned by :meth:`resolve_wrapper`.
            params: Parsed ``--param KEY=VALUE`` values.
            experiment: The ``-E``/``--experiment`` value, if given.

        Returns:
            The launch params :meth:`launch` would POST.

        Raises:
            ResourceNotFoundError: If ``project`` does not exist.
            InputValidationError: See :meth:`build_launch_params`.
        """
        ProjectService(self.client).get(project)
        return self.build_launch_params(wrapper, params, experiment=experiment)

    def available_wrappers(self, project: str, xsi_type: str) -> builtins.list[dict[str, Any]]:
        """List the wrappers ``project`` offers for objects of ``xsi_type``: the Run menu's source.

        ``GET /xapi/projects/{project}/commands/available?xsiType=...`` is the
        route XNAT's own Run menu reads, and it needs only Read access on the
        project (``restrictTo = Read`` in Container Service 3.7.3's
        ``CommandRestApi``). The routes :meth:`resolve_wrapper` and
        ``wrapper_enabled_state`` use are not open to a project member the
        same way: ``GET /xapi/projects/{p}/wrappers/{w}/enabled`` checks Edit
        on the project.

        Verified live on Container Service 3.7.3: a JSON array with one row
        per wrapper whose contexts include ``xsi_type``, enabled in the
        project or not, each carrying ``command-id``, ``command-name``,
        ``command-label``, ``command-description``, ``wrapper-id``,
        ``wrapper-name``, ``wrapper-label``, ``wrapper-description``,
        ``wrapper-contexts``, ``image-name``, ``image-type``, ``enabled``
        (for this project) and ``root-element-name`` (the name of the
        wrapper's input for the object being launched on). A nonexistent
        project answers 500, so callers check the project first.

        Args:
            project: Project ID.
            xsi_type: XSI type of the object a launch would run on, e.g.
                ``xnat:mrScanData``.

        Returns:
            The rows, unfiltered.

        Raises:
            XNATCtlError: If the response is not a JSON array of objects,
                each with an integer ``wrapper-id`` and a boolean ``enabled``.
                A row without them cannot be told apart from a disabled
                wrapper, so it is not quietly treated as one.
        """
        path = f"/xapi/projects/{quote_path_segment(project)}/commands/available"
        rows = _expect_list(self.client.get_json(path, params={"xsiType": xsi_type}), path)
        for index, row in enumerate(rows):
            wrapper_id = row.get("wrapper-id") if isinstance(row, dict) else None
            if (
                not isinstance(row, dict)
                or not isinstance(wrapper_id, int)
                or isinstance(wrapper_id, bool)
                or not isinstance(row.get("enabled"), bool)
            ):
                raise XNATCtlError(
                    f"Unexpected row {index} from GET {path}: expected an object with an "
                    f"integer 'wrapper-id' and a boolean 'enabled', got {row!r}."
                )
        return rows

    def resolve_available_wrapper(self, project: str, xsi_type: str, ref: str) -> dict[str, Any]:
        """Find the one wrapper enabled in ``project`` for ``xsi_type`` that ``ref`` names.

        ``ref`` matches a wrapper's ``wrapper-label`` (what the Run menu
        shows) or its ``wrapper-name``; a ``ref`` of ASCII digits only is a
        ``wrapper-id`` and nothing else, as in :meth:`resolve_wrapper`, so
        the ID the ambiguity error suggests always selects one wrapper. Only
        wrappers enabled in the project are candidates, so a disabled
        wrapper with the same label is not a conflict. Container Service
        does not keep labels or names unique, so two enabled matches are an
        error rather than a guess.

        Args:
            project: Project ID.
            xsi_type: XSI type of the object the launch runs on.
            ref: Run-menu label, wrapper name or numeric wrapper ID.

        Returns:
            The matching :meth:`available_wrappers` row.

        Raises:
            ResourceNotFoundError: If no enabled wrapper matches; its hint
                lists the ones that are enabled.
            InputValidationError: If more than one enabled wrapper matches.
        """
        enabled = [r for r in self.available_wrappers(project, xsi_type) if r["enabled"]]
        if _DECIMAL_ID.fullmatch(ref):
            matches = [r for r in enabled if r["wrapper-id"] == int(ref)]
        else:
            matches = [r for r in enabled if ref in (r.get("wrapper-label"), r.get("wrapper-name"))]
        if not matches:
            offered = ", ".join(
                sorted(f"{_wrapper_display_name(r)!r} (ID {r['wrapper-id']})" for r in enabled)
            )
            error = ResourceNotFoundError(f"pipeline for {xsi_type} in project {project}", ref)
            error.hint = (
                f"Use a pipeline enabled for {xsi_type} in {project}: {offered}."
                if offered
                else f"No pipeline is enabled for {xsi_type} in {project}; a project owner "
                "can enable one."
            )
            raise error
        if len(matches) > 1:
            candidates = ", ".join(
                f"wrapper {r.get('wrapper-id')} ({_wrapper_display_name(r)})" for r in matches
            )
            raise InputValidationError(
                f"Pipeline {ref!r} is ambiguous in project {project}: matched by "
                f"{len(matches)} enabled wrappers ({candidates}). Use the numeric wrapper ID.",
                field="wrapper",
                value=ref,
            )
        return matches[0]

    def plan_scan_launch(
        self,
        project: str,
        wrapper_ref: str,
        params: dict[str, str],
        *,
        experiment: str,
        scan: str,
    ) -> ScanLaunch:
        """Resolve a scan-level launch from what a user sees in XNAT: session, scan, Run-menu name.

        Looks up the session (label or accession ID) in ``project``, reads
        the scan's XSI type, picks the wrapper ``wrapper_ref`` names among
        those enabled in the project for that type (see
        :meth:`resolve_available_wrapper`), and sets that wrapper's own
        input (``root-element-name``) to the scan's
        ``/archive/experiments/{ID}/scans/{scan}`` URI. The launch route
        validates none of this itself (see :meth:`launch`), so every lookup
        here doubles as the check that the launch would run on something.

        Args:
            project: Project ID the launch runs in.
            wrapper_ref: Run-menu label, wrapper name or numeric wrapper ID.
            params: Parsed ``--param KEY=VALUE`` values.
            experiment: Session label or accession ID in ``project``.
            scan: Scan ID within that session.

        Returns:
            The wrapper ID, its Run-menu name, and the params :meth:`launch`
            would POST.

        Raises:
            ResourceNotFoundError: If the project, session, scan or pipeline
                does not exist (see :meth:`resolve_available_wrapper`).
            InputValidationError: If ``scan`` is not a valid scan ID, the
                pipeline is ambiguous, or ``params`` already sets the
                wrapper's scan input.
            XNATCtlError: If a response lacks a field this relies on.
        """
        scan = validate_scan_id(scan)
        ProjectService(self.client).get(project)
        session_id = SessionService(self.client).get(experiment, project=project).id
        # The lookup and the launched URI share these segments, so the scan
        # that was checked is the scan that runs.
        scan_tail = f"experiments/{quote_path_segment(session_id)}/scans/{quote_path_segment(scan)}"
        scan_path = f"/data/{scan_tail}"
        try:
            scan_document = self.client.get_json(scan_path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("scan", f"{experiment}/{scan}") from e
        scan_type = _scan_xsi_type(scan_document)
        if scan_type is None:
            raise XNATCtlError(f"Unexpected response from GET {scan_path}: no scan xsi:type.")

        wrapper = self.resolve_available_wrapper(project, scan_type, wrapper_ref)
        input_name = wrapper.get("root-element-name")
        if not isinstance(input_name, str) or not input_name:
            raise XNATCtlError(
                f"Unexpected row from GET /xapi/projects/{project}/commands/available: wrapper "
                f"{wrapper['wrapper-id']} has no usable 'root-element-name'."
            )
        if input_name in params:
            raise InputValidationError(
                f"--scan conflicts with --param {input_name}=...: the wrapper's scan input is "
                f"named {input_name!r}. Use one or the other, not both.",
                field="scan",
                value=scan,
            )
        scan_uri = f"/archive/{scan_tail}"
        return ScanLaunch(
            wrapper_id=wrapper["wrapper-id"],
            wrapper_label=_wrapper_display_name(wrapper),
            params={**params, input_name: scan_uri},
        )

    def launch(self, project: str, wrapper_id: int, params: dict[str, str]) -> dict[str, Any]:
        """Launch a container from a project-scoped wrapper.

        Verified live: ``POST /xapi/projects/{project}/wrappers/{wrapperId}/launch``
        with a JSON object body (``Content-Type: application/json``) of
        ``{input-name: value}`` pairs answers 200 with a
        ``LaunchReport.Success`` body -- see the module docstring for its
        verified key set. There is no ``container-id`` field on that type
        at all: a launch response can never hand back a container to poll
        on directly. ``cli/container.py``'s ``--wait`` works around this by
        correlating on ``workflow-id`` against :meth:`list` instead.

        *** THE SERVER DOES NOT VALIDATE THE WRAPPER, PROJECT SCOPE, OR
        REQUIRED INPUTS BEFORE QUEUEING THE LAUNCH. *** Verified live:
        POSTing to a nonexistent wrapper ID (999999) on a real project
        still answers 200 ``"success"`` with ``"workflow-id": "To be
        assigned"`` -- and no container is ever subsequently created for
        it. So a bad wrapper ID here would silently do nothing while
        reporting success, if the caller had not resolved it client-side
        first -- every call site in ``cli/container.py`` resolves the wrapper
        before calling this: via :meth:`plan_scan_launch` for ``--scan``,
        otherwise via :meth:`resolve_wrapper`.

        A nonexistent ``project`` DOES fail synchronously, but unhelpfully:
        verified live, 500 with body ``"There was an error in the request :
        null"`` (mapped to ``ServerError`` by the client, not a clean
        not-found). :meth:`preflight_launch` checks the project exists
        first specifically to avoid surfacing that raw 500.

        ``"workflow-id"`` is the literal string ``"To be assigned"`` (not a
        number) whenever the async launch has not been picked up --
        verified live, and on this daemon-less integration stack,
        permanent: querying ``GET /xapi/containers`` repeatedly afterward
        never shows a resulting container, however long the wait. Whether a
        daemon-backed server resolves this synchronously instead (a real
        numeric workflow ID in the same response) could not be verified
        here.

        Also verified live: the ``root/{xsiType}`` variant of this route
        (``POST /xapi/projects/{project}/wrappers/{wrapperId}/root/{xsiType}/launch``)
        behaves identically and does not validate ``xsiType`` either -- this
        uses the simpler form without a root segment since there is nothing
        to gain from a client-supplied, unvalidated ``xsiType``, and not
        every wrapper's root input is a session (see
        :func:`_session_input_name`).

        Args:
            project: Project ID to launch in.
            wrapper_id: Numeric wrapper ID (see :meth:`resolve_wrapper`).
            params: Launch parameter values, keyed by wrapper input name
                (see :meth:`build_launch_params`).

        Returns:
            The ``LaunchReport`` dict -- verified key set on success:
            ``"status"``, ``"params"``, ``"command-id"``, ``"wrapper-id"``,
            ``"workflow-id"``. A ``"failure"`` shape (``status="failure"``,
            with a ``"message"`` key instead of ``"workflow-id"``) is
            declared on ``LaunchReport.Failure`` in the plugin's own DTOs
            but was never observed live -- every input this project tried
            queued as ``"success"`` regardless of validity.

        Raises:
            XNATCtlError: If the response is not a JSON object.
        """
        wid = quote_path_segment(str(wrapper_id))
        path = f"/xapi/projects/{quote_path_segment(project)}/wrappers/{wid}/launch"
        resp = self.client.post(path, json=params)
        data = resp.json()
        return _expect_dict(data, path)

    def kill_container(self, container_id: str) -> None:
        """Kill a running container.

        Verified live: ``POST /xapi/containers/{id}/kill`` answers 404 with
        the same plain-text ``"No container with ID {id}"`` body as ``GET
        /xapi/containers/{id}`` for a nonexistent ID -- unlike
        ``/logs/{stream}``, which answers 500 for the same case (see
        :meth:`stream_logs`). So, unlike :meth:`stream_logs`, this needs no
        separate existence check before the mutating call: the kill
        route's own 404 is already the clean not-found signal. ``GET`` and
        ``PUT`` on this same path both answer 405 Method Not Allowed
        (verified live), confirming ``POST`` is a genuinely registered
        route rather than a catch-all matched by any method.

        Whether a successful kill (an actually-running, daemon-backed
        container) answers 200 or some other 2xx, and what its body looks
        like, could NOT be verified -- this integration stack has no
        reachable Docker daemon (see the module docstring), so no container
        has ever reached a killable state here.

        Args:
            container_id: Container ID -- numeric database ID or Docker
                container ID string.

        Raises:
            ResourceNotFoundError: If the container does not exist.
        """
        path = f"/xapi/containers/{quote_path_segment(container_id)}/kill"
        try:
            self.client.post(path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("container", container_id) from e
