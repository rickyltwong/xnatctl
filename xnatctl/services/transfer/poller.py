"""Background archive poller for the pipelined transfer pipeline.

Monitors XNAT prearchive and archive status for experiments that have
been uploaded but are still awaiting archive completion. The ``ArchivePoller``
itself is read-only: it only performs HTTP GET requests via the executor.

The module-level ``drain_*``/``service_prearchive_actions`` functions below
are the mutating counterpart: they run on the main thread and resolve
READY/CONFLICT prearchive entries the poller has flagged, then hand
completed experiments to a caller-supplied ``finalize`` callback.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xnatctl.core.state import EntityStatus, TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.discovery import DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor

if TYPE_CHECKING:
    from xnatctl.services.transfer.orchestrator import TransferResult

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
    archive_retries: int = 0
    archive_error: str | None = None
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
        except Exception:  # noqa: BLE001  # top-level guard for the background poll thread (see docstring)
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
        except Exception:  # noqa: BLE001  # per-cycle isolation: prearchive snapshot fetch failure returns None
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
        except Exception:  # noqa: BLE001  # per-cycle isolation: scan-count query failure retried next poll cycle
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


def service_prearchive_actions(
    executor: TransferExecutor,
    deferred: deque[DeferredExperiment],
) -> None:
    """Resolve prearchive READY/CONFLICT for any signaled experiments.

    Called on main thread. Performs mutating POST via archive_prearchive().

    Args:
        executor: TransferExecutor used for prearchive HTTP calls.
        deferred: Queue of deferred experiments to check.
    """
    for ctx in deferred:
        if not ctx.needs_archive_action.is_set():
            continue

        try:
            entry = executor.find_prearchive_entry(ctx.dest_project, ctx.exp.local_label)
        except Exception:  # noqa: BLE001  # per-item isolation: one experiment's prearchive resolution failure must not block others
            ctx.archive_retries += 1
            logger.warning(
                "Prearchive resolution failed for %s (attempt %d), will retry",
                ctx.exp.local_label,
                ctx.archive_retries,
                exc_info=True,
            )
            continue

        ctx.archive_retries = 0
        if entry is None:
            ctx.prearchive_cleared = True
            ctx.needs_archive_action.clear()
        else:
            timestamp = entry.get("timestamp", "")
            status = entry.get("status", "")
            if timestamp and status in ("READY", "CONFLICT"):
                overwrite = "append" if status == "CONFLICT" else None
                folder = entry.get("folderName") or entry.get("name", ctx.exp.local_label)
                try:
                    executor.archive_prearchive(
                        dest_project=ctx.dest_project,
                        timestamp=timestamp,
                        session_name=folder,
                        subject_label=ctx.subject.local_label,
                        experiment_label=ctx.exp.local_label,
                        overwrite=overwrite,
                    )
                except Exception as exc:  # noqa: BLE001  # per-item isolation: archive failure recorded on ctx.archive_error, not swallowed
                    ctx.archive_error = (
                        f"Prearchive archive failed for {ctx.exp.local_label}: {exc}"
                    )
                    ctx.archive_ready.set()
                    logger.error(
                        "Prearchive archive failed for %s",
                        ctx.exp.local_label,
                        exc_info=True,
                    )
                else:
                    ctx.prearchive_cleared = True
                finally:
                    ctx.needs_archive_action.clear()
            else:
                ctx.needs_archive_action.clear()


def _record_experiment_failure(
    state_store: TransferStateStore,
    ctx: DeferredExperiment,
    result: TransferResult,
    message: str,
) -> None:
    """Record a failed experiment on both the result and the state store.

    Args:
        state_store: Transfer state store.
        ctx: Deferred experiment that failed.
        result: Mutable result to update.
        message: Failure message.
    """
    result.experiments_failed += 1
    result.success = False
    result.errors.append(f"Experiment {ctx.exp.local_label}: {message}")
    state_store.record_entity(
        sync_id=ctx.sync_id,
        entity_type="experiment",
        local_id=ctx.exp.local_id,
        local_label=ctx.exp.local_label,
        xsi_type=ctx.exp.xsi_type,
        parent_local_id=ctx.subject.local_id,
        status=EntityStatus.FAILED,
        message=message,
    )


def drain_ready(
    state_store: TransferStateStore,
    finalize: Callable[[DeferredExperiment, TransferResult, Callable[[str], None] | None], None],
    deferred: deque[DeferredExperiment],
    result: TransferResult,
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """Finalize any experiment whose archive is ready.

    Scans the entire deferred queue (not just head) to avoid
    head-of-line blocking when a later experiment archives first.

    Args:
        state_store: Transfer state store, for recording failures.
        finalize: Callback that completes the deferred phases for one
            experiment (XML overlay, non-DICOM resources, verification).
        deferred: Queue of deferred experiments.
        result: Mutable result to update.
        progress_callback: Optional progress callback.

    Returns:
        True if at least one experiment was drained.
    """
    drained = False
    remaining: deque[DeferredExperiment] = deque()
    ready_items: list[DeferredExperiment] = []
    for ctx in deferred:
        if ctx.archive_ready.is_set():
            ready_items.append(ctx)
        else:
            remaining.append(ctx)
    deferred.clear()
    deferred.extend(remaining)

    for ctx in ready_items:
        if ctx.archive_error is not None:
            _record_experiment_failure(state_store, ctx, result, ctx.archive_error)
            ctx.work_dir_handle.cleanup()
            drained = True
            continue
        try:
            finalize(ctx, result, progress_callback)
        except Exception as e:  # noqa: BLE001  # per-experiment isolation: finalize() failure recorded via _record_experiment_failure, drain continues
            _record_experiment_failure(state_store, ctx, result, str(e))
        drained = True
    return drained


def _resolve_deferred_prearchive_blocking(
    executor: TransferExecutor,
    ctx: DeferredExperiment,
) -> None:
    """Resolve one item's pending READY/CONFLICT prearchive action, synchronously.

    Mutates ``ctx.prearchive_cleared``/``ctx.archive_error`` in place; any
    failure to even look up the prearchive entry is logged and left for the
    ``wait_for_archive`` fallback in the caller.

    Args:
        executor: TransferExecutor used for prearchive/archive HTTP calls.
        ctx: Deferred experiment with ``needs_archive_action`` set.
    """
    try:
        entry = executor.find_prearchive_entry(ctx.dest_project, ctx.exp.local_label)
        if entry is None:
            ctx.prearchive_cleared = True
        elif entry.get("status") in ("READY", "CONFLICT"):
            overwrite = "append" if entry["status"] == "CONFLICT" else None
            folder = entry.get("folderName") or entry.get("name", ctx.exp.local_label)
            try:
                executor.archive_prearchive(
                    dest_project=ctx.dest_project,
                    timestamp=entry.get("timestamp", ""),
                    session_name=folder,
                    subject_label=ctx.subject.local_label,
                    experiment_label=ctx.exp.local_label,
                    overwrite=overwrite,
                )
                ctx.prearchive_cleared = True
            except Exception as exc:  # noqa: BLE001  # per-item isolation, blocking-drain fallback: archive failure recorded on ctx.archive_error
                ctx.archive_error = f"Prearchive archive failed for {ctx.exp.local_label}: {exc}"
                logger.error(
                    "Prearchive archive failed for %s in blocking drain",
                    ctx.exp.local_label,
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001  # documented fallback: prearchive lookup failure logged, left for wait_for_archive fallback
        logger.warning(
            "Prearchive resolution failed for %s in blocking drain, "
            "falling back to wait_for_archive",
            ctx.exp.local_label,
            exc_info=True,
        )
    ctx.needs_archive_action.clear()


def drain_all_blocking(
    executor: TransferExecutor,
    state_store: TransferStateStore,
    finalize: Callable[[DeferredExperiment, TransferResult, Callable[[str], None] | None], None],
    deferred: deque[DeferredExperiment],
    result: TransferResult,
    config: TransferConfig,
    progress_callback: Callable[[str], None] | None = None,
) -> None:
    """Drain all deferred experiments using blocking wait.

    Fallback when the poller thread has died. First drains any
    already-ready items, then blocks on each remaining experiment's
    archive_ready event with a short timeout before falling back to
    the synchronous wait_for_archive().

    Args:
        executor: TransferExecutor used for prearchive/archive HTTP calls.
        state_store: Transfer state store, for recording failures.
        finalize: Callback that completes the deferred phases for one
            experiment (XML overlay, non-DICOM resources, verification).
        deferred: Queue of deferred experiments.
        result: Mutable result to update.
        config: Transfer configuration (archive wait timeout/interval).
        progress_callback: Optional progress callback.
    """
    # First drain anything already ready
    drain_ready(state_store, finalize, deferred, result, progress_callback)

    while deferred:
        ctx = deferred.popleft()
        # Service prearchive action if needed
        if ctx.needs_archive_action.is_set():
            _resolve_deferred_prearchive_blocking(executor, ctx)

        if ctx.archive_error is not None:
            _record_experiment_failure(state_store, ctx, result, ctx.archive_error)
            ctx.work_dir_handle.cleanup()
            continue

        # Block until ready or use wait_for_archive as fallback
        if not ctx.archive_ready.is_set():
            if progress_callback:
                progress_callback(f"    Blocking wait for {ctx.exp.local_label}...")
            try:
                executor.wait_for_archive(
                    ctx.dest_project,
                    ctx.subject.local_label,
                    ctx.exp.local_label,
                    ctx.dicom_scan_count,
                    timeout=config.archive_wait_timeout,
                    interval=config.archive_poll_interval,
                )
            except Exception as exc:  # noqa: BLE001  # per-item isolation: wait_for_archive failure recorded on ctx.archive_error
                ctx.archive_error = f"Archive wait failed for {ctx.exp.local_label}: {exc}"
                logger.error(
                    "Archive wait failed for %s in blocking drain",
                    ctx.exp.local_label,
                    exc_info=True,
                )

        if ctx.archive_error is not None:
            _record_experiment_failure(state_store, ctx, result, ctx.archive_error)
            ctx.work_dir_handle.cleanup()
            continue

        try:
            finalize(ctx, result, progress_callback)
        except Exception as e:  # noqa: BLE001  # per-experiment isolation: finalize() failure recorded via _record_experiment_failure
            _record_experiment_failure(state_store, ctx, result, str(e))
