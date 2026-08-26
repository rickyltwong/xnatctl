"""Tests for SQLite transfer state store."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from xnatctl.core.state import EntityStatus, SyncStatus, TransferStateStore


@pytest.fixture
def store(tmp_path) -> Iterator[TransferStateStore]:
    """Create a TransferStateStore backed by a temporary database."""
    db_path = tmp_path / "transfer.db"
    store = TransferStateStore(db_path)
    try:
        yield store
    finally:
        store.close()


class TestStateStoreInit:
    def test_creates_tables(self, store: TransferStateStore) -> None:
        tables = store._get_tables()
        assert "sync_history" in tables
        assert "entity_manifest" in tables
        assert "id_mapping" in tables

    def test_idempotent_init(self, tmp_path) -> None:
        db_path = tmp_path / "transfer.db"
        s1 = TransferStateStore(db_path)
        s2 = TransferStateStore(db_path)
        try:
            assert s1._get_tables() == s2._get_tables()
        finally:
            s1.close()
            s2.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits don't mean anything on Windows (see SECURITY.md)",
)
class TestStateStorePermissions:
    """The transfer database can hold subject/session labels and server URLs.

    Same secrets-adjacent treatment as the session cache and audit log:
    created 0600, not left at whatever the process umask would otherwise
    apply.
    """

    def test_db_file_is_private(self, store: TransferStateStore) -> None:
        mode = stat.S_IMODE(store.db_path.stat().st_mode)
        assert mode == 0o600

    def test_wal_and_shm_siblings_are_private(self, store: TransferStateStore) -> None:
        for suffix in ("-wal", "-shm"):
            sibling = store.db_path.with_name(store.db_path.name + suffix)
            assert sibling.exists(), f"expected WAL mode to have created {sibling}"
            assert stat.S_IMODE(sibling.stat().st_mode) == 0o600

    def test_a_pre_existing_loose_db_is_tightened(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transfer.db"
        db_path.write_bytes(b"")
        db_path.chmod(0o644)

        store = TransferStateStore(db_path)
        try:
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        finally:
            store.close()


class TestSyncHistory:
    def test_start_and_end_sync(self, store: TransferStateStore) -> None:
        sync_id = store.start_sync(
            source_url="https://src.example.org",
            source_project="SRC",
            dest_url="https://dst.example.org",
            dest_project="DST",
        )
        assert sync_id > 0

        store.end_sync(
            sync_id=sync_id,
            status=SyncStatus.COMPLETED,
            subjects_synced=5,
            subjects_failed=1,
            subjects_skipped=2,
        )

        history = store.get_sync_history("https://src.example.org", "SRC")
        assert len(history) == 1
        assert history[0]["status"] == "completed"
        assert history[0]["subjects_synced"] == 5

    def test_get_last_sync_time(self, store: TransferStateStore) -> None:
        sync_id = store.start_sync("https://s.org", "S", "https://d.org", "D")
        store.end_sync(sync_id, SyncStatus.COMPLETED)

        ts = store.get_last_sync_time("https://s.org", "S", "https://d.org", "D")
        assert ts is not None


class TestEntityManifest:
    def test_record_and_query_entity(self, store: TransferStateStore) -> None:
        sync_id = store.start_sync("https://s.org", "S", "https://d.org", "D")
        store.record_entity(
            sync_id=sync_id,
            entity_type="subject",
            local_id="XNAT_S001",
            local_label="SUB001",
            remote_id="XNAT_S999",
            remote_label="SUB001",
            status=EntityStatus.SYNCED,
        )

        entities = store.get_entities(sync_id)
        assert len(entities) == 1
        assert entities[0]["local_id"] == "XNAT_S001"
        assert entities[0]["status"] == "synced"


class TestIdMapping:
    def test_save_and_get_mapping(self, store: TransferStateStore) -> None:
        store.save_id_mapping(
            source_url="https://s.org",
            source_project="S",
            dest_url="https://d.org",
            dest_project="D",
            local_id="XNAT_S001",
            remote_id="XNAT_S999",
            entity_type="subject",
        )

        remote = store.get_remote_id(
            source_url="https://s.org",
            source_project="S",
            dest_url="https://d.org",
            dest_project="D",
            local_id="XNAT_S001",
        )
        assert remote == "XNAT_S999"

    def test_missing_mapping_returns_none(self, store: TransferStateStore) -> None:
        remote = store.get_remote_id("a", "b", "c", "d", "missing")
        assert remote is None

    def test_upsert_mapping(self, store: TransferStateStore) -> None:
        for remote in ("R1", "R2"):
            store.save_id_mapping("s", "S", "d", "D", "L1", remote, "subject")
        assert store.get_remote_id("s", "S", "d", "D", "L1") == "R2"

    def test_get_all_mappings(self, store: TransferStateStore) -> None:
        store.save_id_mapping("s", "S", "d", "D", "L1", "R1", "subject")
        store.save_id_mapping("s", "S", "d", "D", "L2", "R2", "subject")

        mappings = store.get_all_mappings("s", "S", "d", "D")
        assert len(mappings) == 2
