"""Tests for the background archive poller."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.poller import ArchivePoller, DeferredExperiment

FAST_POLL = 0.05
WAIT_TIMEOUT = 2.0


@pytest.fixture
def mock_executor() -> MagicMock:
    """Create a mock TransferExecutor with safe defaults."""
    executor = MagicMock(spec=TransferExecutor)
    executor.list_prearchive_entries.return_value = []
    executor.count_dest_scans.return_value = 0
    return executor


def _make_entity(label: str = "EXP001") -> DiscoveredEntity:
    """Create a minimal DiscoveredEntity for testing."""
    return DiscoveredEntity(
        local_id=f"XNAT_E_{label}",
        local_label=label,
        change_type=ChangeType.NEW,
    )


def _make_deferred(
    label: str = "EXP001",
    dest_project: str = "DST",
    dicom_scan_count: int = 3,
    timeout_at: float | None = None,
) -> DeferredExperiment:
    """Create a DeferredExperiment for testing.

    Args:
        label: Experiment label.
        dest_project: Destination project ID.
        dicom_scan_count: Number of DICOM scans expected.
        timeout_at: Monotonic deadline; defaults to far future.

    Returns:
        A DeferredExperiment with real threading.Event objects.
    """
    return DeferredExperiment(
        exp=_make_entity(label),
        subject=_make_entity(f"SUB_{label}"),
        scans=[],
        scan_resources_cache={},
        dicom_scan_count=dicom_scan_count,
        sync_id=1,
        dest_project=dest_project,
        work_dir=Path("/tmp/test"),
        work_dir_handle=MagicMock(spec=tempfile.TemporaryDirectory),
        archive_timeout_at=timeout_at if timeout_at is not None else time.monotonic() + 300,
    )


class TestArchivePoller:
    """Tests for ArchivePoller background polling behaviour."""

    def test_poller_detects_prearchive_ready(self, mock_executor: MagicMock) -> None:
        """needs_archive_action is set when snapshot has status=READY."""
        mock_executor.list_prearchive_entries.return_value = [
            {"name": "EXP001", "folderName": "EXP001", "status": "READY"},
        ]
        item = _make_deferred()
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            assert item.needs_archive_action.wait(timeout=WAIT_TIMEOUT)
        finally:
            poller.stop()

    def test_poller_detects_prearchive_conflict(self, mock_executor: MagicMock) -> None:
        """needs_archive_action is set when snapshot has status=CONFLICT."""
        mock_executor.list_prearchive_entries.return_value = [
            {"name": "EXP001", "folderName": "EXP001", "status": "CONFLICT"},
        ]
        item = _make_deferred()
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            assert item.needs_archive_action.wait(timeout=WAIT_TIMEOUT)
        finally:
            poller.stop()

    def test_poller_clears_on_no_prearchive(self, mock_executor: MagicMock) -> None:
        """prearchive_cleared is set when snapshot has no matching entry."""
        mock_executor.list_prearchive_entries.return_value = []
        item = _make_deferred()
        assert not item.prearchive_cleared
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            deadline = time.monotonic() + WAIT_TIMEOUT
            while not item.prearchive_cleared and time.monotonic() < deadline:
                time.sleep(FAST_POLL)
            assert item.prearchive_cleared
        finally:
            poller.stop()

    def test_poller_sets_archive_ready_on_scan_count(self, mock_executor: MagicMock) -> None:
        """archive_ready is set when count_dest_scans >= expected."""
        mock_executor.list_prearchive_entries.return_value = []
        mock_executor.count_dest_scans.return_value = 3
        item = _make_deferred(dicom_scan_count=3)
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            assert item.archive_ready.wait(timeout=WAIT_TIMEOUT)
        finally:
            poller.stop()

    def test_poller_timeout_unblocks(self, mock_executor: MagicMock) -> None:
        """archive_ready is set after archive_timeout_at deadline."""
        mock_executor.list_prearchive_entries.return_value = []
        mock_executor.count_dest_scans.return_value = 0
        item = _make_deferred(timeout_at=time.monotonic() - 1)
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            assert item.archive_ready.wait(timeout=WAIT_TIMEOUT)
        finally:
            poller.stop()

    def test_poller_removes_from_pending_on_ready(self, mock_executor: MagicMock) -> None:
        """pending_count decrements after archive completes."""
        mock_executor.list_prearchive_entries.return_value = []
        mock_executor.count_dest_scans.return_value = 5
        item = _make_deferred(dicom_scan_count=3)
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        assert poller.pending_count == 1
        poller.start()
        try:
            assert item.archive_ready.wait(timeout=WAIT_TIMEOUT)
            deadline = time.monotonic() + WAIT_TIMEOUT
            while poller.pending_count > 0 and time.monotonic() < deadline:
                time.sleep(FAST_POLL)
            assert poller.pending_count == 0
        finally:
            poller.stop()

    def test_poller_handles_http_errors_gracefully(self, mock_executor: MagicMock) -> None:
        """count_dest_scans raising does not crash the poller; retries next cycle."""
        mock_executor.list_prearchive_entries.return_value = []
        mock_executor.count_dest_scans.side_effect = ConnectionError("network down")
        item = _make_deferred()
        item.prearchive_cleared = True
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            time.sleep(FAST_POLL * 5)
            assert poller.is_alive
            assert not item.archive_ready.is_set()
        finally:
            poller.stop()

    def test_poller_distinguishes_zero_vs_error(self, mock_executor: MagicMock) -> None:
        """count_dest_scans raising does not increment zero_scan_cycles; returning 0 does."""
        mock_executor.list_prearchive_entries.return_value = []

        call_count = 0

        def side_effect(*_args: object, **_kwargs: object) -> int:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("transient")
            return 0

        mock_executor.count_dest_scans.side_effect = side_effect
        item = _make_deferred()
        item.prearchive_cleared = True
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            deadline = time.monotonic() + WAIT_TIMEOUT
            while call_count < 4 and time.monotonic() < deadline:
                time.sleep(FAST_POLL)
            assert item.zero_scan_cycles >= 1
            assert item.zero_scan_cycles <= 2
        finally:
            poller.stop()

    def test_poller_stop_exits_cleanly(self, mock_executor: MagicMock) -> None:
        """Thread joins within poll_interval * 2."""
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.start()
        assert poller.is_alive
        start = time.monotonic()
        poller.stop()
        elapsed = time.monotonic() - start
        assert elapsed < FAST_POLL * 4
        assert not poller.is_alive

    def test_poller_zero_scan_recheck(self, mock_executor: MagicMock) -> None:
        """After 3 cycles of 0 scans, prearchive_cleared resets to False."""
        mock_executor.list_prearchive_entries.return_value = []
        mock_executor.count_dest_scans.return_value = 0
        item = _make_deferred()
        item.prearchive_cleared = True
        poller = ArchivePoller(mock_executor, poll_interval=FAST_POLL)
        poller.enqueue(item)
        poller.start()
        try:
            deadline = time.monotonic() + WAIT_TIMEOUT
            while item.prearchive_cleared and time.monotonic() < deadline:
                time.sleep(FAST_POLL)
            assert not item.prearchive_cleared
            assert item.zero_scan_cycles == 0
        finally:
            poller.stop()
