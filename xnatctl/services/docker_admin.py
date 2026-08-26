"""DockerAdminService: XNAT Container Service docker daemon administration.

Named to avoid clashing with any ``core``/``docker``-flavoured module. Covers
``admin docker images/hubs/server/pull``, including setting the server
connection (:meth:`DockerAdminService.set_server`).

Every endpoint this module calls was verified live against XNAT 1.9.2.1 +
Container Service 3.7.2. The integration stack that verified them has no
Docker daemon reachable from the XNAT container by design, which is exactly
the state ``images()``, ``get_server()``, ``pull_image()``, and
``set_server()`` guard against below: XNAT answers those with a 500 whose
body is a raw Java exception string (``java.io.IOException:
com.sun.jna.LastErrorException: ...``), and this is the single most likely
real-world cause of that response, so it is rendered as an actionable
message instead of the raw text or an empty table. The rewrite is narrowly
matched on both the status code (exactly 500) and that body signature (see
``_looks_like_daemon_unreachable``) -- a 502/503/504 from a proxy, or an
unrelated plugin-side 500, is a different failure and must propagate
unchanged rather than being mislabeled as a Docker connectivity problem.

GET is idempotent, so ``core/retry.py``'s ladder retries a 500 a few times
before giving up -- confirmed live: a persistent no-daemon 500 surfaces to
this module as ``RetryExhaustedError`` wrapping the final ``ServerError``,
not a bare ``ServerError``. POST (``pull_image``, ``set_server``) is not
idempotent, so a 500 there is not retried and always surfaces as a bare
``ServerError`` -- both shapes are handled below, and the rendered message
now names the real HTTP method rather than assuming GET.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from xnatctl.core.exceptions import RetryExhaustedError, ServerError, XNATCtlError

from .base import BaseService

_DAEMON_UNREACHABLE_HINT = (
    "Ask an XNAT admin to check the Docker host/socket configured for Container "
    "Service and confirm the daemon is running and reachable from the XNAT server."
)

# Substrings of the real observed body of a "no Docker daemon reachable" 500
# (see the module docstring): the Docker Java client's low-level failure to
# reach the socket/pipe. Both must be present -- either one alone is common
# enough in an arbitrary Java stack trace that it is not, by itself, a
# reliable signature of this specific failure.
_DAEMON_UNREACHABLE_BODY_SIGNATURES = ("java.io.IOException", "LastErrorException")


def _looks_like_daemon_unreachable(exc: ServerError) -> bool:
    """Whether a ServerError's status and body match the observed no-daemon shape.

    Narrowly matched on purpose: a 502/503/504 from a proxy, or an unrelated
    500 from the plugin itself, must propagate unchanged rather than being
    rewritten into a Docker-specific message that has nothing to do with the
    real failure.
    """
    return exc.status_code == 500 and all(
        sig in exc.body for sig in _DAEMON_UNREACHABLE_BODY_SIGNATURES
    )


def _as_daemon_unreachable_error(path: str, exc: Exception) -> XNATCtlError | None:
    """Recognize a "no Docker daemon reachable" failure, direct or retry-exhausted.

    A single 500 surfaces as :class:`ServerError`; a persistent one survives
    the retry ladder (GET is idempotent, so 500 is retried) and surfaces as
    :class:`RetryExhaustedError` wrapping that same ``ServerError`` in
    ``last_error`` -- verified live. Only a ``ServerError`` whose status is
    exactly 500 AND whose body matches the observed daemon-unreachable
    signature is rewritten; everything else (a different 5xx, or a 500 with
    an unrelated body) returns ``None`` so the caller re-raises it unchanged.
    """
    server_error = exc if isinstance(exc, ServerError) else None
    if (
        server_error is None
        and isinstance(exc, RetryExhaustedError)
        and isinstance(exc.last_error, ServerError)
    ):
        server_error = exc.last_error
    if server_error is None or not _looks_like_daemon_unreachable(server_error):
        return None
    return XNATCtlError(
        f"Docker daemon is unreachable from the XNAT server "
        f"({server_error.method} {path} returned HTTP "
        f"{server_error.details.get('status_code', '5xx')}).",
        details=server_error.details,
        hint=_DAEMON_UNREACHABLE_HINT,
    )


def _expect_list(data: Any, path: str) -> list[dict[str, Any]]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``[]``. See ``commands.py``."""
    if not isinstance(data, list):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON array, "
            f"got {type(data).__name__}."
        )
    return cast(list[dict[str, Any]], data)


def _expect_dict(data: Any, path: str) -> dict[str, Any]:
    """Raise instead of silently coercing an unexpected 2xx shape to ``{}``. See ``commands.py``."""
    if not isinstance(data, dict):
        raise XNATCtlError(
            f"Unexpected response from GET {path}: expected a JSON object, "
            f"got {type(data).__name__}."
        )
    return cast(dict[str, Any], data)


class DockerAdminService(BaseService):
    """Service for XNAT Container Service docker daemon administration.

    Every method returns plain ``dict``/``list[dict]`` -- see
    :class:`~xnatctl.services.commands.CommandService`'s docstring for why
    (plugin-version-dependent shapes, no library-consumer need distinct from
    the CLI's own rendering).
    """

    def images(self) -> list[dict[str, Any]]:
        """List docker images known to the configured daemon.

        Returns:
            List of image dicts. The route is verified live (``GET
            /xapi/docker/images``); the body could not be captured (no
            daemon in the integration stack), but its field names are
            verified from the plugin's own ``@JsonProperty`` declarations
            on ``DockerImage``: ``image-id``, ``tags``, ``labels``,
            ``description``.

        Raises:
            XNATCtlError: If no Docker daemon is reachable from the XNAT
                server -- the observed 500 response for this state, with an
                actionable message in place of its raw Java exception body.
        """
        path = "/xapi/docker/images"
        try:
            data = self.client.get_json(path)
        except (ServerError, RetryExhaustedError) as e:
            daemon_error = _as_daemon_unreachable_error(path, e)
            if daemon_error is None:
                raise
            raise daemon_error from e
        return _expect_list(data, path)

    def hubs(self) -> list[dict[str, Any]]:
        """List configured docker hubs.

        Returns:
            List of hub dicts. Verified live (``GET /xapi/docker/hubs``).
            Real observed key set: ``id``, ``name``, ``url``, ``username``
            (nullable), ``email`` (nullable), ``status`` (an object with
            ``ping``, ``response``, ``message``), ``default``. Works even
            with no daemon reachable -- this list is XNAT-side configuration,
            not a daemon query.
        """
        path = "/xapi/docker/hubs"
        data = self.client.get_json(path)
        return _expect_list(data, path)

    def get_server(self) -> dict[str, Any]:
        """Get the configured docker daemon connection.

        Returns:
            Server config dict. The route is verified live (``GET
            /xapi/docker/server``); the field names are verified from the
            plugin's own ``@JsonProperty`` declarations on
            ``DockerServer``/``DockerServerWithPing``: ``name``, ``host``,
            ``backend``, ``swarm-mode``, ``ping`` (``WithPing`` only --
            worth surfacing since an unreachable daemon is the common real-
            world state this command has to convey), ``auto-cleanup``,
            ``container-user``, ``gpu-vendor``,
            ``pull-images-on-xnat-init``, ``status-email-enabled``,
            ``max-concurrent-finalizing-jobs``,
            ``path-translation-xnat-prefix``,
            ``path-translation-docker-prefix``, ``archive-path-translation``,
            ``build-path-translation``, ``combined-path-translation``,
            ``archive-pvc-name``, ``build-pvc-name``, ``combined-pvc-name``,
            ``swarm-constraints``.

        Raises:
            XNATCtlError: If no Docker daemon is reachable from the XNAT
                server -- see :meth:`images`.
        """
        path = "/xapi/docker/server"
        try:
            data = self.client.get_json(path)
        except (ServerError, RetryExhaustedError) as e:
            daemon_error = _as_daemon_unreachable_error(path, e)
            if daemon_error is None:
                raise
            raise daemon_error from e
        return _expect_dict(data, path)

    def pull_image(self, image: str, *, save_commands: bool = True) -> httpx.Response:
        """Pull a docker image from the default hub.

        Verified live: ``POST /xapi/docker/pull?image=...&save-commands=...``
        -- confirmed query parameter names (the ground-truth notes' guess
        was right). ``image`` is required; a request without it answers 400
        ("Parameter conditions 'image' not met"). With no reachable daemon,
        it answers the same 500 signature as :meth:`images`/:meth:`get_server`.

        The success response body/shape could not be observed (no daemon in
        the integration stack this was verified against), so this returns
        the raw ``httpx.Response`` rather than guessing a shape to parse --
        a caller that needs the body can read it directly.

        Args:
            image: Image reference to pull (e.g. ``"xnat/dcm2niix:v1.2"``).
            save_commands: Whether to also register any commands embedded in
                the image's label metadata. Server default is ``True`` when
                the parameter is omitted; passed explicitly either way.

        Returns:
            The raw response.

        Raises:
            XNATCtlError: If no Docker daemon is reachable from the XNAT
                server -- see :meth:`images`.
        """
        path = "/xapi/docker/pull"
        params = {"image": image, "save-commands": "true" if save_commands else "false"}
        try:
            return self.client.post(path, params=params)
        except (ServerError, RetryExhaustedError) as e:
            daemon_error = _as_daemon_unreachable_error(path, e)
            if daemon_error is None:
                raise
            raise daemon_error from e

    def build_set_server_body(self, host: str) -> dict[str, Any]:
        """Build the body :meth:`set_server` would POST, without posting it.

        Split out of :meth:`set_server` so a dry-run preview can run the
        exact same read-and-merge preflight -- and hit the exact same
        failure modes (an unreadable or malformed current configuration) --
        that execution does, instead of skipping straight past it. See
        :meth:`set_server` for why the read happens at all and what "fail
        open" means for the no-daemon case.

        Args:
            host: The new Docker daemon host/socket URL.

        Returns:
            The full request body :meth:`set_server` would send.

        Raises:
            XNATCtlError: If the current configuration could not be read
                for a reason other than an unreachable daemon -- see
                :meth:`set_server`.
        """
        path = "/xapi/docker/server"
        try:
            current = _expect_dict(self.client.get_json(path), path)
        except (ServerError, RetryExhaustedError) as e:
            daemon_error = _as_daemon_unreachable_error(path, e)
            if daemon_error is None:
                raise
            # No daemon reachable means no existing configuration to lose --
            # safe to proceed with a fresh host-only body.
            current = {}

        body = {k: v for k, v in current.items() if k != "ping"}
        body["host"] = host
        return body

    def set_server(self, host: str) -> dict[str, Any]:
        """Set the docker daemon host.

        ``POST /xapi/docker/server`` takes a full ``DockerServer`` request
        body (per the plugin's own swagger definition) -- the same shape
        :meth:`get_server` returns, minus ``ping`` (``DockerServerWithPing``
        -only, response-side). Whether a partial body is merged server-side
        or replaces every omitted field could NOT be confirmed live: this
        integration stack has no reachable Docker daemon, and both GET and
        POST to this route answer the identical daemon-unreachable 500
        (verified live, same signature as :meth:`images`) before any body
        validation happens. Given the sibling ``POST /xapi/commands/{id}``
        route IS a verified full replace (see
        ``CommandService.update_command``), this method treats
        ``/xapi/docker/server`` the same way rather than risk silently
        resetting every other field to a server default: it reads the
        current configuration first and merges ``host`` into it before
        posting (see :meth:`build_set_server_body`).

        The read is allowed to fail open in exactly one case: no Docker
        daemon reachable (the observed 500 signature -- see
        :meth:`images`), where there is by definition no existing
        configuration to lose, so this falls back to sending ``{"host":
        host}`` alone. Any other read failure -- a permission error, an
        unrelated server error, or an unexpected 2xx shape -- is NOT
        assumed safe to proceed past: this module has no confirmation that
        the write is a partial merge rather than a full replace, so
        guessing wrong there would silently reset every other Docker
        setting. Those failures propagate unchanged instead, aborting
        before any POST is made.

        Args:
            host: The new Docker daemon host/socket URL.

        Returns:
            The response body (a ``DockerServerWithPing`` dict on a real
            daemon, per the swagger schema -- unverified live, so returned
            as-is rather than validated against an assumed field set beyond
            "is a JSON object").

        Raises:
            XNATCtlError: If no Docker daemon is reachable from the XNAT
                server on the POST -- see :meth:`images`. Also raised (or a
                narrower ``XNATCtlError`` subclass, e.g.
                ``PermissionDeniedError``) if the current configuration
                could not be read for a reason other than an unreachable
                daemon -- see above.
        """
        path = "/xapi/docker/server"
        body = self.build_set_server_body(host)

        try:
            resp = self.client.post(path, json=body)
        except (ServerError, RetryExhaustedError) as e:
            daemon_error = _as_daemon_unreachable_error(path, e)
            if daemon_error is None:
                raise
            raise daemon_error from e
        return _expect_dict(resp.json(), path)
