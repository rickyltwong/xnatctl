"""Session/Experiment service for XNAT session operations."""

from __future__ import annotations

import builtins
import re
from typing import Any

import httpx

from xnatctl.core.exceptions import (
    ClientRequestError,
    InputValidationError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.hierarchy import ExperimentRef
from xnatctl.models.resource import Resource
from xnatctl.models.scan import Scan
from xnatctl.models.session import Session

from .base import BaseService
from .hierarchy import HierarchyService
from .resources import ResourceService
from .scans import ScanService

#: Same constraint as ``SubjectService``'s custom-variable names -- see that
#: module's own comment. Kept as a separate module-level compile rather than
#: shared, matching how each service file already carries its own small
#: local helpers (e.g. ``_normalize_modality_filter`` above) instead of a
#: shared cross-file utility for a two-line regex.
# Matched with `fullmatch`, not `match`: Python's `$` also matches just
# BEFORE a final newline, so a `^...$` + `.match()` pair accepts
# "field\n" and embeds the newline in the XNAT query key.
_FIELD_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _validate_field_name(name: str) -> str:
    """Reject a custom-variable name that could break the field[] expression."""
    if not name or not _FIELD_NAME_RE.fullmatch(name):
        raise InputValidationError(
            "custom variable name must be a non-empty string of letters, digits, '.', '_' or '-'",
            field="name",
            value=name,
        )
    return name


#: XNAT session xsiTypes follow `xnat:{modality}SessionData` -- the same
#: pattern `list()` builds when it sends `xsiType` to the server (see below).
#: Matching it generically, instead of a fixed MR/PET/CT/EEG table, is what
#: makes modality detection and filtering work for any modality XNAT accepts
#: (US, XA, CR, MG, ..., and PETMR as its own type distinct from PET), not
#: just the four that table happened to name.
#: Matched with `fullmatch`, not `.match()` on an anchored `^...$` pattern --
#: see `_FIELD_NAME_RE` above for why that combination is unsafe.
_XSI_MODALITY_RE = re.compile(r"xnat:([a-z0-9]+)sessiondata")

#: User-facing modality names that diverge from the xsiType segment XNAT
#: actually stores. OCT ("Optical Coherence Tomography", what users and
#: clinicians call it) is archived as `xnat:optSessionData` -- OPT is
#: DICOM's own modality code for Ophthalmic Tomography. Confirmed against
#: the xnat-web schema (`xnat_optSessionData.js`) and this project's own
#: 0.2.11 fix for the same xsiType (see CHANGELOG.md). `--modality OCT` and
#: `--modality OPT` both have to match; `_detect_modality` below still
#: reports the row's real segment ("OPT"), only the REQUESTED filter value
#: is aliased.
_MODALITY_ALIASES = {"OCT": "OPT"}


def _detect_modality(xsi_type: str) -> str:
    """Best-effort modality extracted from an experiment's xsiType.

    Args:
        xsi_type: The experiment's raw ``xsiType`` (any case).

    Returns:
        The modality segment, uppercased (e.g. ``"MR"``, ``"OPT"``), or
        ``"?"`` when ``xsi_type`` does not follow the
        ``xnat:{modality}SessionData`` pattern. This is XNAT's own segment,
        not a user-facing alias -- an OCT session reports ``"OPT"`` here;
        see ``_MODALITY_ALIASES`` for the filter-side translation.
    """
    match = _XSI_MODALITY_RE.fullmatch(xsi_type.lower())
    return match.group(1).upper() if match else "?"


def _normalize_modality_filter(modality: str) -> str:
    """Resolve a user-supplied ``--modality`` value through the alias table.

    Args:
        modality: The raw filter value as given (any case).

    Returns:
        The uppercased xsiType segment to compare ``_detect_modality``
        against -- the alias target when ``modality`` is a known alias (e.g.
        ``"OCT"`` -> ``"OPT"``), otherwise ``modality`` itself, uppercased.
    """
    upper = modality.upper()
    return _MODALITY_ALIASES.get(upper, upper)


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
            modality: Filter by modality (MR, PET, CT, PETMR, OCT, ...).
                "OCT" is accepted as an alias for XNAT's "OPT" xsiType
                segment (see ``_MODALITY_ALIASES``).
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
        # `subject` without `project` must not be silently DROPPED (falling
        # through to the same site-wide /data/experiments as "no filter at
        # all") -- XNAT subject labels aren't globally unique without a
        # project to scope them, so there is no route that would honor it.
        # Same convention as
        # HierarchyService.build_experiment_collection_path.
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
            # Alias-resolved (e.g. "OCT" -> "OPT") for the same reason
            # list_sessions() below is -- XNAT stores OCT sessions as
            # `xnat:optSessionData`, not `xnat:octSessionData`.
            params["xsiType"] = f"xnat:{_normalize_modality_filter(modality).lower()}SessionData"
        if limit is not None:  # not truthy -- limit=0 must mean 0 results, not "unlimited"
            params["limit"] = str(limit)

        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        if limit is not None:  # belt-and-braces: some XNAT endpoints ignore `limit`
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
    ) -> builtins.list[Scan]:
        """Get scans for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of :class:`Scan` models

        Raises:
            pydantic.ValidationError: If a scan row lacks the fields
                :class:`Scan` requires (an ``ID``).
        """
        # Delegated: ScanService.list issues the identical routable-scans
        # request (see ``HierarchyService.routable_scan_parent``) and owns the
        # row -> Scan validation.
        return ScanService(self.client).list(session_id, project)

    def get_resources(
        self,
        session_id: str,
        project: str | None = None,
    ) -> builtins.list[Resource]:
        """Get resources for a session.

        Args:
            session_id: Session ID
            project: Project ID

        Returns:
            List of :class:`Resource` models
        """
        # Delegated: ResourceService.list issues the identical experiment-level
        # resources request and owns the row normalization (resource rows carry
        # xnat_abstractresource_id rather than ID) and row -> Resource
        # validation.
        return ResourceService(self.client).list(session_id=session_id, project=project)

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
        """Share a session with another project without moving it.

        Verified live against XNAT 1.9.2.1: this is the real, working shape --
        PUT /data/experiments/{id}/projects/{target_project}.

        Args:
            session_id: Session ID (canonical accession ID; the flat
                ``/data/experiments/{id}`` route this builds does not accept
                a bare label).
            target_project: Target project ID
            label: New label in target project
            primary: Make target the primary project

        Returns:
            True if successful

        Raises:
            ResourceExistsError: The session is already shared into
                ``target_project`` (XNAT answers this with HTTP 409).
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

        try:
            self._put(path, params=params)
        except ClientRequestError as e:
            if e.status_code == 409:
                raise ResourceExistsError(
                    "session share", f"{session_id} -> {target_project}"
                ) from e
            raise
        return True

    def unshare(
        self, session_id: str, target_project: str, *, primary_project: str
    ) -> httpx.Response:
        """Remove a session's share into another project.

        ``primary_project`` is required, and aiming this at it is refused.
        Verified live against XNAT 1.9.2.1: ``DELETE
        /data/experiments/{id}/projects/{primary}`` answers 200 and
        **deletes the experiment outright** -- afterwards it is 404. The
        response is indistinguishable from removing an ordinary share, so a
        mistyped target project silently destroys the session's data while
        reporting that a share was removed. The guard is here rather than
        only in the CLI so a library caller gets it too.

        Project IDs are compared case-insensitively -- refusing slightly too
        much is the right trade when a false negative deletes a session.

        Verified live: unlike the subject equivalent, XNAT answers a
        never-shared ``target_project`` with HTTP 200 -- unsharing a session
        is idempotent where unsharing a subject is not.

        Args:
            session_id: Session ID (canonical accession ID).
            target_project: Project to remove the share from.
            primary_project: The session's owning project, used to refuse
                the destructive case.

        Returns:
            The raw DELETE response.

        Raises:
            InputValidationError: If ``target_project`` is the session's
                primary project.
        """
        if not primary_project or not primary_project.strip():
            raise InputValidationError(
                f"refusing to unshare session {session_id}: the primary project is unknown, so "
                "the check that stops this from deleting the session outright cannot run. "
                "Pass the owning project explicitly.",
                field="primary_project",
                value=primary_project,
            )
        if target_project.strip().casefold() == primary_project.strip().casefold():
            raise InputValidationError(
                f"refusing to unshare session {session_id} from {target_project}: that is its "
                "primary project, and XNAT answers this by deleting the session and its data, "
                "not by removing a share. Delete the session explicitly if that is what you "
                "want.",
                field="from",
                value=target_project,
            )
        path = (
            f"/data/experiments/{quote_path_segment(session_id)}"
            f"/projects/{quote_path_segment(target_project)}"
        )
        return self.client.delete(path)

    def list_shares(self, session_id: str) -> builtins.list[dict[str, Any]]:
        """List every project a session is assigned to (primary + shared).

        Args:
            session_id: Session ID (canonical accession ID).

        Returns:
            One row per project, each carrying ``ID`` (the project id),
            ``label`` (the session's label in that project),
            ``Secondary_ID`` and ``Name`` -- exactly what
            ``GET /data/experiments/{id}/projects`` returns, including
            the session's own primary project as one of the rows.
        """
        data = self.client.get_json(f"/data/experiments/{quote_path_segment(session_id)}/projects")
        return HierarchyService.extract_rows(data)

    # -------------------------------------------------------------------------
    # Custom variables (the "xnat-varput" surface)
    #
    # Verified live and materially different from the subject case:
    #
    # * A flat ``/data/experiments/{id}`` PUT silently accepts (HTTP 200) a
    #   custom-field query key and does NOT persist it -- even a documented,
    #   always-writable field like `note` no-ops through that route on this
    #   XNAT version. Only the subject-scoped
    #   ``/data/projects/{P}/subjects/{S}/experiments/{E}`` route actually
    #   applies the write, which is why these methods require project+subject
    #   (resolved by the caller) rather than accepting a bare experiment ID.
    # * The field key must carry the experiment's own xsiType prefix
    #   (``xnat:mrSessionData/fields/field[name=X]/field``, not the
    #   unprefixed ``fields/field[name=X]/field`` that works for subjects) --
    #   the unprefixed form is the same silent-200-no-op trap.
    # * The PUT also needs an explicit ``xsiType`` query param alongside the
    #   field keys, or the same silent no-op happens.
    #
    # Reads are unaffected by any of this: GET on the flat
    # ``/data/experiments/{id}`` document returns the same ``fields/field``
    # children either way.
    # -------------------------------------------------------------------------

    def list_vars(self, experiment_id: str) -> builtins.list[dict[str, str]]:
        """List a session's custom variables.

        Args:
            experiment_id: Session ID (canonical accession ID).

        Returns:
            One dict per variable, each shaped ``{"name": ..., "value": ...}``.
        """
        data = self.client.get_json(
            f"/data/experiments/{quote_path_segment(experiment_id)}", params={"format": "json"}
        )
        children = HierarchyService.extract_item_children(data, "fields/field")
        return [
            {
                "name": HierarchyService.stringify_field(c.get("name")),
                "value": HierarchyService.stringify_field(c.get("field")),
            }
            for c in children
        ]

    def set_vars(
        self,
        *,
        project: str,
        subject: str,
        experiment_id: str,
        xsi_type: str,
        fields: dict[str, str],
    ) -> httpx.Response:
        """Set one or more custom variables on a session in a single request.

        Requires the resolved project, subject, and xsiType -- see the class
        docstring above for why the flat experiment route silently no-ops.
        Each name is validated against a plain-identifier shape before
        building the request -- see :func:`_validate_field_name`.

        Args:
            project: Canonical project ID.
            subject: Canonical subject ID.
            experiment_id: Session ID (canonical accession ID).
            xsi_type: The experiment's own xsiType (e.g.
                ``"xnat:mrSessionData"``), used to prefix every field key.
            fields: Mapping of variable name -> value; creates a variable
                that doesn't exist yet, overwrites one that does.

        Returns:
            The raw PUT response.

        Raises:
            InputValidationError: A field name is empty or contains a
                character outside the allowed identifier shape.
        """
        path = (
            f"/data/projects/{quote_path_segment(project)}"
            f"/subjects/{quote_path_segment(subject)}"
            f"/experiments/{quote_path_segment(experiment_id)}"
        )
        params: dict[str, str] = {"xsiType": xsi_type}
        for name, value in fields.items():
            key = f"{xsi_type}/fields/field[name={_validate_field_name(name)}]/field"
            params[key] = value
        return self.client.put(path, params=params)

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

        Each row's ``xsiType`` is mapped to a display modality via
        :func:`_detect_modality`; when *modality* is given (case-insensitive),
        rows whose detected modality does not match it are dropped. Works for
        any modality XNAT accepts (MR, PET, CT, EEG, US, XA, CR, MG, ...),
        not a fixed set -- including PETMR, which is its own xsiType and
        never matches a plain ``"PET"`` filter. ``"OCT"`` is accepted as an
        alias for the ``"OPT"`` segment XNAT actually stores (see
        ``_MODALITY_ALIASES``).

        Args:
            project: Project ID.
            subject: Optional subject-label filter.
            modality: Optional modality filter.

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
        modality_upper = _normalize_modality_filter(modality) if modality is not None else None

        sessions: builtins.list[dict[str, Any]] = []
        for r in rows:
            detected_modality = _detect_modality(r.get("xsiType", ""))
            if modality_upper is not None and detected_modality != modality_upper:
                continue

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
