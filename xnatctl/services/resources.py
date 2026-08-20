"""Resource service for XNAT resource operations."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from pathlib import Path

import httpx

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.hierarchy import (
    ExperimentRef,
    HierarchyParentRef,
    ProjectRef,
    ResourceRef,
    ScanRef,
    SubjectRef,
)
from xnatctl.models.resource import Resource, ResourceFile

from .base import BaseService
from .hierarchy import HierarchyService


class ResourceService(BaseService):
    """Service for XNAT resource operations.

    Resources attach at every hierarchy level (project, subject, experiment,
    scan). Callers may either pass a ``parent`` :class:`HierarchyParentRef`
    directly, or the legacy ``(session_id, scan_id, project)`` triple which is
    converted to the equivalent experiment/scan ref (the resulting URLs are
    byte-identical to the historical hardcoded paths).
    """

    @staticmethod
    def _resolve_parent(
        session_id: str | None,
        scan_id: str | None,
        project: str | None,
        parent: HierarchyParentRef | None,
    ) -> HierarchyParentRef:
        """Return an explicit *parent*, else build one from the legacy triple."""
        if parent is not None:
            return parent
        if session_id is None:
            raise ValueError("session_id or parent is required")
        experiment = ExperimentRef(experiment=session_id, project_id=project)
        if scan_id:
            return ScanRef(experiment=experiment, scan_id=scan_id)
        return experiment

    @staticmethod
    def _parent_key(parent: HierarchyParentRef) -> str:
        """Return a stable short identifier used for row-normalization fallbacks."""
        if isinstance(parent, ScanRef):
            return f"{parent.experiment.experiment}:{parent.scan_id}"
        if isinstance(parent, ExperimentRef):
            return parent.experiment
        if isinstance(parent, SubjectRef):
            return parent.subject
        if isinstance(parent, ProjectRef):
            return parent.project_id
        return "resource"

    @staticmethod
    def _collection_path(parent: HierarchyParentRef) -> str:
        """Return the ``.../resources`` collection path for *parent*."""
        return HierarchyService.build_resource_collection_path(parent)

    @staticmethod
    def _resource_path(parent: HierarchyParentRef, resource_label: str, *parts: str) -> str:
        """Return the ``.../resources/{label}[/parts]`` path for *parent*."""
        return HierarchyService.build_resource_path(
            ResourceRef(parent=parent, resource_label=resource_label), *parts
        )

    @staticmethod
    def _parse_optional_int(value: object) -> int | None:
        """Parse an optional integer from XNAT-ish API values.

        XNAT sometimes returns empty strings or non-numeric strings for numeric
        fields; those should be treated as missing.

        Args:
            value: Input value from API.

        Returns:
            Parsed integer, or None if not parseable.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return int(stripped)
            except ValueError:
                return None
        return None

    @classmethod
    def _normalize_resource_row(
        cls, row: Mapping[str, object], session_id: str
    ) -> dict[str, object]:
        """Normalize a resource row so it is safe to validate with Resource.

        Args:
            row: Raw row from XNAT ResultSet.
            session_id: Parent session ID used for stable fallback IDs.

        Returns:
            Normalized copy of the row.
        """
        normalized: dict[str, object] = dict(row)

        normalized["file_count"] = cls._parse_optional_int(normalized.get("file_count"))
        normalized["file_size"] = cls._parse_optional_int(normalized.get("file_size"))

        fallback_uri = normalized.get("xnat_abstractresource_id") or normalized.get("id")
        if not fallback_uri:
            fallback_uri = normalized.get("URI") or normalized.get("uri")

        raw_id = normalized.get("ID")
        if isinstance(raw_id, str) and not raw_id.strip():
            raw_id = None

        if raw_id is None:
            label_value = normalized.get("label") or normalized.get("Label")
            label = label_value.strip() if isinstance(label_value, str) else None
            if not label:
                label = None

            if fallback_uri:
                normalized["ID"] = str(fallback_uri)
            elif label is not None:
                normalized["ID"] = f"{session_id}:{label}"
            else:
                normalized["ID"] = session_id
        else:
            normalized["ID"] = str(raw_id)

        return normalized

    def list(
        self,
        session_id: str | None = None,
        scan_id: str | None = None,
        project: str | None = None,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> builtins.list[Resource]:
        """List resources for any hierarchy level.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            parent: Explicit parent ref (project/subject/experiment/scan); takes
                precedence over the legacy triple when provided.

        Returns:
            List of Resource objects
        """
        resolved = self._resolve_parent(session_id, scan_id, project, parent)
        path = self._collection_path(resolved)
        parent_key = self._parent_key(resolved)

        params: dict[str, str] = {"format": "json"}
        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        resources = []
        for r in results:
            r = self._normalize_resource_row(r, session_id=parent_key)
            if session_id:
                r["session_id"] = session_id
            if scan_id:
                r["scan_id"] = scan_id
            if project:
                r["project"] = project
            resources.append(Resource.model_validate(r))

        return resources

    def get(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        scan_id: str | None = None,
        project: str | None = None,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> Resource:
        """Get resource details.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            Resource object

        Raises:
            ResourceNotFoundError: If resource not found
        """
        resources = self.list(session_id, scan_id=scan_id, project=project, parent=parent)
        for resource in resources:
            if resource.label == resource_label:
                return resource

        raise ResourceNotFoundError("resource", resource_label)

    def list_files(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        scan_id: str | None = None,
        project: str | None = None,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> builtins.list[ResourceFile]:
        """List files in a resource.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            List of ResourceFile objects
        """
        resolved = self._resolve_parent(session_id, scan_id, project, parent)
        path = self._resource_path(resolved, resource_label, "files")

        params: dict[str, str] = {"format": "json"}
        data = self._get(path, params=params)
        results = HierarchyService.extract_rows(data)

        return [ResourceFile(**r) for r in results]

    def list_rows(self, parent: HierarchyParentRef) -> builtins.list[dict[str, object]]:
        """Return raw resource rows for a parent (renders in the CLI).

        Unlike :meth:`list`, this keeps every key XNAT sends and skips the
        ``Resource`` normalization, matching what the resource/scan screens
        print today.
        """
        data = self.client.get_json(HierarchyService.build_resource_collection_path(parent))
        return HierarchyService.extract_rows(data)

    def list_file_rows(
        self, parent: HierarchyParentRef, resource_label: str
    ) -> builtins.list[dict[str, object]]:
        """Return raw file rows for a resource (``resource_label`` pre-encoded)."""
        data = self.client.get_json(
            HierarchyService.build_resource_path(
                ResourceRef(parent=parent, resource_label=resource_label), "files"
            )
        )
        return HierarchyService.extract_rows(data)

    def create(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        scan_id: str | None = None,
        project: str | None = None,
        format: str | None = None,
        content: str | None = None,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> Resource:
        """Create a new resource.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            format: Resource format
            content: Content type
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            Created Resource object
        """
        resolved = self._resolve_parent(session_id, scan_id, project, parent)
        path = self._resource_path(resolved, resource_label)

        params: dict[str, str] = {}
        if format:
            params["format"] = format
        if content:
            params["content"] = content

        try:
            self._put(path, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            # 409 Conflict means the resource already exists; proceed to return it
        return self.get(resource_label=resource_label, parent=resolved)

    def delete(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        scan_id: str | None = None,
        project: str | None = None,
        remove_files: bool = True,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> bool:
        """Delete a resource.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            remove_files: Also remove files from filesystem
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            True if successful
        """
        resolved = self._resolve_parent(session_id, scan_id, project, parent)
        path = self._resource_path(resolved, resource_label)

        params: dict[str, str] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def upload_file(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        file_path: Path | None = None,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        overwrite: bool = False,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> dict[str, object]:
        """Upload a file to a resource.

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            file_path: Local file path
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP/TAR files after upload
            overwrite: Overwrite existing files
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            Upload result dict
        """
        if file_path is None:
            raise ValueError("file_path is required")
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        resolved = self._resolve_parent(session_id, scan_id, project, parent)
        path = self._resource_path(resolved, resource_label, "files", file_path.name)

        # XNAT requires ?inbody=true for a raw-body write to a
        # /resources/<label>/files/<name> endpoint; without it the file
        # API rejects the request with an opaque 400/500 (see issue #21).
        params: dict[str, str] = {"inbody": "true"}
        if extract:
            params["extract"] = "true"
        if overwrite:
            params["overwrite"] = "true"

        file_size = file_path.stat().st_size

        # Determine content type
        content_type = "application/octet-stream"
        suffix = file_path.suffix.lower()
        if suffix == ".zip":
            content_type = "application/zip"
        elif suffix in (".tar", ".tar.gz", ".tgz"):
            content_type = "application/x-tar"
        elif suffix in (".json",):
            content_type = "application/json"
        elif suffix in (".xml",):
            content_type = "application/xml"
        elif suffix in (".txt", ".csv"):
            content_type = "text/plain"

        with open(file_path, "rb") as f:
            self.client.put(
                path,
                params=params,
                content=f,
                headers={"Content-Type": content_type},
            )

        return {
            "success": True,
            "file": file_path.name,
            "size": file_size,
            "extracted": extract,
        }

    def upload_directory(
        self,
        session_id: str | None = None,
        resource_label: str = "",
        directory_path: Path | None = None,
        scan_id: str | None = None,
        project: str | None = None,
        overwrite: bool = False,
        *,
        parent: HierarchyParentRef | None = None,
    ) -> dict[str, object]:
        """Upload a directory to a resource (creates ZIP first).

        Args:
            session_id: Session ID (legacy experiment/scan scope)
            resource_label: Resource label
            directory_path: Local directory path
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            overwrite: Overwrite existing files
            parent: Explicit parent ref; takes precedence over the legacy triple.

        Returns:
            Upload result dict
        """
        import shutil
        import tempfile

        if directory_path is None:
            raise ValueError("directory_path is required")
        if not directory_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {directory_path}")

        # Create temporary ZIP
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = Path(tmp_dir) / f"{directory_path.name}.zip"
            shutil.make_archive(
                str(zip_path.with_suffix("")),
                "zip",
                directory_path,
            )

            return self.upload_file(
                session_id=session_id,
                resource_label=resource_label,
                file_path=zip_path,
                scan_id=scan_id,
                project=project,
                extract=True,
                overwrite=overwrite,
                parent=parent,
            )
