"""Discovery service for identifying transferable entities on a source XNAT."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from xnatctl.services.base import BaseService


class ChangeType(enum.Enum):
    """Classification of an entity's change status relative to last sync."""

    NEW = "new"
    MODIFIED = "modified"
    DELETED = "deleted"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class DiscoveredEntity:
    """An XNAT entity discovered during project scanning.

    Attributes:
        local_id: XNAT accession ID on the source server
        local_label: Human-readable label on the source server
        change_type: How this entity changed since last sync
        xsi_type: XSI data type (e.g. xnat:mrSessionData), if applicable
        parent_id: Accession ID of the parent entity, if applicable
        insert_date: When the entity was first created
        last_modified: When the entity was last modified
    """

    local_id: str
    local_label: str
    change_type: ChangeType
    xsi_type: str | None = field(default=None)
    parent_id: str | None = field(default=None)
    insert_date: datetime | None = field(default=None)
    last_modified: datetime | None = field(default=None)


def _parse_xnat_timestamp(ts: str) -> datetime:
    """Parse an XNAT timestamp string into a timezone-aware datetime.

    Args:
        ts: Timestamp string in XNAT format (e.g. '2026-01-01 10:00:00.0')

    Returns:
        Timezone-aware UTC datetime
    """
    cleaned = ts.strip().rstrip("0").rstrip(".")
    dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=UTC)


def _classify_change(
    insert_date: datetime,
    last_modified: datetime,
    cutoff: datetime | None,
) -> ChangeType | None:
    """Classify an entity's change type relative to a sync cutoff.

    Args:
        insert_date: When the entity was created
        last_modified: When the entity was last modified
        cutoff: Last sync timestamp, or None for full sync

    Returns:
        ChangeType, or None if unchanged since cutoff
    """
    if cutoff is None:
        return ChangeType.NEW
    if insert_date > cutoff:
        return ChangeType.NEW
    if last_modified > cutoff:
        return ChangeType.MODIFIED
    return None


class DiscoveryService(BaseService):
    """Discovers subjects and experiments on a source XNAT project."""

    def discover_subjects(
        self,
        project: str,
        last_sync_time: str | None = None,
    ) -> list[DiscoveredEntity]:
        """Discover subjects in a project, classifying changes since last sync.

        Args:
            project: Source project ID
            last_sync_time: ISO 8601 timestamp of last sync, or None for full discovery

        Returns:
            List of discovered subject entities
        """
        cutoff = datetime.fromisoformat(last_sync_time) if last_sync_time else None
        data = self._get(f"/data/projects/{project}/subjects")
        results = self._extract_results(data)

        entities: list[DiscoveredEntity] = []
        for row in results:
            if row.get("project") != project:
                continue

            insert_dt = _parse_xnat_timestamp(row["insert_date"])
            modified_dt = _parse_xnat_timestamp(row["last_modified"])
            change = _classify_change(insert_dt, modified_dt, cutoff)
            if change is None:
                continue

            entities.append(
                DiscoveredEntity(
                    local_id=row["ID"],
                    local_label=row.get("label", ""),
                    change_type=change,
                    insert_date=insert_dt,
                    last_modified=modified_dt,
                )
            )

        return entities

    def discover_experiments(
        self,
        project: str,
        subject_id: str,
        last_sync_time: str | None = None,
    ) -> list[DiscoveredEntity]:
        """Discover experiments for a subject, classifying changes since last sync.

        Args:
            project: Source project ID
            subject_id: Source subject accession ID
            last_sync_time: ISO 8601 timestamp of last sync, or None for full discovery

        Returns:
            List of discovered experiment entities
        """
        cutoff = datetime.fromisoformat(last_sync_time) if last_sync_time else None
        data = self._get(f"/data/projects/{project}/subjects/{subject_id}/experiments")
        results = self._extract_results(data)

        entities: list[DiscoveredEntity] = []
        for row in results:
            if row.get("project") != project:
                continue

            insert_dt = _parse_xnat_timestamp(row["insert_date"])
            modified_dt = _parse_xnat_timestamp(row["last_modified"])
            change = _classify_change(insert_dt, modified_dt, cutoff)
            if change is None:
                continue

            entities.append(
                DiscoveredEntity(
                    local_id=row["ID"],
                    local_label=row.get("label", ""),
                    change_type=change,
                    xsi_type=row.get("xsiType"),
                    parent_id=subject_id,
                    insert_date=insert_dt,
                    last_modified=modified_dt,
                )
            )

        return entities
