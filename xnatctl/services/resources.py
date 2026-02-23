"""Resource service for XNAT resource operations."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from pathlib import Path

from xnatctl.core.exceptions import ResourceNotFoundError
from xnatctl.models.resource import Resource, ResourceFile

from .base import BaseService


class ResourceService(BaseService):
    """Service for XNAT resource operations."""

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
        session_id: str,
        scan_id: str | None = None,
        project: str | None = None,
    ) -> builtins.list[Resource]:
        """List resources for a session or scan.

        Args:
            session_id: Session ID
            scan_id: Scan ID (for scan-level resources)
            project: Project ID

        Returns:
            List of Resource objects
        """
        if scan_id:
            if project:
                path = (
                    f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources"
                )
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources"
        else:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/resources"
            else:
                path = f"/data/experiments/{session_id}/resources"

        params: dict[str, str] = {"format": "json"}
        data = self._get(path, params=params)
        results = self._extract_results(data)

        resources = []
        for r in results:
            r = self._normalize_resource_row(r, session_id=session_id)
            r["session_id"] = session_id
            if scan_id:
                r["scan_id"] = scan_id
            if project:
                r["project"] = project
            resources.append(Resource.model_validate(r))

        return resources

    def get(
        self,
        session_id: str,
        resource_label: str,
        scan_id: str | None = None,
        project: str | None = None,
    ) -> Resource:
        """Get resource details.

        Args:
            session_id: Session ID
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID

        Returns:
            Resource object

        Raises:
            ResourceNotFoundError: If resource not found
        """
        resources = self.list(session_id, scan_id=scan_id, project=project)
        for resource in resources:
            if resource.label == resource_label:
                return resource

        raise ResourceNotFoundError("resource", resource_label)

    def list_files(
        self,
        session_id: str,
        resource_label: str,
        scan_id: str | None = None,
        project: str | None = None,
    ) -> builtins.list[ResourceFile]:
        """List files in a resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID

        Returns:
            List of ResourceFile objects
        """
        if scan_id:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files"
        else:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}/files"
            else:
                path = f"/data/experiments/{session_id}/resources/{resource_label}/files"

        params: dict[str, str] = {"format": "json"}
        data = self._get(path, params=params)
        results = self._extract_results(data)

        return [ResourceFile(**r) for r in results]

    def create(
        self,
        session_id: str,
        resource_label: str,
        scan_id: str | None = None,
        project: str | None = None,
        format: str | None = None,
        content: str | None = None,
    ) -> Resource:
        """Create a new resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            format: Resource format
            content: Content type

        Returns:
            Created Resource object
        """
        if scan_id:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}"
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}"
        else:
            if project:
                path = (
                    f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}"
                )
            else:
                path = f"/data/experiments/{session_id}/resources/{resource_label}"

        params: dict[str, str] = {}
        if format:
            params["format"] = format
        if content:
            params["content"] = content

        self._put(path, params=params)
        return self.get(session_id, resource_label, scan_id=scan_id, project=project)

    def delete(
        self,
        session_id: str,
        resource_label: str,
        scan_id: str | None = None,
        project: str | None = None,
        remove_files: bool = True,
    ) -> bool:
        """Delete a resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            remove_files: Also remove files from filesystem

        Returns:
            True if successful
        """
        if scan_id:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}"
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}"
        else:
            if project:
                path = (
                    f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}"
                )
            else:
                path = f"/data/experiments/{session_id}/resources/{resource_label}"

        params: dict[str, str] = {}
        if remove_files:
            params["removeFiles"] = "true"

        return self._delete(path, params=params)

    def upload_file(
        self,
        session_id: str,
        resource_label: str,
        file_path: Path,
        scan_id: str | None = None,
        project: str | None = None,
        extract: bool = False,
        overwrite: bool = False,
    ) -> dict[str, object]:
        """Upload a file to a resource.

        Args:
            session_id: Session ID
            resource_label: Resource label
            file_path: Local file path
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            extract: Extract ZIP/TAR files after upload
            overwrite: Overwrite existing files

        Returns:
            Upload result dict
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if scan_id:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files/{file_path.name}"
            else:
                path = f"/data/experiments/{session_id}/scans/{scan_id}/resources/{resource_label}/files/{file_path.name}"
        else:
            if project:
                path = f"/data/projects/{project}/experiments/{session_id}/resources/{resource_label}/files/{file_path.name}"
            else:
                path = f"/data/experiments/{session_id}/resources/{resource_label}/files/{file_path.name}"

        params: dict[str, str] = {}
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
                data=f,
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
        session_id: str,
        resource_label: str,
        directory_path: Path,
        scan_id: str | None = None,
        project: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, object]:
        """Upload a directory to a resource (creates ZIP first).

        Args:
            session_id: Session ID
            resource_label: Resource label
            directory_path: Local directory path
            scan_id: Scan ID (for scan-level resources)
            project: Project ID
            overwrite: Overwrite existing files

        Returns:
            Upload result dict
        """
        import shutil
        import tempfile

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
            )
