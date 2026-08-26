"""Prearchive service for XNAT prearchive operations."""

from __future__ import annotations

import builtins
import re
from typing import Any

from xnatctl.core.exceptions import (
    OperationError,
    PermissionDeniedError,
    ResourceExistsError,
    ResourceNotFoundError,
    XNATCtlError,
)
from xnatctl.core.validation import quote_path_segment
from xnatctl.core.validation import quote_prearchive_segment as _quote_path_segment

from .base import BaseService

# The three meaningful project prearchive-routing codes, read out of
# ``org.nrg.framework.constants.PrearchiveCode``'s static initializer in the
# running server's ``framework-1.9.2.jar`` via ``javap -p -c`` (ordinal 0/1/2
# -> code 0/4/5). The server itself does NOT validate what gets written to
# ``PUT /data/projects/{id}/prearchive_code/{value}`` -- verified live,
# ``.../3`` and ``.../9`` both answer 200 and are stored verbatim -- so this
# mapping (and rejecting anything outside it, via the CLI's ``click.Choice``)
# is the only thing standing between a typo and a project silently left in an
# undefined routing state.
PREARCHIVE_MODE_TO_CODE: dict[str, int] = {
    "manual": 0,
    "auto-archive": 4,
    "auto-archive-overwrite": 5,
}
PREARCHIVE_CODE_TO_MODE: dict[int, str] = {
    code: mode for mode, code in PREARCHIVE_MODE_TO_CODE.items()
}

# XNAT's prearchive services answer HTTP 200 with an error-shaped body rather
# than a 4xx, so the status code alone cannot be trusted.
_EXPERIMENT_URI_RE = re.compile(r"/data/(?:archive/)?experiments/[^\s\"'<>]+")
_CONFLICT_MARKERS = ("already exists", "conflict")
_ERROR_MARKERS = ("error", "exception", "failed", "failure", "not allowed", "denied")
_RESULT_SNIPPET_CHARS = 200


def _response_text(result: Any) -> str:
    """Flatten a ``_post`` result (parsed JSON or text) to searchable text."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return str(result)


def _experiment_uri(result: Any) -> str | None:
    """Return the archived experiment URI from a response body, if present."""
    match = _EXPERIMENT_URI_RE.search(_response_text(result))
    return match.group(0) if match else None


def _raise_if_error_shaped(operation: str, result: Any, details: dict[str, Any]) -> None:
    """Raise when a 200 response carries an error-shaped body.

    Only positively error-shaped bodies raise. An empty or unrecognised body is
    left alone: several XNAT deployments answer these services with a bare 200
    and no payload, and failing those would be worse than the silence this check
    exists to fix.
    """
    text = _response_text(result).strip()
    if not text:
        return

    lowered = text.lower()
    snippet = text[:_RESULT_SNIPPET_CHARS]

    if any(marker in lowered for marker in _CONFLICT_MARKERS):
        raise ResourceExistsError("prearchive session", snippet)

    # A body that names an experiment URI is a success report, even if some
    # surrounding prose happens to contain one of the generic error words.
    if _experiment_uri(result) is None and any(marker in lowered for marker in _ERROR_MARKERS):
        raise OperationError(
            operation,
            f"XNAT returned HTTP 200 with an error-shaped body: {snippet}",
            details,
        )


class PrearchiveService(BaseService):
    """Service for XNAT prearchive operations."""

    def list(
        self,
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List prearchive sessions.

        Args:
            project: Filter by project ID

        Returns:
            List of prearchive session dicts
        """
        # `is not None`, not truthy: `project=""` is a caller mistake, not
        # "no filter" -- it must not silently widen to every project's
        # prearchive. _quote_path_segment already rejects the empty string.
        if project is not None:
            path = f"/data/prearchive/projects/{_quote_path_segment(project)}"
        else:
            path = "/data/prearchive"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return self._extract_results(data)

    def get(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> dict[str, Any]:
        """Get prearchive session details.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            Prearchive session dict

        Raises:
            ResourceNotFoundError: If session not found
        """
        path = (
            f"/data/prearchive/projects/{_quote_path_segment(project)}"
            f"/{_quote_path_segment(timestamp)}/{_quote_path_segment(session_name)}"
        )
        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            # Re-scope the client's typed 404 to name the prearchive session.
            # Matching the typed exception, not the text: a string match
            # like `if "404" in str(e)` would also fire on any unrelated
            # error whose text merely contains "404" -- a session labelled
            # SUB404, for instance.
            raise ResourceNotFoundError(
                "prearchive session",
                f"{project}/{timestamp}/{session_name}",
            ) from e

        results = self._extract_results(data)
        if results:
            return results[0]
        raise ResourceNotFoundError("prearchive session", f"{project}/{timestamp}/{session_name}")

    def archive(
        self,
        project: str,
        timestamp: str,
        session_name: str,
        subject: str | None = None,
        experiment_label: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Archive a session from prearchive.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive
            subject: Target subject ID (optional, uses DICOM if not provided)
            experiment_label: Target session label
            overwrite: Overwrite existing session data

        Returns:
            Result dict with archived session info
        """
        encoded_project = _quote_path_segment(project)
        encoded_timestamp = _quote_path_segment(timestamp)
        encoded_session_name = _quote_path_segment(session_name)
        src = f"/prearchive/projects/{encoded_project}/{encoded_timestamp}/{encoded_session_name}"
        data: dict[str, Any] = {"src": src}

        resolved_subject = subject
        # `is not None`, not truthy: `experiment_label=""` must still trigger
        # the subject lookup (and later get rejected at the point of use)
        # rather than being silently treated the same as "no experiment
        # label override".
        if experiment_label is not None and resolved_subject is None:
            session = self.get(project, timestamp, session_name)
            resolved_subject = session.get("subject")
            if not resolved_subject:
                raise ValueError("Cannot archive to a specific experiment label without a subject")

        # `is not None`, not truthy: an explicitly-supplied `subject=""`
        # would skip a truthy check entirely (treated the same as "not
        # provided"), silently falling back to XNAT's DICOM-derived subject
        # instead of raising on the caller's empty value -- a different
        # archive destination than either the caller or "no override" meant.
        # _quote_path_segment below already rejects the empty string.
        if resolved_subject is not None:
            dest = (
                f"/archive/projects/{encoded_project}"
                f"/subjects/{_quote_path_segment(resolved_subject)}"
            )
            if experiment_label is not None:
                dest = f"{dest}/experiments/{_quote_path_segment(experiment_label)}"
            data["dest"] = dest
        if overwrite:
            data["overwrite"] = "delete"

        try:
            result = self._post("/data/services/archive", data=data)
        except ResourceNotFoundError as exc:
            # XNAT returns 404 when the prearchive path no longer exists. The
            # most common cause is that the session was already archived (a
            # successful archive removes it from the prearchive), so a bare
            # "resource not found: /data/services/archive" is misleading. Raise
            # an idempotency-aware, actionable error instead (see issue #20).
            raise OperationError(
                "archive",
                f"Prearchive session '{session_name}' was not found under "
                f"{project}/{timestamp}. It may already be archived (a successful "
                f"archive removes the session from the prearchive), or the "
                f"project/timestamp/session identifiers are incorrect. Verify with: "
                f"xnatctl session show -P {project} -E {session_name}",
                {"project": project, "timestamp": timestamp, "session": session_name},
            ) from exc

        # A 2xx is not proof of success: XNAT reports conflicts and failures in
        # the body of a 200.
        details = {"project": project, "timestamp": timestamp, "session": session_name}
        _raise_if_error_shaped("archive", result, details)

        return {
            "success": True,
            "project": project,
            "session": session_name,
            "experiment_uri": _experiment_uri(result),
            "result": result,
        }

    def delete(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> bool:
        """Delete a session from prearchive.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            True if successful
        """
        path = (
            f"/data/prearchive/projects/{_quote_path_segment(project)}"
            f"/{_quote_path_segment(timestamp)}/{_quote_path_segment(session_name)}"
        )
        return self._delete(path)

    def rebuild(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> dict[str, Any]:
        """Rebuild/refresh a prearchive session.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            Result dict
        """
        path = (
            f"/data/prearchive/projects/{_quote_path_segment(project)}"
            f"/{_quote_path_segment(timestamp)}/{_quote_path_segment(session_name)}"
        )
        params = {"action": "rebuild"}

        result = self._post(path, params=params)
        _raise_if_error_shaped(
            "rebuild",
            result,
            {"project": project, "timestamp": timestamp, "session": session_name},
        )

        return {
            "success": True,
            "project": project,
            "session": session_name,
            "result": result,
        }

    def move(
        self,
        project: str,
        timestamp: str,
        session_name: str,
        target_project: str,
    ) -> dict[str, Any]:
        """Move a prearchive session to another project.

        Args:
            project: Source project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive
            target_project: Target project ID

        Returns:
            Result dict
        """
        path = (
            f"/data/prearchive/projects/{_quote_path_segment(project)}"
            f"/{_quote_path_segment(timestamp)}/{_quote_path_segment(session_name)}"
        )
        params = {
            "action": "move",
            "newProject": target_project,
        }

        result = self._post(path, params=params)
        _raise_if_error_shaped(
            "move",
            result,
            {
                "project": project,
                "timestamp": timestamp,
                "session": session_name,
                "target_project": target_project,
            },
        )

        return {
            "success": True,
            "source_project": project,
            "target_project": target_project,
            "session": session_name,
            "result": result,
        }

    def get_scans(
        self,
        project: str,
        timestamp: str,
        session_name: str,
    ) -> builtins.list[dict[str, Any]]:
        """Get scans from a prearchive session.

        Args:
            project: Project ID
            timestamp: Prearchive timestamp
            session_name: Session name in prearchive

        Returns:
            List of scan dicts
        """
        path = (
            f"/data/prearchive/projects/{_quote_path_segment(project)}"
            f"/{_quote_path_segment(timestamp)}/{_quote_path_segment(session_name)}/scans"
        )
        params = {"format": "json"}

        data = self._get(path, params=params)
        return self._extract_results(data)

    def get_routing_code(self, project: str) -> int:
        """Return a project's raw prearchive-routing code.

        Verified live against XNAT 1.9.2.1: ``GET
        /data/projects/{project}/prearchive_code`` answers 200 with a BARE
        INTEGER as the response text (e.g. ``4``) -- NOT JSON. A fresh
        project defaults to 4 (AutoArchive).

        Args:
            project: Project ID.

        Returns:
            The raw code as currently stored server-side. May be a value
            outside the three meaningful codes in :data:`PREARCHIVE_CODE_TO_MODE`
            -- the server accepts and stores any integer here without
            validation (see :meth:`set_routing_mode`) -- so callers must be
            prepared for an unrecognized code, not assume one of the three.

        Raises:
            XNATCtlError: If the response body is not parseable as an integer.
        """
        path = f"/data/projects/{quote_path_segment(project)}/prearchive_code"
        text = self.client.get(path).text.strip()
        try:
            return int(text)
        except ValueError as exc:
            raise XNATCtlError(
                f"Unexpected response from GET {path}: expected a bare integer, got {text!r}."
            ) from exc

    def set_routing_mode(self, project: str, mode: str) -> None:
        """Set a project's prearchive-routing mode.

        Verified live against XNAT 1.9.2.1: ``PUT
        /data/projects/{project}/prearchive_code/{code}`` answers 200 and the
        value reads back unchanged on a subsequent GET.

        Args:
            project: Project ID.
            mode: One of the keys in :data:`PREARCHIVE_MODE_TO_CODE`
                (``"manual"``, ``"auto-archive"``, ``"auto-archive-overwrite"``).
                The CLI restricts ``--set`` to these via ``click.Choice``
                before this is ever called; this method trusts its caller
                and does not re-validate.

        Raises:
            KeyError: If ``mode`` is not one of the three valid mode names.
            XNATCtlError: If the server refuses a NON-MANUAL mode change with
                HTTP 403. xnat-web's ``ProjectResource.java`` refuses any
                NON-ZERO prearchive_code when the site property
                ``project.allow-auto-archive`` is disabled, for every user
                including admins -- this is site policy, not a permissions
                problem, so the client's generic ``PermissionDeniedError`` is
                re-raised here with that explanation instead.
            PermissionDeniedError: If the server refuses ``mode="manual"``
                (prearchive_code 0) with HTTP 403. That site property only
                governs non-zero codes, so a 403 here is an ordinary
                authorization failure -- rewriting it as the site-policy
                message would send the caller to the wrong setting.
        """
        code = PREARCHIVE_MODE_TO_CODE[mode]
        path = f"/data/projects/{quote_path_segment(project)}/prearchive_code/{code}"
        try:
            self.client.put(path)
        except PermissionDeniedError as exc:
            if code == 0:
                raise
            raise XNATCtlError(
                f"XNAT refused to set project {project!r} to prearchive mode "
                f"{mode!r} (HTTP 403). This is site policy, not a permissions "
                "problem: the site property 'project.allow-auto-archive' is "
                "disabled, which blocks any non-manual prearchive_code for "
                "every user, including admins. Ask a site admin to enable it, "
                "or use --set manual instead."
            ) from exc
