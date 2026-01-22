"""Resource model for XNAT resources (file collections)."""

from __future__ import annotations

from pydantic import Field

from .base import BaseModel, XNATResource


class ResourceFile(BaseModel):
    """Individual file within a resource."""

    name: str = Field(..., alias="Name", description="File name")
    size: int | None = Field(None, alias="Size", description="File size in bytes")
    uri: str | None = Field(None, alias="URI", description="Download URI")
    digest: str | None = Field(None, description="File checksum/digest")
    content_type: str | None = Field(None, alias="content_type", description="MIME type")
    collection: str | None = Field(None, alias="collection", description="Parent resource label")

    @property
    def size_mb(self) -> float:
        """Return file size in megabytes."""
        if self.size:
            return self.size / (1024 * 1024)
        return 0.0


class Resource(XNATResource):
    """XNAT resource (file collection)."""

    format: str | None = Field(None, description="Resource format")
    content: str | None = Field(None, description="Content type/description")
    file_count: int | None = Field(None, alias="file_count", description="Number of files")
    file_size: int | None = Field(None, alias="file_size", description="Total size in bytes")
    category: str | None = Field(None, description="Resource category")
    cacheable: bool | None = Field(None, description="Whether resource is cacheable")

    # Parent references
    session_id: str | None = Field(None, description="Parent session ID")
    scan_id: str | None = Field(None, description="Parent scan ID (if scan-level)")
    project: str | None = Field(None, description="Parent project ID")

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return ["label", "format", "file_count", "file_size_display", "content"]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        data["file_size_display"] = self.file_size_display
        return {col: str(data.get(col, "")) for col in cols}

    @property
    def file_size_mb(self) -> float:
        """Return file size in megabytes."""
        if self.file_size:
            return self.file_size / (1024 * 1024)
        return 0.0

    @property
    def file_size_display(self) -> str:
        """Return human-readable file size."""
        if not self.file_size:
            return ""
        size = float(self.file_size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"
