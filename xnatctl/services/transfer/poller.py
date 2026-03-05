"""Background archive poller for the pipelined transfer pipeline.

Monitors XNAT prearchive and archive status for experiments that have
been uploaded but are still awaiting archive completion. Read-only: only
performs HTTP GET requests via the executor.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xnatctl.services.transfer.discovery import DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor

logger = logging.getLogger(__name__)


@dataclass
class DeferredExperiment:
    """Context for an experiment awaiting archive completion.

    Attributes:
        exp: Discovered experiment entity.
        subject: Parent subject entity.
        scans: Source scan dicts for this experiment.
        scan_resources_cache: Cached scan resource lists keyed by scan ID.
        dicom_scan_count: Number of DICOM scans expected in archive.
        sync_id: Current sync run ID.
        dest_project: Destination project ID.
        work_dir: Temporary working directory for this experiment.
        work_dir_handle: Explicit lifecycle handle for the temp directory.
        archive_ready: Event set by the poller when scans >= expected.
        needs_archive_action: Event set by the poller on READY/CONFLICT.
        prearchive_cleared: True once the experiment leaves prearchive.
        zero_scan_cycles: Consecutive poll cycles returning 0 scans.
        archive_timeout_at: Monotonic deadline for archive wait.
    """

    exp: DiscoveredEntity
    subject: DiscoveredEntity
    scans: list[dict[str, Any]]
    scan_resources_cache: dict[str, list[dict[str, Any]]]
    dicom_scan_count: int
    sync_id: int
    dest_project: str
    work_dir: Path
    work_dir_handle: tempfile.TemporaryDirectory[str]
    archive_ready: threading.Event = field(default_factory=threading.Event)
    needs_archive_action: threading.Event = field(default_factory=threading.Event)
    prearchive_cleared: bool = False
    zero_scan_cycles: int = 0
    archive_timeout_at: float = 0.0


class ArchivePoller:
    """Background thread that polls archive status for pending experiments.

    Read-only: only performs GET requests via the executor.

    Args:
        executor: TransferExecutor used for HTTP GET calls.
        poll_interval: Seconds between poll cycles.
    """

    def __init__(self, executor: TransferExecutor, poll_interval: float = 5.0) -> None:
        self._executor = executor
        self._poll_interval = poll_interval
        self._pending: deque[DeferredExperiment] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background polling thread."""
        if self.is_alive:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="archive-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the poller to stop and wait for thread exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval * 2)
            if not self._thread.is_alive():
                self._thread = None
            else:
                logger.warning("Archive poller thread did not exit within timeout")

    def enqueue(self, item: DeferredExperiment) -> None:
        """Thread-safe add of a deferred experiment to the pending queue.

        Args:
            item: DeferredExperiment to monitor.
        """
        with self._lock:
            self._pending.append(item)

    @property
    def pending_count(self) -> int:
        """Thread-safe count of pending items."""
        with self._lock:
            return len(self._pending)

    @property
    def is_alive(self) -> bool:
        """Check if the polling thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def _poll_loop(self) -> None:
        """Main poll loop with top-level exception guard."""
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    snapshot_items = list(self._pending)

                if not snapshot_items:
                    self._stop_event.wait(timeout=self._poll_interval)
                    continue

                prearchive_snapshot = self._fetch_prearchive_snapshot(snapshot_items)

                for item in snapshot_items:
                    if item.archive_ready.is_set():
                        continue

                    if time.monotonic() >= item.archive_timeout_at:
                        item.archive_ready.set()
                        with self._lock:
                            try:
                                self._pending.remove(item)
                            except ValueError:
                                pass
                        continue

                    if not item.prearchive_cleared:
                        self._poll_prearchive(item, prearchive_snapshot)
                    else:
                        self._poll_scan_count(item)

                self._stop_event.wait(timeout=self._poll_interval)
        except Exception:
            logger.error("Archive poller crashed", exc_info=True)

    def _fetch_prearchive_snapshot(
        self,
        items: list[DeferredExperiment],
    ) -> dict[tuple[str, str], dict[str, Any]] | None:
        """Fetch prearchive entries for all unique destination projects.

        Args:
            items: Current snapshot of pending items.

        Returns:
            Dict keyed by (project, name/folderName) to entry dict, or None on error.
        """
        projects = {item.dest_project for item in items if not item.prearchive_cleared}
        if not projects:
            return {}

        result: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            for project in projects:
                entries = self._executor.list_prearchive_entries(project)
                for entry in entries:
                    name = entry.get("name", "")
                    folder_name = entry.get("folderName", "")
                    if name:
                        result[(project, name)] = entry
                    if folder_name:
                        result[(project, folder_name)] = entry
        except Exception:
            logger.error("Failed to fetch prearchive snapshot", exc_info=True)
            return None

        return result

    def _poll_prearchive(
        self,
        item: DeferredExperiment,
        snapshot: dict[tuple[str, str], dict[str, Any]] | None,
    ) -> None:
        """Check prearchive status for a single item.

        Args:
            item: DeferredExperiment to check.
            snapshot: Prearchive snapshot, or None if fetch failed.
        """
        if snapshot is None:
            return

        entry = snapshot.get((item.dest_project, item.exp.local_label))
        if entry is None:
            item.prearchive_cleared = True
            return

        status = entry.get("status", "")
        if status in ("READY", "CONFLICT"):
            item.needs_archive_action.set()

    def _poll_scan_count(self, item: DeferredExperiment) -> None:
        """Check archived scan count for a single item.

        Args:
            item: DeferredExperiment to check.
        """
        try:
            count = self._executor.count_dest_scans(
                item.dest_project,
                item.subject.local_label,
                item.exp.local_label,
            )
        except Exception:
            logger.debug(
                "count_dest_scans failed for %s, will retry",
                item.exp.local_label,
                exc_info=True,
            )
            return

        if count == 0:
            item.zero_scan_cycles += 1
            if item.zero_scan_cycles >= 3:
                item.prearchive_cleared = False
                item.zero_scan_cycles = 0
            return

        item.zero_scan_cycles = 0
        if count >= item.dicom_scan_count:
            item.archive_ready.set()
            with self._lock:
                try:
                    self._pending.remove(item)
                except ValueError:
                    pass
