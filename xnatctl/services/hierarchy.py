"""Shared hierarchy path building and resolution helpers."""

from __future__ import annotations

import re
from typing import Any

from xnatctl.core.exceptions import InvalidIdentifierError, ResourceNotFoundError, XNATCtlError
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.hierarchy import (
    ExperimentRef,
    HierarchyParentRef,
    ItemsEnvelope,
    ProjectRef,
    ResolvedExperimentRef,
    ResolvedSubjectRef,
    ResourceRef,
    ResultSetEnvelope,
    ScanRef,
    SubjectRef,
)
from xnatctl.models.session import Session
from xnatctl.models.subject import Subject

from .base import BaseService

# Accession-ID-shaped tokens such as ``XNAT_E00001`` or ``CLM01_E12``.
# Applied with `fullmatch` at both call sites below, not `match`:
# Python's `$` also matches just BEFORE a final newline, so
# `"XNAT_E00001\n"` matches a `^...$` pattern under `.match()` and
# would be routed as a clean accession ID.
_ACCESSION_ID_PATTERN = re.compile(r"[A-Z][A-Za-z0-9]*_E\d+")


def join_api_path(*parts: str | None) -> str:
    """Join API path segments into a normalized absolute path.

    Each part is percent-encoded as its own path segment via
    :func:`~xnatctl.core.validation.quote_path_segment` -- dots stay literal
    (XNAT subject/experiment labels routinely contain them), but a reserved
    character embedded in a caller-supplied identifier (``/``, ``?``, ``#``,
    etc.) is neutralized into that same segment rather than allowed to route
    the request somewhere else. Refs (:mod:`xnatctl.models.hierarchy`) already
    reject those characters at construction; this is defense in depth for the
    few builders below that still take a raw string directly (e.g.
    ``build_subject_collection_path(project_id=...)``).

    ``None`` is the one deliberate "omit this part" sentinel (an optional
    segment a caller genuinely doesn't have); every part actually supplied is
    a plain string with no reason to carry a leading/trailing ``/`` of its
    own -- every static literal in this module is a single bare word ("data",
    "projects", "scans", ...), and every caller-supplied value either goes
    through a ref (which already rejects ``/`` outright) or is quoted here.
    So a leading/trailing ``/`` at this point is never legitimate content to
    silently reinterpret; quietly stripping it would turn ``"/TARGET"`` into
    ``"TARGET"`` (a different resource for what was actually invalid input)
    and collapse a bare ``"/"`` to an empty segment (a double-slash route),
    so it is rejected instead.

    Raises:
        InvalidIdentifierError: If a supplied (non-``None``) part is empty,
            composed entirely of ``/`` characters, or has a leading or
            trailing ``/``.
    """
    segments = []
    for part in parts:
        if part is None:
            continue
        stripped = part.strip("/")
        if stripped == "":
            raise InvalidIdentifierError("path segment", part, "cannot be empty or slash-only")
        if stripped != part:
            raise InvalidIdentifierError(
                "path segment", part, "cannot have a leading or trailing '/'"
            )
        segments.append(quote_path_segment(part))
    return "/" + "/".join(segments)


class HierarchyService(BaseService):
    """Build and resolve hierarchy-aware XNAT paths."""

    @staticmethod
    def build_project_path(ref: ProjectRef, *parts: str) -> str:
        """Build a project-scoped path."""
        return join_api_path("data", "projects", ref.project_id, *parts)

    @staticmethod
    def build_subject_collection_path(project_id: str | None = None) -> str:
        """Build a subject collection path.

        ``project_id`` is a raw string, not a ref, so nothing has validated
        it yet. ``None`` means "no project filter" (the site-wide
        collection) -- but an explicitly-supplied empty string is a
        different thing and must not silently widen to that same site-wide
        scope, so it is rejected rather than treated as falsy.

        Raises:
            InvalidIdentifierError: If ``project_id`` is supplied but empty
                or whitespace-only.
        """
        if project_id is not None:
            if project_id.strip() == "":
                raise InvalidIdentifierError(
                    "project_id", project_id, "cannot be empty when explicitly supplied"
                )
            return join_api_path("data", "projects", project_id, "subjects")
        return join_api_path("data", "subjects")

    @classmethod
    def build_subject_path(cls, ref: SubjectRef, *parts: str) -> str:
        """Build a subject item path."""
        if ref.project_id:
            return join_api_path(
                "data", "projects", ref.project_id, "subjects", ref.subject, *parts
            )
        if ref.is_label:
            raise ValueError("Subject labels require project context")
        return join_api_path("data", "subjects", ref.subject, *parts)

    @staticmethod
    def build_experiment_collection_path(
        project_id: str | None = None, subject: str | None = None
    ) -> str:
        """Build an experiment collection path.

        ``project_id``/``subject`` are raw strings, not refs, so nothing has
        validated them yet. ``None`` means "no filter at this level" (widens
        the scope) -- an explicitly-supplied empty string is a different
        thing and must not silently widen the same way, so it is rejected
        rather than treated as falsy.

        Raises:
            ValueError: If ``subject`` is supplied without ``project_id``.
            InvalidIdentifierError: If ``project_id`` or ``subject`` is
                supplied but empty or whitespace-only.
        """
        if project_id is not None and project_id.strip() == "":
            raise InvalidIdentifierError(
                "project_id", project_id, "cannot be empty when explicitly supplied"
            )
        if subject is not None and subject.strip() == "":
            raise InvalidIdentifierError(
                "subject", subject, "cannot be empty when explicitly supplied"
            )
        if subject is not None and project_id is None:
            raise ValueError("Subject scope requires project context")
        if project_id is not None and subject is not None:
            return join_api_path("data", "projects", project_id, "subjects", subject, "experiments")
        if project_id is not None:
            return join_api_path("data", "projects", project_id, "experiments")
        return join_api_path("data", "experiments")

    @classmethod
    def build_experiment_path(cls, ref: ExperimentRef, *parts: str) -> str:
        """Build an experiment item path."""
        if ref.subject and not ref.project_id:
            raise ValueError("Subject scope requires project context")
        if ref.experiment_is_label and not ref.project_id:
            raise ValueError("Experiment labels require project context")
        if ref.subject_is_label and not ref.project_id:
            raise ValueError("Subject labels require project context")

        if ref.project_id and ref.subject:
            return join_api_path(
                "data",
                "projects",
                ref.project_id,
                "subjects",
                ref.subject,
                "experiments",
                ref.experiment,
                *parts,
            )
        if ref.project_id:
            return join_api_path(
                "data", "projects", ref.project_id, "experiments", ref.experiment, *parts
            )
        return join_api_path("data", "experiments", ref.experiment, *parts)

    @staticmethod
    def routable_scan_parent(ref: ExperimentRef) -> ExperimentRef:
        """Return an experiment ref whose ``/scans`` suffix XNAT will route.

        XNAT ignores sub-resource suffixes on
        ``/data/projects/{P}/experiments/{E}``. It answers ``/scans``,
        ``/scans/{id}``, ``/scans/ALL/files`` -- and equally a nonsense suffix
        -- with 200 and the *parent experiment document*. Verified live: on one
        session ``/scans`` returned a single ``items[]`` record for the
        experiment, while the subject-scoped and flat forms both returned a
        23-row ``ResultSet``.

        Two consequences make this worth normalizing centrally rather than at
        each call site: a listing silently yields zero rows (``extract_rows``
        finds no ``ResultSet``), and a DELETE resolves to the experiment rather
        than the scan.

        Routing needs either the subject segment or the flat
        ``/data/experiments/{E}`` form. When the caller has no subject, fall
        back to the flat form -- permissions are enforced server-side, so
        dropping the project from the URL costs nothing but ambiguity. A
        genuine *label* cannot drop its project, so it is left alone; callers
        must resolve it to an accession ID first.

        ``experiment_is_label`` means "may be a label" -- callers set it
        whenever a project is in scope, including for accession IDs -- so the
        accession-ID shape is what decides, not the flag alone.
        """
        if not ref.project_id or ref.subject:
            return ref
        if ref.experiment_is_label and not _ACCESSION_ID_PATTERN.fullmatch(ref.experiment):
            return ref
        return ExperimentRef(experiment=ref.experiment)

    @classmethod
    def build_scan_collection_path(cls, ref: ExperimentRef) -> str:
        """Build a scan collection path."""
        return cls.build_experiment_path(cls.routable_scan_parent(ref), "scans")

    def get_experiment_json(
        self,
        ref: ExperimentRef,
        *parts: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Fetch an experiment document, or one of its sub-resources, as JSON.

        The single raw read behind experiment inspection and the session-show
        scans/resources listings. Keeping it here lets the CLI operate on
        hierarchy refs without reaching through ``service.client``.
        """
        path = self.build_experiment_path(ref, *parts)
        if params is None:
            return self.client.get_json(path)
        return self.client.get_json(path, params=params)

    @classmethod
    def build_scan_path(cls, ref: ScanRef, *parts: str) -> str:
        """Build a scan item path."""
        return cls.build_experiment_path(
            cls.routable_scan_parent(ref.experiment), "scans", ref.scan_id, *parts
        )

    def list_scan_rows(
        self, ref: ExperimentRef, session_xsi_type: str | None
    ) -> list[dict[str, Any]]:
        """List the raw scan rows for an experiment.

        The scans endpoint returns every scan of a session regardless of its
        ``xsiType``, so the reliable query is an unfiltered listing. Some
        non-imaging session types (e.g. ``xnat:eegSessionData``) can return an
        empty ResultSet unless the request narrows to the matching scan
        ``xsiType``; for those, fall back to a filtered query.

        Filtering must never be the primary query: a scan ``xsiType`` cannot be
        derived from the session ``xsiType`` in general (an
        ``xnat:optSessionData`` session holds ``xnat:otherDicomScanData`` scans,
        not ``xnat:optScanData``), so a guessed filter would silently drop every
        scan whose type does not match the guess. See issue #16.

        Args:
            ref: Parent experiment reference.
            session_xsi_type: Experiment ``xsiType``, used only for the fallback.

        Returns:
            List of scan row dicts (possibly empty).
        """
        path = self.build_scan_collection_path(ref)
        rows = self.extract_rows(self.client.get_json(path))
        if rows:
            return rows

        scan_xsi = self.resolve_scan_xsi_type(session_xsi_type)
        if not scan_xsi:
            return rows
        fallback = self.client.get_json(path, params={"xsiType": scan_xsi})
        return self.extract_rows(fallback)

    @classmethod
    def build_resource_collection_path(cls, parent: HierarchyParentRef) -> str:
        """Build a resource collection path for any supported parent level."""
        if isinstance(parent, ProjectRef):
            return cls.build_project_path(parent, "resources")
        if isinstance(parent, SubjectRef):
            return cls.build_subject_path(parent, "resources")
        if isinstance(parent, ExperimentRef):
            return cls.build_experiment_path(parent, "resources")
        if isinstance(parent, ScanRef):
            return cls.build_scan_path(parent, "resources")
        raise TypeError(f"Unsupported resource parent: {type(parent)!r}")

    @classmethod
    def build_resource_path(cls, ref: ResourceRef, *parts: str) -> str:
        """Build a resource item path for any supported parent level.

        ``build_resource_collection_path`` already returns a fully-joined,
        fully-quoted path (a leading ``/`` and internal ``/``s between real
        segments) -- passing it back through ``join_api_path`` as one more
        "part" would quote its internal slashes too, mangling the URL. Only
        the new segments (``resource_label``, ``*parts``) are raw and need
        quoting; the collection prefix is concatenated as-is.
        """
        prefix = cls.build_resource_collection_path(ref.parent).rstrip("/")
        suffix = join_api_path(ref.resource_label, *parts)
        return f"{prefix}{suffix}"

    @staticmethod
    def extract_rows(data: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract collection rows from a response.

        XNAT collection endpoints usually return ``ResultSet.Result``, but some
        docs still show a bare top-level JSON array for older/project endpoints.
        """
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return ResultSetEnvelope.model_validate(data).rows

    @staticmethod
    def extract_rows_strict(
        data: dict[str, Any] | list[dict[str, Any]], what: str
    ) -> list[dict[str, Any]]:
        """Extract collection rows, raising on a body that isn't the envelope.

        :meth:`extract_rows` validates through ``ResultSetEnvelope``, whose
        ``result_set``/``results`` fields both carry ``default_factory`` --
        so a 200 response with no ``ResultSet`` key at all (a plugin error
        body like ``{"message": "plugin disabled"}``, say) validates to zero
        rows, indistinguishable from a query that legitimately matched
        nothing. Reporting "0 results" for a server that never answered the
        question is the failure mode this avoids.

        Only an absent envelope raises. A genuinely empty result set
        (``{"ResultSet": {"Result": []}}``) still returns ``[]``, as does a
        present ``ResultSet`` with no ``Result`` inside it, and a bare
        top-level array is passed through unchanged.

        Most callers still use the lenient :meth:`extract_rows`; migrating
        one is a matter of passing a description of the request here.

        Args:
            data: The raw JSON body.
            what: Short description of the request, for the error message.

        Returns:
            The extracted rows.

        Raises:
            XNATCtlError: If ``data`` is a dict with no top-level
                ``ResultSet`` key.
        """
        if isinstance(data, dict) and "ResultSet" not in data:
            raise XNATCtlError(
                f"Unexpected response while {what}: no 'ResultSet' field in a "
                f"200 response. Got keys: {sorted(data.keys())!r}."
            )
        return HierarchyService.extract_rows(data)

    @staticmethod
    def extract_first_item(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Extract the first `items[]` record's data fields and meta.

        Unlike :meth:`extract_item_children`, a missing top-level ``items``
        key here is not treated as malformed: callers of this method (e.g.
        ``SubjectService.get``/``SessionService.get``) always follow a
        ``None`` result with an :meth:`extract_rows` fallback for a
        ResultSet-shaped response, so an ``items``-less document is still a
        recognized shape at this call site, just the other one.
        """
        item = ItemsEnvelope.model_validate(data).first_item
        if item is None:
            return None
        return item.data_fields, item.meta

    @staticmethod
    def extract_item_children(data: dict[str, Any], child_field: str) -> list[dict[str, Any]]:
        """Extract the ``data_fields`` of every child item under one ``children[]`` entry.

        A detailed (``format=json``) XNAT document nests related records under
        ``items[0].children``, each keyed by a ``field`` name -- e.g.
        ``"fields/field"`` for a subject/experiment's custom variables, or
        ``"sharing/share"`` for the projects it is shared into. This is the
        one place that walks that shape, used by custom-variable read (see
        ``SubjectService.list_vars``/``SessionService.list_vars``).

        Args:
            data: The raw ``format=json`` document.
            child_field: The ``children[].field`` value to match (e.g.
                ``"fields/field"``).

        Returns:
            The ``data_fields`` dict of each item under the matching child,
            in document order. Empty if there is no first item, or no child
            with that field name, or that child's ``items`` is absent/null
            (all legitimately mean "no records here").

        Raises:
            XNATCtlError: If the top-level ``items`` key is absent or not a
                list, if ``items[0]`` is present but is not an object with a
                ``data_fields`` key, or if a matching child's ``items`` is
                present but not a list, or a list entry is not an object
                with a ``data_fields`` object. A plugin or schema regression
                here must not be reported as "no custom variables" -- see
                ``commands.py``'s ``wrappers_of`` for the same rule.
        """
        # Unlike ``extract_first_item``, every caller of THIS method (the
        # subject/session custom-variable readers) always requests a
        # detailed format=json document and has no ResultSet-shaped
        # fallback to fall through to -- so an absent top-level ``items``
        # key here is not "genuinely empty", it is a body this client does
        # not recognize (e.g. a plugin error like
        # ``{"message": "plugin disabled"}``). ``ItemsEnvelope``'s
        # ``default_factory=list`` would otherwise let that validate
        # silently to zero rows, reported as "no custom variables" for a
        # request that never actually returned the expected shape.
        # ``{"items": []}`` is unaffected -- that is the genuinely-empty
        # case and stays a normal empty read.
        if "items" not in data:
            raise XNATCtlError(
                "Unexpected response: no top-level 'items' field in a detailed "
                f"(format=json) document. Got keys: {sorted(data.keys())!r}."
            )
        raw_items = data["items"]
        if not isinstance(raw_items, list):
            raise XNATCtlError(
                "Unexpected response: top-level 'items' is not a list in a detailed "
                f"(format=json) document. Got {type(raw_items).__name__} ({raw_items!r})."
            )
        # ``ItemRecord``'s fields all carry ``default_factory``, so a
        # present-but-empty entry like ``{}`` would otherwise validate
        # silently into an item with no data_fields/children/meta --
        # indistinguishable from a legitimately empty one. Checked against
        # the raw dict, before Pydantic coerces it, for the same reason:
        # coercion is also where a non-object entry would surface as a raw
        # ``pydantic.ValidationError`` instead of this method's own typed
        # one.
        if raw_items:
            first_raw = raw_items[0]
            if not isinstance(first_raw, dict) or "data_fields" not in first_raw:
                raise XNATCtlError(
                    "Unexpected entry at items[0] in a detailed (format=json) document: "
                    f"expected an object with a 'data_fields' key, got {first_raw!r}."
                )
        item = ItemsEnvelope.model_validate(data).first_item
        if item is None:
            return []
        for child in item.children:
            if not isinstance(child, dict) or child.get("field") != child_field:
                continue
            child_items = child.get("items")
            if child_items is None:
                return []
            if not isinstance(child_items, list):
                raise XNATCtlError(
                    f"Unexpected 'items' under children[].field={child_field!r}: "
                    f"expected a list or null, got {type(child_items).__name__} "
                    f"({child_items!r})."
                )
            rows: list[dict[str, Any]] = []
            for index, it in enumerate(child_items):
                if not isinstance(it, dict) or not isinstance(it.get("data_fields"), dict):
                    raise XNATCtlError(
                        f"Unexpected entry at children[].field={child_field!r}.items[{index}]: "
                        f"expected an object with a 'data_fields' object, got {it!r}."
                    )
                rows.append(it["data_fields"])
            return rows
        return []

    @staticmethod
    def stringify_field(value: Any) -> str:
        """Coerce a raw custom-variable field value to display text.

        ``None`` -- whether the source key was absent or explicitly present
        as ``null`` -- renders as ``""``, never as the literal text
        ``"None"`` a bare ``str(None)`` would produce. A genuinely-absent
        value staying absent is fine; a null value silently becoming the
        three-character string ``"None"`` is not.
        """
        return "" if value is None else str(value)

    @staticmethod
    def resolve_scan_xsi_type(session_xsi_type: str | None) -> str | None:
        """Translate an experiment xsiType into the matching scan xsiType."""
        if not session_xsi_type or "sessiondata" not in session_xsi_type.lower():
            return None
        return session_xsi_type.replace("SessionData", "ScanData").replace(
            "sessionData", "scanData"
        )

    @classmethod
    def parse_resolved_subject(cls, ref: SubjectRef, data: dict[str, Any]) -> ResolvedSubjectRef:
        """Parse a subject detail or summary response into a resolved ref."""
        item = cls.extract_first_item(data)
        if item is not None:
            fields, _meta = item
            return ResolvedSubjectRef(
                project_id=str(fields.get("project") or ref.project_id or "") or None,
                subject_id=str(fields.get("ID") or fields.get("id") or ref.subject),
                subject_label=str(fields.get("label") or "") or None,
                uri=str(fields.get("URI") or fields.get("uri") or "") or None,
            )

        rows = cls.extract_rows(data)
        if rows:
            subject = Subject.model_validate(rows[0])
            return ResolvedSubjectRef(
                project_id=subject.project or ref.project_id,
                subject_id=subject.id,
                subject_label=subject.label,
                uri=subject.uri,
            )

        raise ResourceNotFoundError("subject", ref.subject)

    @classmethod
    def parse_resolved_experiment(
        cls, ref: ExperimentRef, data: dict[str, Any]
    ) -> ResolvedExperimentRef:
        """Parse an experiment detail or summary response into a resolved ref."""
        item = cls.extract_first_item(data)
        if item is not None:
            fields, meta = item
            session = Session.model_validate(fields)
            return ResolvedExperimentRef(
                project_id=session.project or ref.project_id,
                subject_id=session.subject_id,
                subject_label=session.subject_label,
                experiment_id=session.id,
                experiment_label=session.label,
                session_date=session.session_date.isoformat() if session.session_date else None,
                xsi_type=session.xsi_type or str(meta.get("xsi:type") or "") or None,
                uri=session.uri,
            )

        rows = cls.extract_rows(data)
        if rows:
            session = Session.model_validate(rows[0])
            return ResolvedExperimentRef(
                project_id=session.project or ref.project_id,
                subject_id=session.subject_id,
                subject_label=session.subject_label,
                experiment_id=session.id,
                experiment_label=session.label,
                session_date=session.session_date.isoformat() if session.session_date else None,
                xsi_type=session.xsi_type,
                uri=session.uri,
            )

        raise ResourceNotFoundError("session", ref.experiment)

    def resolve_subject(self, ref: SubjectRef) -> ResolvedSubjectRef:
        """Resolve a subject reference to canonical IDs."""
        data = self.client.get_json(self.build_subject_path(ref))
        return self.parse_resolved_subject(ref, data)

    def resolve_experiment(self, ref: ExperimentRef) -> ResolvedExperimentRef:
        """Resolve an experiment reference to canonical IDs.

        When the direct GET against ``build_experiment_path(ref)`` either raises
        ``ResourceNotFoundError`` (HTTP 404) or returns a payload that
        ``parse_resolved_experiment`` cannot resolve (empty ``ResultSet``), the
        method falls back to:

        1. Listing project experiments via
           ``GET /data/projects/{project_id}/experiments?columns=ID,label,subject_ID,xsiType``
           and matching ``ref.experiment`` client-side against ``label`` OR ``ID``
           (exact, case-sensitive). Requires ``ref.experiment_is_label`` and a
           ``project_id``.
        2. If ``ref.experiment`` is accession-ID-shaped (matches
           ``^[A-Z][A-Za-z0-9]*_E\\d+$``), trying ``GET /data/experiments/{ID}``
           as a final cross-project fallback.

        If neither fallback resolves, the original
        ``ResourceNotFoundError("session", ref.experiment)`` is raised.
        """
        try:
            data = self.client.get_json(self.build_experiment_path(ref))
        except ResourceNotFoundError:
            return self._resolve_experiment_fallback(ref)

        try:
            return self.parse_resolved_experiment(ref, data)
        except ResourceNotFoundError:
            return self._resolve_experiment_fallback(ref)

    def _resolve_experiment_fallback(self, ref: ExperimentRef) -> ResolvedExperimentRef:
        """Apply project-listing and accession-ID fallbacks for label resolution."""
        if ref.experiment_is_label and ref.project_id:
            canonical_id = self._lookup_experiment_in_project_listing(
                ref.project_id, ref.experiment
            )
            if canonical_id is not None:
                canonical_ref = ExperimentRef(
                    experiment=canonical_id,
                    project_id=ref.project_id,
                    subject=ref.subject,
                    experiment_is_label=False,
                    subject_is_label=ref.subject_is_label,
                )
                return self.resolve_experiment(canonical_ref)

        if _ACCESSION_ID_PATTERN.fullmatch(ref.experiment):
            try:
                data = self.client.get_json(join_api_path("data", "experiments", ref.experiment))
            except ResourceNotFoundError:
                pass
            else:
                try:
                    return self.parse_resolved_experiment(ref, data)
                except ResourceNotFoundError:
                    pass

        raise ResourceNotFoundError("session", ref.experiment)

    def _lookup_experiment_in_project_listing(self, project_id: str, token: str) -> str | None:
        """Return canonical accession ID for ``token`` within ``project_id``.

        Issues a single
        ``GET /data/projects/{project_id}/experiments?columns=ID,label,subject_ID,xsiType``
        call and matches ``token`` against ``label`` or ``ID`` exactly
        (case-sensitive). Returns ``None`` when no row matches.
        """
        try:
            data = self.client.get_json(
                join_api_path("data", "projects", project_id, "experiments"),
                params={"columns": "ID,label,subject_ID,xsiType"},
            )
        except ResourceNotFoundError:
            return None

        for row in self.extract_rows(data):
            row_id = str(row.get("ID") or "")
            row_label = str(row.get("label") or "")
            if token in (row_id, row_label):
                return row_id or None
        return None
