"""Filter engine for transfer configuration.

Applies the YAML filter hierarchy to determine which entities
should be included in a transfer operation.
"""

from __future__ import annotations

from xnatctl.models.transfer import FilterConfig, FilterSyncType
from xnatctl.services.transfer.discovery import DiscoveredEntity


class FilterEngine:
    """Evaluates transfer filter rules against discovered entities.

    Args:
        config: Filter configuration from YAML.
    """

    def __init__(self, config: FilterConfig) -> None:
        self.config = config

    def should_include_experiment(self, entity: DiscoveredEntity) -> bool:
        """Check if an experiment should be transferred.

        Args:
            entity: Discovered experiment entity.

        Returns:
            True if the experiment passes the filter.
        """
        xsi_type = entity.xsi_type or ""
        return self.config.imaging_sessions.should_include_type(xsi_type)

    def should_include_scan(self, session_xsi_type: str, scan_type: str) -> bool:
        """Check if a scan should be transferred.

        Args:
            session_xsi_type: Parent session's XSI type.
            scan_type: Scan type string.

        Returns:
            True if the scan passes the filter.
        """
        sessions = self.config.imaging_sessions
        if sessions.sync_type == FilterSyncType.NONE:
            return False
        if sessions.sync_type == FilterSyncType.ALL:
            return True

        type_filter = sessions.get_type_filter(session_xsi_type)
        if type_filter is None:
            return False

        return type_filter.scan_types.should_include(scan_type)

    def should_include_scan_resource(self, session_xsi_type: str, resource_label: str) -> bool:
        """Check if a scan resource should be transferred.

        Args:
            session_xsi_type: Parent session's XSI type.
            resource_label: Resource label.

        Returns:
            True if the resource passes the filter.
        """
        sessions = self.config.imaging_sessions
        if sessions.sync_type == FilterSyncType.ALL:
            return True
        if sessions.sync_type == FilterSyncType.NONE:
            return False

        type_filter = sessions.get_type_filter(session_xsi_type)
        if type_filter is None:
            return False

        return type_filter.scan_resources.should_include(resource_label)

    def should_include_session_resource(self, session_xsi_type: str, resource_label: str) -> bool:
        """Check if a session-level resource should be transferred.

        Args:
            session_xsi_type: Parent session's XSI type.
            resource_label: Resource label.

        Returns:
            True if the resource passes the filter.
        """
        sessions = self.config.imaging_sessions
        if sessions.sync_type == FilterSyncType.ALL:
            return True
        if sessions.sync_type == FilterSyncType.NONE:
            return False

        type_filter = sessions.get_type_filter(session_xsi_type)
        if type_filter is None:
            return False

        return type_filter.resources.should_include(resource_label)

    def should_include_project_resource(self, resource_label: str) -> bool:
        """Check if a project-level resource should be transferred.

        Args:
            resource_label: Resource label.

        Returns:
            True if the resource passes the filter.
        """
        return self.config.project_resources.should_include(resource_label)

    def should_include_subject_resource(self, resource_label: str) -> bool:
        """Check if a subject-level resource should be transferred.

        Args:
            resource_label: Resource label.

        Returns:
            True if the resource passes the filter.
        """
        return self.config.subject_resources.should_include(resource_label)

    def should_include_assessor(self, xsi_type: str) -> bool:
        """Check if a subject assessor should be transferred.

        Args:
            xsi_type: Assessor XSI type.

        Returns:
            True if the assessor passes the filter.
        """
        return self.config.subject_assessors.should_include_type(xsi_type)
