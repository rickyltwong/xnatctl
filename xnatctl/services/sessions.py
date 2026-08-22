"""Session/Experiment service for XNAT session operations."""

from __future__ import annotations

import builtins
from typing import Any

from xnatctl.core.exceptions import InputValidationError, ResourceNotFoundError
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.models.session import Session

from .base import BaseService
from .hierarchy import HierarchyService

#: Substring that identifies each modality inside an experiment's xsiType,
#: e.g. "xnat:mrSessionData" for MR. A table rather than four ``elif`` branches
#: because ruff's SIM114 autofix collapsed the equivalent branches into one
#: 200-character boolean.
_MODALITY_XSI_MARKERS = {
    "MR": "mrsession",
    "PET": "petsession",
    "CT": "ctsession",
    "EEG": "eegsession",
}


class SessionService(BaseService):
    """Service for XNAT session/experiment operations."""

    def list(
        self,
        project: str | None = None,
        subject: str | None = None,
        modality: str | None = None,
        limit: int | None = None,
        columns: builtins.list[str] | None = None,
    ) -> builtins.list[Session]:
        """List sessions/experiments.

        Args:
            project: Filter by project ID
            subject: Filter by subject ID
            modality: Filter by modality (MR, PET, CT)
            limit: Maximum number of results
            columns: Specific columns to retrieve

        Returns:
            List of Session objects
        """
        # `is not None`, not truthy: an explicitly-supplied `project=""` (or
        # `subject=""`) is a caller mistake, not "no filter" -- it must not
        # silently widen to the unfiltered/site-wide listing. quote_path_segment
        # already rejects the empty string wherever it's used below.
        #
        # `subject` without `project` used to be silently DROPPED (falling
        # through to the same site-wide /data/experiments as "no filter at
        # all"), not merely widened by an empty string -- XNAT subject
        # labels aren't globally unique without a project to scope them, so
        # there is no route this could route to that would honor it. Same
        # convention as HierarchyService.build_experiment_collection_path.
        if subject is not None and project is None:
            raise InputValidationError(
                "subject filter requires project", field="subject", value=subject
            )
        if project is not None and subject is not None:
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/subjects/{quote_path_segment(subject)}/experiments"
            )
        elif project is not None:
            path = f"/data/projects/{quote_path_segment(project)}/experiments"
        else:
            path = "/data/experiments"

        params: dict[str, Any] = {"format": "json"}
        if columns:
            params["columns"] = ",".join(columns)
        # `is not None`, not truthy: `modality=""` is a caller mistake, not
        # "no modality filter" -- it must not silently widen to every
        # modality. A query-param value (httpx encodes it safely), so the
        # risk is a wider read, not a different route.
        if modality is not None:
            if modality.strip() == "":
                raise InputValidationError(
                    "modality filter cannot be empty or whitespace-only",
                    field="modality",
                    value=modality,
                )
            params["xsiType"] = f"xnat:{modality.lower()}SessionData"

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
            results = results[:limit]

        return [Session(**r) for r in results]

    def get(
        self,
        session_id: str,
        project: str | None = None,
    ) -> Session:
        """Get session details.

        Args:
            session_id: Session ID or label
            project: Project ID (helps with label lookup)

        Returns:
            Session object

        Raises:
            ResourceNotFoundError: If session not found
        """
        if (
            project is not None
        ):  # not truthy -- "" must raise, not fall to the flat experiments path
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/experiments/{quote_path_segment(session_id)}"
            )
        else:
            path = f"/data/experiments/{quote_path_segment(session_id)}"

        params = {"format": "json"}

        try:
            data = self._get(path, params=params)
        except ResourceNotFoundError as e:
            raise ResourceNotFoundError("session", session_id) from e

        item = HierarchyService.extract_first_item(data) if isinstance(data, dict) else None
        if item is not None:
            fields, meta = item
            normalized = dict(fields)
            if meta.get("xsi:type") and not normalized.get("xsiType"):
                normalized["xsiType"] = meta["xsi:type"]
            return Session.model_validate(normalized)

        results = HierarchyService.extract_rows(data)
        if results:
            return Session.model_validate(results[0])
        raise ResourceNotFoundError("session", session_id)

    def create(
        self,
        project: str,
        subject: str,
        label: str,
        xsi_type: str = "xnat:mrSessionData",
        date: str | None = None,
        time: str | None = None,
        visit_id: str | None = None,
        modality: str | None = None,
    ) -> Session:
        """Create a new session/experiment.

        Args:
            project: Project ID
            subject: Subject ID or label
            label: Session label
            xsi_type: XSI type (xnat:mrSessionData, xnat:petSessionData, etc)
            date: Session date (YYYY-MM-DD)
            time: Session time (HH:MM:SS)
            visit_id: Visit identifier
            modality: Modality (overrides xsi_type if provided)

        Returns:
            Created Session object
        """
        path = (
            f"/data/projects/{quote_path_segment(project)}"
            f"/subjects/{quote_path_segment(subject)}"
            f"/experiments/{quote_path_segment(label)}"
        )
        params: dict[str, Any] = {}

        # Determine xsi_type from modality if provided
        if modality:
            modality_map = {
                "MR": "xnat:mrSessionData",
                "PET": "xnat:petSessionData",
                "CT": "xnat:ctSessionData",
            }
            xsi_type = modality_map.get(modality.upper(), xsi_type)

        params["xsiType"] = xsi_type
        if date:
            params["date"] = date
        if time:
            params["time"] = time
        if visit_id:
            params["visit_id"] = visit_id

        self._put(path, params=params)
        return self.get(label, project=project)

    def delete(
        self,
        session_id: str,
        project: str | None = None,
        remove_files: bool = False,
    ) -> bool:
        """Delete a session.

        Args:
            session_id: Session ID
            project: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/experiments/{quote_path_segment(session_id)}"
            )
        else:
            path = f"/data/experiments/{quote_path_segment(session_id)}"

        params: dict[str, Any] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def get_scans(
        self,
        session_id: str,
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get scans for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of scan data dicts
        """
        # Built rather than interpolated: the project-scoped form is not a scan
        # listing. XNAT answers /data/projects/{P}/experiments/{E}/scans with
        # the experiment document, so this returned zero rows for every session
        # that had a project. See ``HierarchyService.routable_scan_parent``.
        path = HierarchyService.build_scan_collection_path(
            ExperimentRef(experiment=session_id, project_id=project)
        )

        params = {"format": "json"}
        data = self._get(path, params=params)
        return HierarchyService.extract_rows(data)

    def get_resources(
        self,
        session_id: str,
        project: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Get resources for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of resource data dicts
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/experiments/{quote_path_segment(session_id)}/resources"
            )
        else:
            path = f"/data/experiments/{quote_path_segment(session_id)}/resources"

        params = {"format": "json"}
        data = self._get(path, params=params)
        return HierarchyService.extract_rows(data)

    def set_field(
        self,
        session_id: str,
        field: str,
        value: str,
        project: str | None = None,
    ) -> bool:
        """Set a field value on a session.

        Args:
            session_id: Session ID
            field: Field name (e.g., 'note', 'acquisition_site')
            value: Field value
            project: Project ID

        Returns:
            True if successful
        """
        if project is not None:  # not truthy -- see get() above
            path = (
                f"/data/projects/{quote_path_segment(project)}"
                f"/experiments/{quote_path_segment(session_id)}"
            )
        else:
            path = f"/data/experiments/{quote_path_segment(session_id)}"

        params = {field: value}
        self._put(path, params=params)
        return True

    def share(
        self,
        session_id: str,
        target_project: str,
        label: str | None = None,
        primary: bool = False,
    ) -> bool:
        """Share a session with another project.

        Args:
            session_id: Session ID
            target_project: Target project ID
            label: New label in target project
            primary: Make target the primary project

        Returns:
            True if successful
        """
        path = (
            f"/data/experiments/{quote_path_segment(session_id)}"
            f"/projects/{quote_path_segment(target_project)}"
        )
        params: dict[str, Any] = {}

        if label:
            params["label"] = label
        if primary:
            params["primary"] = "true"

        self._put(path, params=params)
        return True

    # -------------------------------------------------------------------------
    # Raw-row accessors
    #
    # The typed ``list``/``get_scans``/``get_resources`` above return models or
    # take a different scope than the CLI's screens need. These issue exactly
    # the request each CLI command sends and hand back the untyped rows it
    # renders.
    # -------------------------------------------------------------------------

    def list_project_experiment_rows(
        self, project: str, subject: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Return raw experiment rows for the ``session list`` screen."""
        params = {"columns": "ID,label,subject_label,date,xsiType"}
        # `is not None`, not truthy: `subject=""` is a caller mistake, not
        # "no subject filter" -- it must not silently widen to every
        # experiment in the project. This is a query-param VALUE (httpx
        # encodes it safely), so the risk is a wider result set, not a
        # different route.
        if subject is not None:
            if subject.strip() == "":
                raise InputValidationError(
                    "subject filter cannot be empty or whitespace-only",
                    field="subject",
                    value=subject,
                )
            params["subject_label"] = subject
        resp = self.client.get_json(
            f"/data/projects/{quote_path_segment(project)}/experiments", params=params
        )
        rows: builtins.list[dict[str, Any]] = resp.get("ResultSet", {}).get("Result", [])
        return rows

    def list_sessions(
        self,
        project: str,
        *,
        subject: str | None = None,
        modality: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Return classified, modality-filtered rows for the ``session list`` screen.

        Each row's ``xsiType`` is mapped to a display modality; when *modality*
        is given, rows whose xsiType does not carry that modality's marker are
        dropped.

        Args:
            project: Project ID.
            subject: Optional subject-label filter.
            modality: Optional modality filter (MR/PET/CT/EEG).

        Returns:
            Render-ready dicts with id/label/subject/date/modality keys.
        """
        # `is not None`, not truthy: `modality=""` is a caller mistake, not
        # "no modality filter" -- it must not silently widen to every
        # session, matching list()'s own modality filter above. Checked
        # before the fetch, not after, so a bad modality value fails
        # without making an HTTP request at all.
        if modality is not None and modality.strip() == "":
            raise InputValidationError(
                "modality filter cannot be empty or whitespace-only",
                field="modality",
                value=modality,
            )
        rows = self.list_project_experiment_rows(project, subject=subject)
        marker = _MODALITY_XSI_MARKERS.get(modality) if modality is not None else None

        sessions: builtins.list[dict[str, Any]] = []
        for r in rows:
            xsi_lower = r.get("xsiType", "").lower()
            if modality is not None and marker and marker not in xsi_lower:
                continue

            detected_modality = "?"
            if "mrsession" in xsi_lower:
                detected_modality = "MR"
            elif "petsession" in xsi_lower:
                detected_modality = "PET"
            elif "ctsession" in xsi_lower:
                detected_modality = "CT"
            elif "eegsession" in xsi_lower:
                detected_modality = "EEG"

            sessions.append(
                {
                    "id": r.get("ID", ""),
                    "label": r.get("label", ""),
                    "subject": r.get("subject_label", ""),
                    "date": r.get("date", ""),
                    "modality": detected_modality,
                }
            )
        return sessions

    def scan_rows(
        self, session_id: str, project: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Return raw scan rows for a session via a routable scans URL."""
        path = HierarchyService.build_scan_collection_path(
            ExperimentRef(experiment=session_id, project_id=project)
        )
        resp = self.client.get_json(path)
        rows: builtins.list[dict[str, Any]] = resp.get("ResultSet", {}).get("Result", [])
        return rows

    def experiment_resource_rows(
        self,
        session_id: str,
        project: str | None = None,
        subject: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Return raw session-level resource rows (subject-scoped URL)."""
        path = HierarchyService.build_experiment_path(
            ExperimentRef(experiment=session_id, project_id=project, subject=subject),
            "resources",
        )
        resp = self.client.get_json(path)
        rows: builtins.list[dict[str, Any]] = resp.get("ResultSet", {}).get("Result", [])
        return rows
