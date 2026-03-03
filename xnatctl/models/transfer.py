"""Transfer configuration models.

Defines the YAML-based filter hierarchy for project transfers,
mirroring XSync's JSON configuration structure.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from xnatctl.models.base import BaseModel


class FilterSyncType(str, Enum):
    """Sync type for a filter level."""

    ALL = "all"
    NONE = "none"
    INCLUDE = "include"
    EXCLUDE = "exclude"


class ResourceFilter(BaseModel):
    """Filter for resource labels at any level."""

    sync_type: FilterSyncType = FilterSyncType.ALL
    items: list[str] = Field(default_factory=list)

    def should_include(self, label: str) -> bool:
        """Check if a resource label passes this filter.

        Args:
            label: Resource label to check.

        Returns:
            True if the label should be included.
        """
        if self.sync_type == FilterSyncType.ALL:
            return True
        if self.sync_type == FilterSyncType.NONE:
            return False
        if self.sync_type == FilterSyncType.INCLUDE:
            return label in self.items
        return label not in self.items


class ScanTypeFilter(BaseModel):
    """Filter for scan types within an imaging session type."""

    sync_type: FilterSyncType = FilterSyncType.ALL
    items: list[str] = Field(default_factory=list)

    def should_include(self, scan_type: str) -> bool:
        """Check if a scan type passes this filter.

        Args:
            scan_type: Scan type string to check.

        Returns:
            True if the scan type should be included.
        """
        if self.sync_type == FilterSyncType.ALL:
            return True
        if self.sync_type == FilterSyncType.NONE:
            return False
        if self.sync_type == FilterSyncType.INCLUDE:
            return scan_type in self.items
        return scan_type not in self.items


class XsiTypeFilter(BaseModel):
    """Filter config for a specific xsiType within imaging sessions."""

    xsi_type: str
    scan_types: ScanTypeFilter = Field(default_factory=ScanTypeFilter)
    scan_resources: ResourceFilter = Field(default_factory=ResourceFilter)
    resources: ResourceFilter = Field(default_factory=ResourceFilter)
    session_assessors: ResourceFilter = Field(default_factory=ResourceFilter)


class ImagingSessionFilter(BaseModel):
    """Filter for imaging sessions (experiments with scans)."""

    sync_type: FilterSyncType = FilterSyncType.ALL
    xsi_types: list[XsiTypeFilter] = Field(default_factory=list)

    def get_type_filter(self, xsi_type: str) -> XsiTypeFilter | None:
        """Get the filter config for a specific xsiType.

        Args:
            xsi_type: XSI type to look up.

        Returns:
            XsiTypeFilter if found, None otherwise.
        """
        for tf in self.xsi_types:
            if tf.xsi_type == xsi_type:
                return tf
        return None

    def should_include_type(self, xsi_type: str) -> bool:
        """Check if an xsiType passes this filter.

        Args:
            xsi_type: XSI type to check.

        Returns:
            True if the type should be included.
        """
        if self.sync_type == FilterSyncType.ALL:
            return True
        if self.sync_type == FilterSyncType.NONE:
            return False
        type_names = [t.xsi_type for t in self.xsi_types]
        if self.sync_type == FilterSyncType.INCLUDE:
            return xsi_type in type_names
        return xsi_type not in type_names


class AssessorFilter(BaseModel):
    """Filter for subject assessors."""

    sync_type: FilterSyncType = FilterSyncType.ALL
    xsi_types: list[XsiTypeFilter] = Field(default_factory=list)

    def should_include_type(self, xsi_type: str) -> bool:
        """Check if an assessor xsiType passes this filter.

        Args:
            xsi_type: XSI type to check.

        Returns:
            True if the type should be included.
        """
        if self.sync_type == FilterSyncType.ALL:
            return True
        if self.sync_type == FilterSyncType.NONE:
            return False
        type_names = [t.xsi_type for t in self.xsi_types]
        if self.sync_type == FilterSyncType.INCLUDE:
            return xsi_type in type_names
        return xsi_type not in type_names


class FilterConfig(BaseModel):
    """Top-level filter configuration mirroring XSync's hierarchy."""

    project_resources: ResourceFilter = Field(default_factory=ResourceFilter)
    subject_resources: ResourceFilter = Field(default_factory=ResourceFilter)
    subject_assessors: AssessorFilter = Field(default_factory=AssessorFilter)
    imaging_sessions: ImagingSessionFilter = Field(default_factory=ImagingSessionFilter)


class TransferConfig(BaseModel):
    """Transfer configuration loaded from YAML."""

    source_project: str
    dest_project: str
    sync_new_only: bool = True
    max_failures: int = 5
    scan_retry_count: int = 3
    scan_retry_delay: float = 5.0
    verify_after_transfer: bool = True
    scan_workers: int = Field(default=4, ge=1)
    filtering: FilterConfig = Field(default_factory=FilterConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> TransferConfig:
        """Load transfer config from a YAML file.

        Args:
            path: Path to the YAML config file.

        Returns:
            Parsed TransferConfig.

        Raises:
            TransferConfigError: If the file is invalid.
        """
        from xnatctl.core.exceptions import TransferConfigError

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            raise TransferConfigError(f"Failed to load {path}: {e}") from e

        try:
            return cls.model_validate(data)
        except Exception as e:
            raise TransferConfigError(f"Invalid transfer config: {e}") from e

    @staticmethod
    def scaffold(source_project: str, dest_project: str) -> str:
        """Generate a starter YAML config.

        Args:
            source_project: Source project ID.
            dest_project: Destination project ID.

        Returns:
            YAML string with default config.
        """
        data: dict[str, Any] = {
            "source_project": source_project,
            "dest_project": dest_project,
            "sync_new_only": True,
            "max_failures": 5,
            "scan_retry_count": 3,
            "scan_retry_delay": 5.0,
            "verify_after_transfer": True,
            "scan_workers": 4,
            "filtering": {
                "project_resources": {"sync_type": "all"},
                "subject_resources": {"sync_type": "all"},
                "subject_assessors": {"sync_type": "all"},
                "imaging_sessions": {
                    "sync_type": "all",
                    "xsi_types": [],
                },
            },
        }
        return yaml.dump(data, default_flow_style=False, sort_keys=False)
