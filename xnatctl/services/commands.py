"""Command Service: XNAT Container Service command/wrapper registration.

Every endpoint this module calls was verified live against XNAT 1.9.2.1 +
Container Service 3.7.2, including the mutating verbs (create, update,
delete, enable/disable, config set) -- see each method's docstring for its
own verified request/response shape. A few of those shapes are genuinely
surprising and are called out loudly at their call site: ``POST
/xapi/commands/{id}`` (update) is a full replace that silently wipes
wrappers if ``xnat`` is omitted, ``DELETE /xapi/commands/{id}`` answers 204
even for a nonexistent ID, and ``POST .../config`` does not validate the
wrapper ID at all.
"""

from __future__ import annotations

from typing import Any, cast

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError, XNATCtlError
from xnatctl.core.validation import quote_path_segment

from .base import BaseService
from .projects import ProjectService


def _expect_list(data: Any, path: str) -> list[dict[str, Any]]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``[]``.

    A server that answers 200 with something other than a JSON array is not
    "no results" -- it is a shape xnatctl does not understand, and printing
    an empty table for it hides a real failure. See ``get_json``: it maps
    non-2xx statuses to typed exceptions already, so anything reaching here
    is a 2xx body.
    """
    if not isinstance(data, list):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON array, "
            f"got {type(data).__name__}."
        )
    return cast(list[dict[str, Any]], data)


def _expect_dict(data: Any, path: str) -> dict[str, Any]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``{}``. See :func:`_expect_list`."""
    if not isinstance(data, dict):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON object, "
            f"got {type(data).__name__}."
        )
    return cast(dict[str, Any], data)


def _drop_stale_wrapper_ids(current: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Strip ``id`` from any ``xnat[]`` entry in ``payload`` that isn't identical to a current one.

    See :meth:`CommandService.update_command` for the live-verified failure
    this works around: reusing a wrapper's existing numeric ``id`` on an
    entry whose content has changed makes the server's ``POST
    /xapi/commands/{id}`` answer a Hibernate identity-conflict 500. Matching
    is by ``id`` first, then exact dict equality -- a wrapper that is
    byte-for-byte unchanged from what :meth:`CommandService.get_command`
    just returned keeps its id (stable references for anything scoped to
    that numeric id, e.g. ``wrapper enable``/``config set``); anything else
    -- a modified wrapper reusing an old id, or a genuinely new one -- has
    its id stripped so the server mints a fresh one instead of colliding
    with the one already loaded for this command in the same request.

    A payload may also legitimately contain the SAME wrapper id more than
    once (e.g. a hand-edited command.json that duplicated an entry, or an id
    that actually belongs to a different command and so isn't in
    ``current`` at all). Only the FIRST occurrence of an id that matches
    ``current`` byte-for-byte keeps it; every later occurrence of that same
    id has its id stripped regardless of content, so two payload entries
    never reach the server carrying the same id -- exactly the identity
    conflict this function exists to prevent.

    Args:
        current: The command's current definition, as returned by
            :meth:`CommandService.get_command`.
        payload: The full replacement command definition about to be sent.

    Returns:
        ``payload`` unchanged if it carries no ``xnat`` key (an intentional
        wrapper wipe needs no id handling); otherwise a shallow copy with a
        rebuilt ``xnat`` list. Non-dict entries are passed through as-is and
        left for the server to reject -- this function only knows how to
        compare wrapper objects.
    """
    if "xnat" not in payload:
        return payload

    current_by_id: dict[Any, dict[str, Any]] = {
        wrapper["id"]: wrapper
        for wrapper in current.get("xnat") or []
        if isinstance(wrapper, dict) and "id" in wrapper
    }

    retained_ids: set[Any] = set()
    rebuilt: list[Any] = []
    for wrapper in payload["xnat"] or []:
        if not isinstance(wrapper, dict) or "id" not in wrapper:
            rebuilt.append(wrapper)
            continue
        wrapper_id = wrapper["id"]
        if wrapper_id not in retained_ids and current_by_id.get(wrapper_id) == wrapper:
            retained_ids.add(wrapper_id)
            rebuilt.append(wrapper)
        else:
            rebuilt.append({k: v for k, v in wrapper.items() if k != "id"})

    return {**payload, "xnat": rebuilt}


def wrappers_of(command: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one command's embedded wrapper rows, tagged with its ``command-id``.

    Container Service embeds a command's wrappers under the key ``xnat``
    (not ``xnat-wrappers``), and exposes no wrapper-listing endpoint of its
    own, so this is the single place that reads that key. It is a
    module-level function rather than a private method because the CLI needs
    the same rules to count wrappers -- two readers of one undocumented
    shape is exactly how the ``or []`` coercion this replaces got out of
    step with the service.

    ``xnat`` absent, ``null``, or an empty list all legitimately mean "no
    wrappers". Anything else -- an object, a string, a number, or a list
    holding non-objects -- is a shape this client does not understand, and
    is NOT quietly reported as "no wrappers": that turns a plugin or schema
    regression into a successful, empty, entirely convincing listing.

    Args:
        command: One command object as returned by ``GET /xapi/commands``.
            Reads ``id`` and ``xnat``.

    Returns:
        Wrapper dicts, each carrying at least ``id``, ``name``, ``label``,
        ``description``, ``contexts``, plus the ``command-id`` added here.

    Raises:
        XNATCtlError: If ``xnat`` is present and is neither ``None``, a list
            of objects, nor an empty list.
    """
    command_id = command.get("id")
    xnat = command.get("xnat")
    if xnat is None:
        return []
    if not isinstance(xnat, list):
        raise XNATCtlError(
            f"Unexpected 'xnat' field on command {command_id!r}: expected a list of "
            f"wrapper objects or null, got {type(xnat).__name__} ({xnat!r})."
        )
    rows: list[dict[str, Any]] = []
    for index, wrapper in enumerate(xnat):
        if not isinstance(wrapper, dict):
            raise XNATCtlError(
                f"Unexpected wrapper at 'xnat[{index}]' on command {command_id!r}: "
                f"expected an object, got {type(wrapper).__name__} ({wrapper!r})."
            )
        row = dict(wrapper)
        row.setdefault("command-id", command_id)
        rows.append(row)
    return rows


class CommandService(BaseService):
    """Service for XNAT Container Service command/wrapper registration.

    Every method returns plain ``dict``/``list[dict]`` -- command/wrapper
    JSON shapes are plugin-version-dependent (the Container Service plugin
    versions independently of XNAT core and its wrapper schema has changed
    across CS releases) and have no library-consumer need distinct from the
    CLI's own rendering, per the data-flow rule in ``AGENTS.md``.
    """

    def list_commands(self) -> list[dict[str, Any]]:
        """List all registered commands.

        Verified live: ``GET /xapi/commands`` returns a bare JSON array.

        Returns:
            List of command dicts. Verified key set: ``id``, ``name``,
            ``label``, ``description``, ``version``, ``image``, ``type``,
            ``command-line``, ``mounts``, ``environment-variables``,
            ``ports``, ``inputs``, ``outputs``, ``xnat`` (the command's
            embedded wrapper definitions -- see :meth:`list_wrappers`),
            ``container-labels``, ``generic-resources``, ``ulimits``,
            ``secrets``, ``visibility``.
        """
        path = "/xapi/commands"
        data = self.client.get_json(path)
        return _expect_list(data, path)

    def get_command(self, command_id: int) -> dict[str, Any]:
        """Get one command's full definition.

        Args:
            command_id: Numeric command ID.

        Returns:
            Command dict with the same key set as :meth:`list_commands`'s
            rows. Verified live (``GET /xapi/commands/{id}``).

        Raises:
            ResourceNotFoundError: If the command does not exist.
        """
        path = f"/xapi/commands/{quote_path_segment(str(command_id))}"
        try:
            data = self.client.get_json(path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("command", str(command_id)) from e
        return _expect_dict(data, path)

    def list_wrappers(self, command_id: int | None = None) -> list[dict[str, Any]]:
        """List command wrappers, derived client-side from each command's ``xnat`` array.

        There is no server-side wrapper-listing endpoint in Container
        Service 3.7.2: ``GET /xapi/wrappers`` is 404, and ``GET
        /xapi/commands/{id}/wrappers`` is 405 Method Not Allowed (both
        verified live). Every wrapper definition is embedded under the
        ``xnat`` key of its owning command's object (``GET
        /xapi/commands``), so this derives the flat wrapper list by walking
        every command's ``xnat`` array. Do not "optimise" this back into a
        call against either of those two endpoints -- they do not exist in
        this plugin version.

        Args:
            command_id: When given, return only this command's wrappers.

        Returns:
            List of wrapper dicts. Verified embedded key set: ``contexts``,
            ``derived-inputs``, ``description``, ``external-inputs``,
            ``id``, ``label``, ``name``, ``output-handlers`` -- plus a
            synthesized ``command-id`` key so callers can tell which
            command a wrapper belongs to. There is no ``enabled`` key on a
            wrapper in this plugin version; nothing here fabricates one.

        Raises:
            ResourceNotFoundError: If ``command_id`` is given and no such
                command exists.
        """
        commands = self.list_commands()

        if command_id is not None:
            for command in commands:
                if command.get("id") == command_id:
                    return self._wrappers_of(command)
            raise ResourceNotFoundError("command", str(command_id))

        wrappers: list[dict[str, Any]] = []
        for command in commands:
            wrappers.extend(self._wrappers_of(command))
        return wrappers

    @staticmethod
    def _wrappers_of(command: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract one command's embedded wrapper rows, tagged with its ``command-id``.

        Thin wrapper over :func:`wrappers_of`, kept as a method so the
        service's own call sites read naturally.

        Raises:
            XNATCtlError: If ``xnat`` is not a shape this client understands.
        """
        return wrappers_of(command)

    def _check_project_exists(self, project: str) -> None:
        """Raise ``ResourceNotFoundError`` if ``project`` does not exist.

        The project-scoped enable/disable routes answer 200 for a
        nonexistent project (verified live -- see :meth:`enable_wrapper`),
        so this is the only thing standing between a typo'd ``-P`` and a
        confident "success" message that changed nothing.
        """
        ProjectService(self.client).get(project)

    def get_wrapper(self, command_id: int, wrapper_id: int) -> dict[str, Any]:
        """Get one wrapper's full definition, derived client-side.

        There is no single-wrapper GET endpoint in Container Service 3.7.2
        (``GET /xapi/commands/{id}/wrappers/{wrapperId}`` is 405 Method Not
        Allowed, verified live) -- this filters :meth:`list_wrappers`.

        Args:
            command_id: Numeric command ID the wrapper belongs to.
            wrapper_id: Numeric wrapper ID.

        Returns:
            Wrapper dict -- see :meth:`list_wrappers` for the key set.

        Raises:
            ResourceNotFoundError: If the command or wrapper does not exist.
        """
        for wrapper in self.list_wrappers(command_id=command_id):
            if wrapper.get("id") == wrapper_id:
                return wrapper
        raise ResourceNotFoundError("wrapper", str(wrapper_id))

    def get_wrapper_config(
        self,
        wrapper_id: int,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get a wrapper's site- or project-scoped configuration.

        Three equivalent forms exist for the site-scoped read, all verified
        live: ``GET /xapi/wrappers/{wrapperId}/config``, ``GET
        /xapi/commands/{cid}/wrappers/{wrapperName}/config``, and ``GET
        /xapi/commands/{cid}/wrappers/{wrapperId}/config``. This uses the
        first (wrapper-id-only) form, since a numeric wrapper ID already
        identifies it uniquely on that route and it needs no command ID.
        The project-scoped form is ``GET
        /xapi/projects/{project}/wrappers/{wrapperId}/config`` (also
        verified live).

        Args:
            wrapper_id: Numeric wrapper ID (see :meth:`resolve_wrapper`).
            project: When given, read the project-scoped configuration
                instead of the site-scoped default.

        Returns:
            Configuration dict. Verified key set: ``inputs`` (keyed by
            input name, each an object with ``description``, ``type``,
            ``default-value``, ``matcher``, ``user-settable``,
            ``advanced``, ``required`` -- present as explicit nulls when
            unset, not absent) and ``outputs``.

        Raises:
            ResourceNotFoundError: If no configuration exists for this
                wrapper (site-scoped) or project/wrapper pair. Before the
                command exists, these paths answer 403, not 404 -- that
                surfaces as ``PermissionDeniedError``, not this.
        """
        if project is not None:
            path = (
                f"/xapi/projects/{quote_path_segment(project)}"
                f"/wrappers/{quote_path_segment(str(wrapper_id))}/config"
            )
        else:
            path = f"/xapi/wrappers/{quote_path_segment(str(wrapper_id))}/config"
        try:
            data = self.client.get_json(path)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("wrapper config", str(wrapper_id)) from e
        return _expect_dict(data, path)

    def resolve_wrapper(self, ref: str) -> tuple[int, dict[str, Any]]:
        """Resolve a wrapper by numeric ID or name.

        Wrapper names are unique only within a command -- two different
        commands can expose a wrapper with the same name -- so a name
        lookup matched by wrappers from more than one command is an error,
        not a first-match pick: which one runs matters too much to guess.

        Args:
            ref: A numeric wrapper ID (as a string) or a wrapper name.

        Returns:
            Tuple of ``(wrapper_id, wrapper_dict)`` -- the dict is one of
            :meth:`list_wrappers`'s rows, carrying a ``command-id`` key.

        Raises:
            ResourceNotFoundError: If no wrapper matches ``ref``.
            InputValidationError: If ``ref`` is a name matched by wrappers
                from more than one command.
        """
        wrappers = self.list_wrappers()

        if ref.isdigit():
            wrapper_id = int(ref)
            for wrapper in wrappers:
                if wrapper.get("id") == wrapper_id:
                    return wrapper_id, wrapper
            raise ResourceNotFoundError("wrapper", ref)

        matches = [w for w in wrappers if w.get("name") == ref]
        if not matches:
            raise ResourceNotFoundError("wrapper", ref)
        if len(matches) > 1:
            candidates = ", ".join(
                f"command {m.get('command-id')} wrapper {m.get('id')}" for m in matches
            )
            raise InputValidationError(
                f"Wrapper name {ref!r} is ambiguous: matched by {len(matches)} wrappers "
                f"({candidates}). Use a numeric wrapper ID instead.",
                field="wrapper",
                value=ref,
            )
        wrapper = matches[0]
        resolved_id = wrapper.get("id")
        if not isinstance(resolved_id, int):
            raise ResourceNotFoundError("wrapper", ref)
        return resolved_id, wrapper

    def create_command(self, payload: dict[str, Any]) -> int:
        """Register a new command.

        Verified live: ``POST /xapi/commands`` with a single command.json
        OBJECT answers 201 with the new numeric ID as a BARE INTEGER body
        (not a JSON object) -- ``resp.json()`` still parses it correctly
        since the body is valid JSON, just not an object. This reads it
        directly rather than going through ``client.get_json``, which
        always adds ``format=json`` -- a GET-only convention that does not
        apply here.

        Args:
            payload: The command definition to register. Server-validated:
                a malformed payload (e.g. a blank name/image) answers 400
                with a human-readable message, surfaced as
                ``ClientRequestError``.

        Returns:
            The new command's numeric ID.

        Raises:
            XNATCtlError: If the 2xx response body is not an integer.
        """
        path = "/xapi/commands"
        resp = self.client.post(path, json=payload)
        data = resp.json()
        if not isinstance(data, int) or isinstance(data, bool):
            raise XNATCtlError(
                f"Unexpected response from POST {path}: expected a bare integer ID, "
                f"got {type(data).__name__}."
            )
        return data

    def prepare_update_body(
        self, command_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build the exact request body :meth:`update_command` will POST, without sending it.

        :meth:`update_command` calls this and POSTs the returned body
        unmodified. ``command update --dry-run`` calls it too, so the diff
        it previews is built from the identical transformation execution
        applies -- see :func:`_drop_stale_wrapper_ids` for why the stale-id
        stripping can't be skipped: a preview that diffed the raw ``payload``
        instead would show a modified wrapper keeping an id that execution
        actually strips, silently invalidating anything scoped to that id
        (``wrapper enable``, ``wrapper config set``).

        Args:
            command_id: Numeric ID of the command to update.
            payload: The full replacement command definition, as given by
                the caller.

        Returns:
            Tuple of ``(current, body)`` -- ``current`` is the command's
            present definition (:meth:`get_command`), for diffing against;
            ``body`` is ``payload`` with stale wrapper ids stripped, exactly
            as it will be POSTed.

        Raises:
            ResourceNotFoundError: If the command does not exist.
        """
        current = self.get_command(command_id)
        body = _drop_stale_wrapper_ids(current, payload)
        return current, body

    def update_command(self, command_id: int, payload: dict[str, Any]) -> None:
        """Replace a command's full definition.

        Verified live: there is no ``PUT`` on ``/xapi/commands/{id}`` (405
        Method Not Allowed) -- update is ``POST /xapi/commands/{id}`` with a
        full command.json OBJECT, answering 200 with an empty body.

        *** THIS IS A FULL REPLACE, NOT A MERGE. *** Omitting ``xnat`` from
        ``payload`` wipes every wrapper registered on the command. Callers
        that want to preserve existing wrappers must include the command's
        current ``xnat`` array (from :meth:`get_command`) in ``payload``.

        An embedded wrapper entry that keeps its current numeric ``id`` but
        changes anything else about that wrapper (e.g. renaming it) answers
        500 -- verified live: a Hibernate identity conflict ("A different
        object with the same identifier value was already associated with
        the session : [...CommandWrapperEntity#<id>]"). Re-POSTing the exact
        same ``xnat`` array unchanged, id included, succeeds and keeps that
        id stable; only a MODIFIED wrapper sharing its old id triggers the
        conflict. So :meth:`prepare_update_body` compares each incoming
        wrapper against the command's currently-registered ones and strips
        ``id`` from any that differ or are new -- verified live that
        dropping ``id`` lets the server mint a fresh one and apply the
        change without error. A wrapper submitted byte-for-byte unchanged
        keeps its existing id.

        A nonexistent ``command_id`` does NOT answer 404 here -- verified
        live, it answers 500 with a raw Hibernate ``StaleStateException``
        body ("Batch update returned unexpected row count..."). This checks
        existence first via :meth:`prepare_update_body` (which calls
        :meth:`get_command`, and that does answer 404 cleanly) and lets that
        raise instead of exposing the 500.

        Args:
            command_id: Numeric ID of the command to update.
            payload: The full replacement command definition.

        Raises:
            ResourceNotFoundError: If the command does not exist.
        """
        _current, body = self.prepare_update_body(command_id, payload)
        path = f"/xapi/commands/{quote_path_segment(str(command_id))}"
        self.client.post(path, json=body)

    def delete_command(self, command_id: int) -> None:
        """Delete a command.

        Verified live: ``DELETE /xapi/commands/{id}`` answers 204 even for a
        nonexistent ID -- delete is idempotent-succeeds, not
        idempotent-404s, so a bad ID would otherwise vanish as a silent
        no-op. This checks existence first via :meth:`get_command` so
        deleting an unknown command still raises ``ResourceNotFoundError``.

        Args:
            command_id: Numeric ID of the command to delete.

        Raises:
            ResourceNotFoundError: If the command does not exist.
        """
        self.get_command(command_id)
        path = f"/xapi/commands/{quote_path_segment(str(command_id))}"
        self.client.delete(path)

    def check_wrapper_scope(
        self, command_id: int, wrapper_id: int, *, project: str | None = None
    ) -> None:
        """Validate an enable/disable request without changing any state.

        Runs exactly the preflight :meth:`enable_wrapper`/:meth:`disable_wrapper`
        run before their ``PUT`` -- confirms the ``(command_id, wrapper_id)``
        pair exists via :meth:`get_wrapper` and, when scoped to a project,
        that the project exists via :meth:`_check_project_exists`. Both
        methods call this rather than duplicating the two checks, and it is
        also the public entry point ``wrapper enable/disable --dry-run``
        calls in :mod:`xnatctl.cli.wrapper` -- a dry run that skipped the
        project check would report success for a project that does not
        exist, which real execution refuses (see the project-scoped case
        below).

        Args:
            command_id: Numeric ID of the command the wrapper belongs to.
            wrapper_id: Numeric ID of the wrapper.
            project: When given, also check this project exists.

        Raises:
            ResourceNotFoundError: If the command, wrapper, or project does
                not exist.
        """
        self.get_wrapper(command_id, wrapper_id)
        if project is not None:
            self._check_project_exists(project)

    def enable_wrapper(
        self, command_id: int, wrapper_id: int, *, project: str | None = None
    ) -> None:
        """Enable a wrapper, site- or project-scoped.

        Verified live: ``PUT /xapi/commands/{cid}/wrappers/{wid}/enabled``
        (site) and ``PUT /xapi/projects/{project}/wrappers/{wid}/enabled``
        (project) both answer 200 with an empty body. Neither the
        project-scoped route (its path carries no command ID at all) nor
        the site-scoped one validates that ``wrapper_id`` actually belongs
        to ``command_id`` beyond "some wrapper with this ID exists on this
        command" for the site form -- and enabling a wrapper for a project
        that does not exist SILENTLY SUCCEEDS (verified live, 200), with no
        error at all. :meth:`check_wrapper_scope` checks the
        ``(command_id, wrapper_id)`` pair exists first, so an unknown pair
        still raises ``ResourceNotFoundError`` rather than a confusing 404
        (site form) or a silent no-op that looks like success (project
        form, nonexistent project).

        Args:
            command_id: Numeric ID of the command the wrapper belongs to.
            wrapper_id: Numeric ID of the wrapper to enable.
            project: When given, enable for this project instead of
                site-wide. Checked to exist first -- see above.

        Raises:
            ResourceNotFoundError: If the command, wrapper, or project does
                not exist.
        """
        self.check_wrapper_scope(command_id, wrapper_id, project=project)
        wid = quote_path_segment(str(wrapper_id))
        if project is not None:
            path = f"/xapi/projects/{quote_path_segment(project)}/wrappers/{wid}/enabled"
        else:
            path = f"/xapi/commands/{quote_path_segment(str(command_id))}/wrappers/{wid}/enabled"
        self.client.put(path)

    def disable_wrapper(
        self, command_id: int, wrapper_id: int, *, project: str | None = None
    ) -> None:
        """Disable a wrapper, site- or project-scoped. See :meth:`enable_wrapper`.

        Verified live: ``PUT /xapi/commands/{cid}/wrappers/{wid}/disabled``
        and ``PUT /xapi/projects/{project}/wrappers/{wid}/disabled`` -- a
        separate route from ``enabled``, not the same route with a query
        parameter (``PUT .../enabled?enable=false`` was tried live and does
        NOT disable anything; ``enabled`` always enables).

        Raises:
            ResourceNotFoundError: If the command, wrapper, or project does
                not exist.
        """
        self.check_wrapper_scope(command_id, wrapper_id, project=project)
        wid = quote_path_segment(str(wrapper_id))
        if project is not None:
            path = f"/xapi/projects/{quote_path_segment(project)}/wrappers/{wid}/disabled"
        else:
            path = f"/xapi/commands/{quote_path_segment(str(command_id))}/wrappers/{wid}/disabled"
        self.client.put(path)

    def wrapper_enabled_state(
        self, command_id: int, wrapper_id: int, *, project: str | None = None
    ) -> bool:
        """Read whether a wrapper is currently enabled, at the given scope.

        Verified live: the site-scoped route (``GET
        /xapi/commands/{cid}/wrappers/{wid}/enabled``) answers a bare
        boolean; the project-scoped one (``GET
        /xapi/projects/{project}/wrappers/{wid}/enabled``) answers an object
        (``{"enabled-for-site": ..., "enabled-for-project": ...,
        "project": ...}``), and this reads ``enabled-for-project`` from it
        -- the project's own enablement, not the inherited site one.

        Public (not name-mangled) because it is the exact preflight
        :meth:`set_wrapper_config` runs before its POST, and ``wrapper
        config set --dry-run`` in :mod:`xnatctl.cli.wrapper` calls it too --
        see :meth:`check_wrapper_config_scope`, the shared entry point both
        use, so preview and execution read identical state.

        Args:
            command_id: Numeric ID of the command the wrapper belongs to.
            wrapper_id: Numeric ID of the wrapper.
            project: When given, read the project-scoped state instead of
                the site-wide one.

        Returns:
            Whether the wrapper is currently enabled at the requested scope.
        """
        wid = quote_path_segment(str(wrapper_id))
        if project is not None:
            path = f"/xapi/projects/{quote_path_segment(project)}/wrappers/{wid}/enabled"
            data = _expect_dict(self.client.get_json(path), path)
            return bool(data.get("enabled-for-project", False))
        path = f"/xapi/commands/{quote_path_segment(str(command_id))}/wrappers/{wid}/enabled"
        return bool(self.client.get_json(path))

    def check_wrapper_config_scope(
        self, command_id: int, wrapper_id: int, *, project: str | None = None
    ) -> bool:
        """Validate a config-set request and return the enabled state it must carry forward.

        Runs the exact preflight :meth:`set_wrapper_config` runs before its
        POST: confirms the ``(command_id, wrapper_id)`` pair exists via
        :meth:`get_wrapper`, then reads the wrapper's current enabled state
        at the requested scope via :meth:`wrapper_enabled_state`. This is
        also the entry point ``wrapper config set --dry-run`` calls in
        :mod:`xnatctl.cli.wrapper`, so a preview and the real write always
        agree on what state gets preserved -- see
        :meth:`set_wrapper_config`'s docstring for the re-enable bug this
        exists to prevent.

        Args:
            command_id: Numeric ID of the command the wrapper belongs to.
            wrapper_id: Numeric ID of the wrapper.
            project: When given, check the project-scoped state instead of
                the site-wide one.

        Returns:
            Whether the wrapper is currently enabled at the requested scope
            -- exactly the value a config write must send back as ``enable``
            to leave enablement unchanged.

        Raises:
            ResourceNotFoundError: If the command or wrapper does not exist.
        """
        self.get_wrapper(command_id, wrapper_id)
        return self.wrapper_enabled_state(command_id, wrapper_id, project=project)

    def set_wrapper_config(
        self,
        command_id: int,
        wrapper_id: int,
        payload: dict[str, Any],
        *,
        project: str | None = None,
    ) -> None:
        """Set a wrapper's site- or project-scoped configuration.

        Verified live: ``POST /xapi/wrappers/{wrapperId}/config`` (site) and
        ``POST /xapi/projects/{project}/wrappers/{wrapperId}/config``
        (project) both accept a ``CommandConfiguration`` object (``inputs``/
        ``outputs``, matching :meth:`get_wrapper_config`'s shape) and answer
        201 with an empty body. Neither route validates the wrapper ID --
        POSTing to a nonexistent wrapper ID answers 201 just the same
        (verified live). :meth:`check_wrapper_config_scope` checks the
        ``(command_id, wrapper_id)`` pair exists first, so an unknown pair
        raises ``ResourceNotFoundError`` instead of silently writing an
        orphaned configuration row.

        The route's ``enable`` query parameter DOES default to ``true`` when
        omitted. Reproduced live on 2026-08-25 against XNAT 1.9.2.1 +
        Container Service 3.7.2, using this service's own routes, AT BOTH
        SCOPES:

            PUT  /xapi/commands/1/wrappers/171/enabled          -> 200
            GET  /xapi/commands/1/wrappers/171/enabled          -> true
            PUT  /xapi/commands/1/wrappers/171/disabled         -> 200
            GET  /xapi/commands/1/wrappers/171/enabled          -> false
            POST /xapi/wrappers/171/config  (no ``enable`` param)  -> 201
            GET  /xapi/commands/1/wrappers/171/enabled          -> true   # RE-ENABLED

            PUT  /xapi/projects/P/wrappers/171/enabled          -> 200
            GET  /xapi/projects/P/wrappers/171/enabled -> {"enabled-for-project": true, ...}
            PUT  /xapi/projects/P/wrappers/171/disabled         -> 200
            GET  /xapi/projects/P/wrappers/171/enabled -> {"enabled-for-project": false, ...}
            POST /xapi/projects/P/wrappers/171/config  (no ``enable`` param) -> 201
            GET  /xapi/projects/P/wrappers/171/enabled -> {"enabled-for-project": true, ...}  # RE-ENABLED

        So configuring a wrapper that was just deliberately disabled
        silently re-enables it, at both scopes, with nothing in the request
        or response saying so. :meth:`check_wrapper_config_scope` reads the
        wrapper's current enabled state first and this method passes it back
        explicitly as ``enable`` on the POST, so setting configuration never
        changes enablement as a side effect -- confirmed by the same live
        sequence that an explicit ``enable=false`` on the POST holds a
        disabled wrapper disabled through the write.

        Args:
            command_id: Numeric ID of the command the wrapper belongs to.
            wrapper_id: Numeric ID of the wrapper to configure.
            payload: The full replacement configuration (``inputs``/
                ``outputs``).
            project: When given, set the project-scoped configuration
                instead of the site-wide default.

        Raises:
            ResourceNotFoundError: If the command or wrapper does not exist.
        """
        enabled = self.check_wrapper_config_scope(command_id, wrapper_id, project=project)
        wid = quote_path_segment(str(wrapper_id))
        if project is not None:
            path = f"/xapi/projects/{quote_path_segment(project)}/wrappers/{wid}/config"
        else:
            path = f"/xapi/wrappers/{wid}/config"
        self.client.post(path, params={"enable": "true" if enabled else "false"}, json=payload)
