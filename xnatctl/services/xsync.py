"""XSync service for XNAT XSync API operations.

Wraps the ``/xapi/xsync/...`` REST surface so the CLI never has to assemble
URLs or pick Content-Types directly. The ``credentials/save`` and
``credentials/check`` endpoints require ``Content-Type: text/plain`` (XNAT
returns 415 on ``application/json``); ``remoteREST`` is sent as
``application/json``; everything else uses the client's defaults.

The high-level :meth:`XsyncService.refresh_credentials` orchestrates the
three-step rotation flow (``remoteREST`` -> ``credentials/save`` ->
``credentials/check``) used by the ``xnatctl xsync refresh-credentials``
command. The remote password supplied to that method is only ever placed
in request bodies; it is never logged, never echoed, and never embedded in
a URL query string.
"""

from __future__ import annotations

import json
from typing import Any

from xnatctl.core.validation import quote_path_segment
from xnatctl.services.base import BaseService

# Single source of truth for the XSync URL templates so tests can pin them.
_LIST_PATH: str = "/xapi/xsync/projects"
_SETUP_PATH: str = "/xapi/xsync/setup/projects/{project_id}"
_STATUS_PATH: str = "/xapi/xsync/status/projects/{project_id}"
_HISTORY_PATH: str = "/xapi/xsync/history/projects/{project_id}"
_PROGRESS_PATH: str = "/xapi/xsync/progress/projects/{project_id}"
_SYNC_PATH: str = "/xapi/xsync/projects/{project_id}"
_SYNC_SUBJECT_PATH: str = "/xapi/xsync/syncsubject/{experiment_id}"
_REMOTE_REST_PATH: str = "/xapi/xsync/remoteREST"
_CREDENTIALS_SAVE_PATH: str = "/xapi/xsync/credentials/save/projects/{project_id}"
_CREDENTIALS_CHECK_PATH: str = "/xapi/xsync/credentials/check/projects/{project_id}"

_TEXT_PLAIN_HEADERS: dict[str, str] = {"Content-Type": "text/plain"}


class XsyncService(BaseService):
    """Service for XNAT XSync feature operations.

    All HTTP I/O goes through ``self.client`` (an ``XNATClient``). The
    service never shells out to the CLI escape hatch and never imports from
    ``xnatctl.cli.*``; the layering rule in ``decisions.md`` is intentional.
    """

    # ----- read endpoints -------------------------------------------------

    def list_projects(self) -> Any:
        """List XSync-bound projects.

        Returns:
            The parsed JSON payload from ``GET /xapi/xsync/projects``. The
            exact shape is XNAT-version dependent; callers should not
            assume a specific schema and should pass the value through to
            output formatters.
        """
        return self._get(_LIST_PATH)

    def get_setup(self, project_id: str) -> Any:
        """Fetch the XSync setup record for a project."""
        return self._get(_SETUP_PATH.format(project_id=quote_path_segment(project_id)))

    def get_status(self, project_id: str) -> Any:
        """Fetch the XSync status record for a project."""
        return self._get(_STATUS_PATH.format(project_id=quote_path_segment(project_id)))

    def get_history(self, project_id: str) -> Any:
        """Fetch the XSync run history for a project."""
        return self._get(_HISTORY_PATH.format(project_id=quote_path_segment(project_id)))

    def get_progress(self, project_id: str) -> str:
        """Fetch the current XSync progress log as plain text.

        The XNAT endpoint returns ``text/plain``; the CLI streams the body
        straight to stdout without going through the ``api get`` JSON
        fallback path.
        """
        resp = self.client.get(_PROGRESS_PATH.format(project_id=quote_path_segment(project_id)))
        return resp.text

    # ----- write endpoints ------------------------------------------------

    def sync(self, project_id: str) -> Any:
        """Trigger an XSync run for the given project."""
        return self._post(_SYNC_PATH.format(project_id=quote_path_segment(project_id)))

    def sync_subject(self, experiment_id: str) -> Any:
        """Trigger an XSync run for a single experiment / session."""
        return self._post(
            _SYNC_SUBJECT_PATH.format(experiment_id=quote_path_segment(experiment_id))
        )

    def remote_rest(
        self,
        url: str,
        method: str,
        username: str,
        password: str,
    ) -> Any:
        """Proxy a REST call to a remote XNAT via ``/xapi/xsync/remoteREST``.

        Sent as ``application/json`` (the server expects a JSON body). The
        response includes the ephemeral ``alias`` / ``secret`` /
        ``xdatUserId`` triple that the subsequent ``credentials/save`` call
        persists on the local XNAT.

        Args:
            url: Remote XNAT base URL.
            method: HTTP method to proxy (``GET`` / ``POST`` / ...).
            username: Remote XNAT username.
            password: Remote XNAT password. Never logged.

        Returns:
            Parsed JSON response body.
        """
        payload = {
            "url": url,
            "method": method,
            "username": username,
            "password": password,
        }
        resp = self.client.post(_REMOTE_REST_PATH, json=payload)
        return resp.json()

    def save_credentials(self, project_id: str, payload: dict[str, Any]) -> Any:
        """Persist remote credentials for ``project_id``.

        XNAT requires ``Content-Type: text/plain`` even though the body
        itself is a JSON document; ``application/json`` returns 415.

        Args:
            project_id: Local XNAT project id.
            payload: Body to serialize and send verbatim under ``text/plain``.

        Returns:
            Parsed JSON response when the server returns one, otherwise the
            raw text body.
        """
        body = json.dumps(payload)
        resp = self.client.post(
            _CREDENTIALS_SAVE_PATH.format(project_id=quote_path_segment(project_id)),
            data=body,
            headers=_TEXT_PLAIN_HEADERS,
        )
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    def check_credentials(self, project_id: str, payload: dict[str, Any]) -> Any:
        """Validate the stored remote credentials for ``project_id``.

        Same ``text/plain`` framing constraint as :meth:`save_credentials`.
        On success XNAT returns the new JSESSIONID; the caller should not
        log the response body at default verbosity.

        Args:
            project_id: Local XNAT project id.
            payload: Body to serialize and send verbatim under ``text/plain``.

        Returns:
            Parsed JSON response when the server returns one, otherwise the
            raw text body.
        """
        body = json.dumps(payload)
        resp = self.client.post(
            _CREDENTIALS_CHECK_PATH.format(project_id=quote_path_segment(project_id)),
            data=body,
            headers=_TEXT_PLAIN_HEADERS,
        )
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # ----- high-level orchestration ---------------------------------------

    def refresh_credentials(
        self,
        project_id: str,
        remote_url: str,
        remote_username: str,
        remote_password: str,
        local_project: str,
        remote_project: str,
        sync_new_only: bool = True,
    ) -> dict[str, Any]:
        """Run the three-step XSync credential rotation flow.

        The flow:

        1. POST ``/xapi/xsync/remoteREST`` (application/json) to mint a
           short-lived alias/secret pair against the remote XNAT.
        2. POST ``/xapi/xsync/credentials/save/projects/{project_id}``
           (text/plain) to persist the alias/secret on the local XNAT.
        3. POST ``/xapi/xsync/credentials/check/projects/{project_id}``
           (text/plain) to validate that the saved credentials work.

        Args:
            project_id: Local XNAT project id (the credentials are saved
                under this project).
            remote_url: Remote XNAT base URL.
            remote_username: Remote XNAT username.
            remote_password: Remote XNAT password. Only placed in the
                ``remoteREST`` request body; never logged.
            local_project: Project id passed in the save/check payloads as
                ``localProject``. Usually equal to ``project_id``.
            remote_project: Project id passed in the save payload as
                ``remoteProject``.
            sync_new_only: Whether the saved configuration should request
                "new only" sync semantics.

        Returns:
            Dict summarising the flow with keys ``alias`` (echo only — not
            the value), ``saved`` (bool), ``checked`` (bool). The remote
            password and the returned secret are NOT included.
        """
        remote_response = self.remote_rest(
            url=remote_url,
            method="POST",
            username=remote_username,
            password=remote_password,
        )

        alias = remote_response.get("alias")
        secret = remote_response.get("secret")
        xdat_user_id = remote_response.get("xdatUserId")

        save_payload: dict[str, Any] = {
            "host": remote_url,
            "username": alias,
            "password": secret,
            "xdatUserId": xdat_user_id,
            "localProject": local_project,
            "remoteProject": remote_project,
            "syncNew": sync_new_only,
        }
        self.save_credentials(project_id, save_payload)

        check_payload: dict[str, Any] = {
            "host": remote_url,
            "localProject": local_project,
        }
        self.check_credentials(project_id, check_payload)

        return {
            "project": project_id,
            "remote_url": remote_url,
            "local_project": local_project,
            "remote_project": remote_project,
            "alias_present": alias is not None,
            "saved": True,
            "checked": True,
        }
