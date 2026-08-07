"""Shared hierarchy path building and resolution helpers."""

from __future__ import annotations

import re
from typing import Any

from xnatctl.core.exceptions import ResourceNotFoundError
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
_ACCESSION_ID_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*_E\d+$")


def join_api_path(*parts: str | None) -> str:
    """Join API path segments into a normalized absolute path."""
    return "/" + "/".join(part.strip("/") for part in parts if part)


class HierarchyService(BaseService):
    """Build and resolve hierarchy-aware XNAT paths."""

    @staticmethod
    def build_project_path(ref: ProjectRef, *parts: str) -> str:
        """Build a project-scoped path."""
        return join_api_path("data", "projects", ref.project_id, *parts)

    @staticmethod
    def build_subject_collection_path(project_id: str | None = None) -> str:
        """Build a subject collection path."""
        if project_id:
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
        """Build an experiment collection path."""
        if subject and not project_id:
            raise ValueError("Subject scope requires project context")
        if project_id and subject:
            return join_api_path("data", "projects", project_id, "subjects", subject, "experiments")
        if project_id:
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
        if ref.experiment_is_label and not _ACCESSION_ID_PATTERN.match(ref.experiment):
            return ref
        return ExperimentRef(experiment=ref.experiment)

    @classmethod
    def build_scan_collection_path(cls, ref: ExperimentRef) -> str:
        """Build a scan collection path."""
        return cls.build_experiment_path(cls.routable_scan_parent(ref), "scans")

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
        """Build a resource item path for any supported parent level."""
        return join_api_path(
            cls.build_resource_collection_path(ref.parent),
            ref.resource_label,
            *parts,
        )

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
    def extract_first_item(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Extract the first `items[]` record's data fields and meta."""
        item = ItemsEnvelope.model_validate(data).first_item
        if item is None:
            return None
        return item.data_fields, item.meta

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

        if _ACCESSION_ID_PATTERN.match(ref.experiment):
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
