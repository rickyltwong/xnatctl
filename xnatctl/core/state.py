"""SQLite state store for transfer history and ID mappings.

Tracks sync runs, per-entity transfer status, and local-to-remote ID mappings
to enable incremental synchronization between XNAT instances.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class SyncStatus(str, Enum):
    """Overall sync run status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class EntityStatus(str, Enum):
    """Per-entity transfer status."""

    SYNCED = "synced"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    CONFLICT = "conflict"
    DELETED = "deleted"


class TransferStateStore:
    """SQLite-backed state store for project transfers.

    Args:
        db_path: Path to SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                source_project TEXT NOT NULL,
                dest_url TEXT NOT NULL,
                dest_project TEXT NOT NULL,
                sync_start TEXT NOT NULL,
                sync_end TEXT,
                status TEXT NOT NULL,
                subjects_synced INTEGER DEFAULT 0,
                subjects_failed INTEGER DEFAULT 0,
                subjects_skipped INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS entity_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_id INTEGER NOT NULL REFERENCES sync_history(id),
                entity_type TEXT NOT NULL,
                local_id TEXT NOT NULL,
                local_label TEXT NOT NULL,
                remote_id TEXT,
                remote_label TEXT,
                xsi_type TEXT,
                parent_local_id TEXT,
                status TEXT NOT NULL,
                message TEXT,
                file_count INTEGER,
                file_size INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS id_mapping (
                source_url TEXT NOT NULL,
                source_project TEXT NOT NULL,
                dest_url TEXT NOT NULL,
                dest_project TEXT NOT NULL,
                local_id TEXT NOT NULL,
                remote_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                PRIMARY KEY (
                    source_url, source_project, dest_url, dest_project, local_id
                )
            );
        """)
        self._conn.commit()

    def _get_tables(self) -> list[str]:
        """List table names in the database.

        Returns:
            Sorted list of table names.
        """
        cur = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # -- sync_history --------------------------------------------------------

    def start_sync(
        self,
        source_url: str,
        source_project: str,
        dest_url: str,
        dest_project: str,
    ) -> int:
        """Record the start of a sync run.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.
            dest_url: Destination XNAT URL.
            dest_project: Destination project ID.

        Returns:
            Sync run ID.
        """
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            """INSERT INTO sync_history
               (source_url, source_project, dest_url, dest_project,
                sync_start, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                source_url,
                source_project,
                dest_url,
                dest_project,
                now,
                SyncStatus.RUNNING.value,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def end_sync(
        self,
        sync_id: int,
        status: SyncStatus,
        subjects_synced: int = 0,
        subjects_failed: int = 0,
        subjects_skipped: int = 0,
    ) -> None:
        """Record the end of a sync run.

        Args:
            sync_id: Sync run ID.
            status: Final status.
            subjects_synced: Number of subjects synced.
            subjects_failed: Number of subjects failed.
            subjects_skipped: Number of subjects skipped.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """UPDATE sync_history
               SET sync_end=?, status=?, subjects_synced=?,
                   subjects_failed=?, subjects_skipped=?
               WHERE id=?""",
            (now, status.value, subjects_synced, subjects_failed, subjects_skipped, sync_id),
        )
        self._conn.commit()

    def get_sync_history(
        self,
        source_url: str,
        source_project: str,
    ) -> list[dict[str, Any]]:
        """Get sync history for a source project.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.

        Returns:
            List of sync history records, newest first.
        """
        cur = self._conn.execute(
            """SELECT * FROM sync_history
               WHERE source_url=? AND source_project=?
               ORDER BY sync_start DESC""",
            (source_url, source_project),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_last_sync_time(
        self,
        source_url: str,
        source_project: str,
        dest_url: str,
        dest_project: str,
    ) -> str | None:
        """Get the end time of the last successful sync.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.
            dest_url: Destination XNAT URL.
            dest_project: Destination project ID.

        Returns:
            ISO8601 timestamp or None if no previous sync.
        """
        cur = self._conn.execute(
            """SELECT sync_end FROM sync_history
               WHERE source_url=? AND source_project=?
                 AND dest_url=? AND dest_project=?
                 AND status=?
               ORDER BY sync_end DESC LIMIT 1""",
            (
                source_url,
                source_project,
                dest_url,
                dest_project,
                SyncStatus.COMPLETED.value,
            ),
        )
        row = cur.fetchone()
        return row["sync_end"] if row else None

    # -- entity_manifest -----------------------------------------------------

    def record_entity(
        self,
        sync_id: int,
        entity_type: str,
        local_id: str,
        local_label: str,
        status: EntityStatus,
        remote_id: str | None = None,
        remote_label: str | None = None,
        xsi_type: str | None = None,
        parent_local_id: str | None = None,
        message: str | None = None,
        file_count: int | None = None,
        file_size: int | None = None,
    ) -> int:
        """Record a transferred entity.

        Args:
            sync_id: Sync run ID.
            entity_type: Entity type (subject, experiment, scan, resource).
            local_id: Source entity ID.
            local_label: Source entity label.
            status: Transfer status.
            remote_id: Destination entity ID.
            remote_label: Destination entity label.
            xsi_type: XSI type.
            parent_local_id: Parent entity's local ID.
            message: Status message.
            file_count: Number of files.
            file_size: Total size in bytes.

        Returns:
            Entity manifest record ID.
        """
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            """INSERT INTO entity_manifest
               (sync_id, entity_type, local_id, local_label, remote_id,
                remote_label, xsi_type, parent_local_id, status,
                message, file_count, file_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sync_id,
                entity_type,
                local_id,
                local_label,
                remote_id,
                remote_label,
                xsi_type,
                parent_local_id,
                status.value,
                message,
                file_count,
                file_size,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_entities(
        self,
        sync_id: int,
        entity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get entity records for a sync run.

        Args:
            sync_id: Sync run ID.
            entity_type: Optional filter by entity type.

        Returns:
            List of entity manifest records.
        """
        if entity_type:
            cur = self._conn.execute(
                "SELECT * FROM entity_manifest WHERE sync_id=? AND entity_type=?",
                (sync_id, entity_type),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM entity_manifest WHERE sync_id=?",
                (sync_id,),
            )
        return [dict(row) for row in cur.fetchall()]

    # -- id_mapping ----------------------------------------------------------

    def save_id_mapping(
        self,
        source_url: str,
        source_project: str,
        dest_url: str,
        dest_project: str,
        local_id: str,
        remote_id: str,
        entity_type: str,
    ) -> None:
        """Save or update a local-to-remote ID mapping.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.
            dest_url: Destination XNAT URL.
            dest_project: Destination project ID.
            local_id: Source entity ID.
            remote_id: Destination entity ID.
            entity_type: Entity type.
        """
        self._conn.execute(
            """INSERT INTO id_mapping
               (source_url, source_project, dest_url, dest_project,
                local_id, remote_id, entity_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_url, source_project, dest_url,
                           dest_project, local_id)
               DO UPDATE SET remote_id=excluded.remote_id,
                             entity_type=excluded.entity_type""",
            (
                source_url,
                source_project,
                dest_url,
                dest_project,
                local_id,
                remote_id,
                entity_type,
            ),
        )
        self._conn.commit()

    def get_remote_id(
        self,
        source_url: str,
        source_project: str,
        dest_url: str,
        dest_project: str,
        local_id: str,
    ) -> str | None:
        """Look up a remote ID by local ID.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.
            dest_url: Destination XNAT URL.
            dest_project: Destination project ID.
            local_id: Source entity ID.

        Returns:
            Remote entity ID or None.
        """
        cur = self._conn.execute(
            """SELECT remote_id FROM id_mapping
               WHERE source_url=? AND source_project=?
                 AND dest_url=? AND dest_project=? AND local_id=?""",
            (source_url, source_project, dest_url, dest_project, local_id),
        )
        row = cur.fetchone()
        return row["remote_id"] if row else None

    def get_experiment_parents(self, experiment_local_ids: set[str]) -> set[str]:
        """Get parent subject IDs for given experiment IDs from entity_manifest.

        Args:
            experiment_local_ids: Set of experiment local (source) IDs.

        Returns:
            Set of parent subject local IDs.
        """
        if not experiment_local_ids:
            return set()
        placeholders = ",".join("?" for _ in experiment_local_ids)
        cur = self._conn.execute(
            f"""SELECT DISTINCT parent_local_id FROM entity_manifest
                WHERE entity_type='experiment' AND local_id IN ({placeholders})
                AND parent_local_id IS NOT NULL""",
            list(experiment_local_ids),
        )
        return {row["parent_local_id"] for row in cur.fetchall()}

    def get_all_mappings(
        self,
        source_url: str,
        source_project: str,
        dest_url: str,
        dest_project: str,
    ) -> list[dict[str, Any]]:
        """Get all ID mappings for a source-dest pair.

        Args:
            source_url: Source XNAT URL.
            source_project: Source project ID.
            dest_url: Destination XNAT URL.
            dest_project: Destination project ID.

        Returns:
            List of mapping records.
        """
        cur = self._conn.execute(
            """SELECT * FROM id_mapping
               WHERE source_url=? AND source_project=?
                 AND dest_url=? AND dest_project=?""",
            (source_url, source_project, dest_url, dest_project),
        )
        return [dict(row) for row in cur.fetchall()]
