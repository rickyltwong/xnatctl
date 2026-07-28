"""Prearchive service for XNAT prearchive operations."""

from __future__ import annotations

import builtins
import re
from typing import Any
from urllib.parse import quote

from xnatctl.core.exceptions import (
    OperationError,
    ResourceExistsError,
    ResourceNotFoundError,
)

from .base import BaseService


def _quote_path_segment(value: str) -> str:
    """Encode a single REST path segment for XNAT service URIs."""
    return quote(value, safe="").replace(".", "%2E")


# XNAT's prearchive services answer HTTP 200 with an error-shaped body rather
# than a 4xx, so the status code alone cannot be trusted (ROB-10).
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
    and no payload, and failing those would be worse than the silence ROB-10 set
    out to fix.
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
        if project:
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
            # This used to be `if "404" in str(e)`, which also fired on any
            # unrelated error whose text merely contained "404" -- a session
            # labelled SUB404, for instance (ROB-10).
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
        if experiment_label and resolved_subject is None:
            session = self.get(project, timestamp, session_name)
            resolved_subject = session.get("subject")
            if not resolved_subject:
                raise ValueError("Cannot archive to a specific experiment label without a subject")

        if resolved_subject:
            dest = (
                f"/archive/projects/{encoded_project}"
                f"/subjects/{_quote_path_segment(resolved_subject)}"
            )
            if experiment_label:
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
        # the body of a 200 (ROB-10).
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
