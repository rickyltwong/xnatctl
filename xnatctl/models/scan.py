"""Scan model for XNAT scans."""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from .base import XNATResource


class Scan(XNATResource):
    """XNAT scan resource."""

    type: Optional[str] = Field(None, description="Scan type")
    series_description: Optional[str] = Field(
        None, alias="series_description", description="DICOM series description"
    )
    quality: Optional[str] = Field(None, description="Quality assessment")
    condition: Optional[str] = Field(None, description="Condition")
    note: Optional[str] = Field(None, description="Scan notes")
    frames: Optional[int] = Field(None, description="Number of frames")
    start_time: Optional[str] = Field(
        None, alias="startTime", description="Acquisition start time"
    )
    scanner: Optional[str] = Field(None, description="Scanner")
    modality: Optional[str] = Field(None, description="Modality")
    file_count: Optional[int] = Field(
        None, alias="file_count", description="Number of files"
    )
    file_size: Optional[int] = Field(
        None, alias="file_size", description="Total file size in bytes"
    )

    # Parent references
    session_id: Optional[str] = Field(None, description="Parent session ID")
    project: Optional[str] = Field(None, description="Parent project ID")

    @classmethod
    def table_columns(cls) -> list[str]:
        """Return columns for table output."""
        return ["id", "type", "series_description", "quality", "frames", "file_count"]

    def to_row(self, columns: list[str] | None = None) -> dict[str, str]:
        """Convert to row for table output."""
        cols = columns or self.table_columns()
        data = self.to_dict()
        return {col: str(data.get(col, "")) for col in cols}

    @property
    def file_size_mb(self) -> float:
        """Return file size in megabytes."""
        if self.file_size:
            return self.file_size / (1024 * 1024)
        return 0.0
