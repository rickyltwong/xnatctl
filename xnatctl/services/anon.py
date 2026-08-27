"""Anonymize Service: XNAT site- and project-scoped DicomEdit anonymization scripts.

Every endpoint here was verified live against XNAT 1.9.2.1, including the
mutating verbs (set script, enable/disable). A few shapes are surprising
enough to call out loudly:

- ``GET /xapi/anonymize/projects/{project}`` answers 204 (empty body) when
  the project has no override script -- it simply inherits the site script.
  This is NOT an error and NOT the same as the project not existing.
- ``GET``/``PUT`` for a project that does not exist answers a raw 500
  ("There was an error in the request : null") rather than a clean 404, on
  both the script route and the ``/enabled`` route -- every project-scoped
  method here checks the project exists first via ``ProjectService.get``
  so a typo'd ``-P`` raises ``ResourceNotFoundError`` instead.
- ``PUT .../enabled`` requires a JSON-typed request body (its CONTENT is
  never read -- ``"null"`` works as well as ``"true"``) and the actual value
  is read from the ``enable`` QUERY PARAMETER instead, which defaults to
  ``true`` when omitted. A body of ``{"enabled": false}``/``false`` with no
  ``?enable=false`` query parameter is silently ignored and always leaves
  the row enabled -- reproduced live, repeatedly, against both the site and
  a project scope.
- The project-scoped ``/enabled`` route 500s ("Couldn't find the site
  configuration for tool anon and path ...") if no project script has ever
  been PUT for that project -- :meth:`AnonymizeService.set_project_enabled`
  preflights that via :meth:`AnonymizeService.check_project_enable_scope`
  and raises an actionable error instead.
- ``GET /xapi/anonymize/projects/{project}`` answering 204 does NOT
  distinguish "no script was ever set" from "a script is set but currently
  disabled" -- both read as 204, verified live by disabling a
  freshly-PUT project script and re-reading it. :meth:`project_has_script`
  uses a different, older route (``GET
  /data/projects/{project}/config/anon``, the generic config-service
  history endpoint) that returns every version including disabled ones, to
  tell those two states apart -- this is what
  :meth:`check_project_enable_scope` actually checks, not
  :meth:`get_project_script`, so re-enabling a script you just disabled
  does not get rejected as "no script set".
"""

from __future__ import annotations

from xnatctl.core.exceptions import ResourceNotFoundError, XNATCtlError
from xnatctl.core.validation import quote_path_segment

from .base import BaseService
from .projects import ProjectService

# `enable=`/`disable=` need a JSON-typed body to be accepted at all (a
# request with no Content-Type, or any non-JSON one, answers 415) but the
# body's CONTENT is never read -- the real value travels in the `enable`
# query parameter instead. See the module docstring.
_ENABLE_HEADERS = {"Content-Type": "application/json"}
_ENABLE_BODY = b"null"


class AnonymizeService(BaseService):
    """Service for XNAT site- and project-scoped anonymization scripts.

    Every method returns a plain ``str``/``bool``/``None`` -- an
    anonymization script is DicomEdit text, not JSON, so there is no
    Pydantic model to return here (per the data-flow rule in ``AGENTS.md``).
    """

    def _check_project_exists(self, project: str) -> None:
        """Raise ``ResourceNotFoundError`` if ``project`` does not exist.

        The anonymize project routes answer a raw 500 for an unknown
        project (verified live -- see the module docstring), so this is the
        only thing standing between a typo'd ``-P`` and a confusing error.
        """
        ProjectService(self.client).get(project)

    def get_default_script(self) -> str:
        """Return XNAT's built-in default anonymization script (read-only).

        Verified live: ``GET /xapi/anonymize/default`` -> 200 text/plain.
        There is no ``PUT`` on this route (``Allow: GET,HEAD,OPTIONS``,
        verified live) -- it is the template XNAT ships with, not a
        configurable value.
        """
        return self.client.get("/xapi/anonymize/default").text

    def get_site_script(self) -> str:
        """Return the site-wide anonymization script.

        Verified live: ``GET /xapi/anonymize/site`` -> 200 text/plain.
        """
        return self.client.get("/xapi/anonymize/site").text

    def set_site_script(self, script: str) -> None:
        """Replace the site-wide anonymization script.

        Verified live: ``PUT /xapi/anonymize/site`` with a ``text/plain``
        body -> 200, empty response.

        *** THE MATCHING GET IS EVENTUALLY CONSISTENT. *** Measured live
        against XNAT 1.9.2.1: a ``GET /xapi/anonymize/site`` issued
        immediately after this write returns the PREVIOUS script, and the
        new one becomes visible about a second later. Two writes in quick
        succession make that unmistakable -- the second read still serves
        the value from before the first write.

        So a caller must not treat an immediate read-back as confirmation:
        reading straight after writing will normally show the old script
        and look like the write silently failed. Poll until the expected
        content appears (the integration tier does exactly this) rather
        than reading once. Nothing here retries on the caller's behalf,
        because how long to wait is a policy this service should not pick.

        Args:
            script: The full new DicomEdit script text.
        """
        self.client.put(
            "/xapi/anonymize/site",
            content=script.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )

    def site_enabled(self) -> bool:
        """Whether the site-wide anonymization script is active.

        Verified live: ``GET /xapi/anonymize/site/enabled`` -> a bare
        JSON boolean.
        """
        return bool(self.client.get_json("/xapi/anonymize/site/enabled"))

    def set_site_enabled(self, enable: bool) -> None:
        """Enable or disable the site-wide anonymization script.

        Args:
            enable: Whether the site script should be active.
        """
        self._put_enabled("/xapi/anonymize/site/enabled", enable)

    def get_project_script(self, project: str) -> str | None:
        """Return a project's override anonymization script, or ``None`` if unset.

        Verified live: ``GET /xapi/anonymize/projects/{project}`` -> 200
        text/plain when a project-specific script has been set, 204 (empty
        body) when it has not -- the project simply inherits the site
        script. 204 is not an error.

        Args:
            project: Project ID.

        Returns:
            The project's override script text, or ``None`` if it has none.

        Raises:
            ResourceNotFoundError: If the project does not exist.
        """
        self._check_project_exists(project)
        resp = self.client.get(f"/xapi/anonymize/projects/{quote_path_segment(project)}")
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.text

    def set_project_script(self, project: str, script: str) -> None:
        """Replace a project's override anonymization script.

        Verified live: ``PUT /xapi/anonymize/projects/{project}`` with a
        ``text/plain`` body -> 200, empty response.

        Args:
            project: Project ID.
            script: The full new DicomEdit script text.

        Raises:
            ResourceNotFoundError: If the project does not exist.
        """
        self._check_project_exists(project)
        self.client.put(
            f"/xapi/anonymize/projects/{quote_path_segment(project)}",
            content=script.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )

    def project_enabled(self, project: str) -> bool:
        """Whether a project's override anonymization script is active.

        Verified live: ``GET /xapi/anonymize/projects/{project}/enabled`` ->
        a bare JSON boolean, ``false`` when the project has no override
        script at all -- this route does not require one to exist, unlike
        the ``PUT`` form (see :meth:`check_project_enable_scope`).

        Args:
            project: Project ID.

        Raises:
            ResourceNotFoundError: If the project does not exist.
        """
        self._check_project_exists(project)
        path = f"/xapi/anonymize/projects/{quote_path_segment(project)}/enabled"
        return bool(self.client.get_json(path))

    def project_has_script(self, project: str) -> bool:
        """Whether a script has ever been set for a project, regardless of its enabled state.

        Verified live: ``GET /data/projects/{project}/config/anon`` -- a
        different, older route from ``/xapi/anonymize/projects/{project}``
        -- returns the project's full anon-script version history,
        including entries whose ``status`` is ``disabled``, as long as at
        least one version exists; a clean 404 ("Couldn't find config for
        user ... on tool [anon] path [null]") means none ever has. Unlike
        :meth:`get_project_script`, this does NOT read as "unset" just
        because the current version happens to be disabled -- see the
        module docstring.

        Args:
            project: Project ID. Not existence-checked here; call
                :meth:`get_project_script` or catch ``ResourceNotFoundError``
                from :class:`~xnatctl.services.projects.ProjectService` first
                if that distinction matters to the caller.

        Returns:
            Whether at least one script version has ever been set.
        """
        path = f"/data/projects/{quote_path_segment(project)}/config/anon"
        try:
            self.client.get(path)
        except ResourceNotFoundError:
            return False
        return True

    def check_project_enable_scope(self, project: str) -> None:
        """Validate a project enable/disable request without changing any state.

        Runs the exact preflight :meth:`set_project_enabled` runs before its
        ``PUT``, and is also the entry point ``anon enable``/``anon
        disable --dry-run`` call in :mod:`xnatctl.cli.anon` -- a dry run that
        skipped this would report success for a project whose ``PUT``
        would actually 500.

        Args:
            project: Project ID.

        Raises:
            ResourceNotFoundError: If the project does not exist.
            XNATCtlError: If the project has no override script set yet --
                the project-scoped ``PUT .../enabled`` route 500s
                ("Couldn't find the site configuration for tool anon and
                path ...") in that state (verified live).
        """
        self._check_project_exists(project)
        if not self.project_has_script(project):
            raise XNATCtlError(
                f"Project {project!r} has no anonymization script set. Run "
                f"`anon set -P {project} FILE` first -- enabling or disabling "
                "requires a project-specific script to already exist."
            )

    def set_project_enabled(self, project: str, enable: bool) -> None:
        """Enable or disable a project's override anonymization script.

        Args:
            project: Project ID.
            enable: Whether the project's override script should be active.

        Raises:
            ResourceNotFoundError: If the project does not exist.
            XNATCtlError: If the project has no override script set yet.
        """
        self.check_project_enable_scope(project)
        path = f"/xapi/anonymize/projects/{quote_path_segment(project)}/enabled"
        self._put_enabled(path, enable)

    def _put_enabled(self, path: str, enable: bool) -> None:
        """Shared PUT for the ``.../enabled`` routes. See the module docstring for the contract."""
        self.client.put(
            path,
            params={"enable": "true" if enable else "false"},
            content=_ENABLE_BODY,
            headers=_ENABLE_HEADERS,
        )
