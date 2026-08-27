"""Subject/experiment transfer pipeline with DICOM archive-wait overlap.

Owns the per-subject and per-experiment transfer orchestration: archive
pipelining (overlapping the archive wait for one experiment with the DICOM
upload of the next), XML metadata overlay, session resource transfer, and
post-transfer verification. Per-scan download/upload mechanics are delegated
to :class:`~xnatctl.services.transfer.scan_transfer.ScanTransfer`.

:class:`~xnatctl.services.transfer.orchestrator.TransferOrchestrator` owns
discovery, filtering, and reconciliation of *which* subjects to transfer and
calls :meth:`ScanPipeline.transfer_subject` per subject; the archive-wait
polling and draining machinery lives in
:mod:`xnatctl.services.transfer.poller`.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xnatctl.core.state import EntityStatus, TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.conflicts import ConflictChecker
from xnatctl.services.transfer.discovery import DiscoveredEntity, DiscoveryService
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
from xnatctl.services.transfer.poller import (
    ArchivePoller,
    DeferredExperiment,
    drain_all_blocking,
    drain_ready,
    service_prearchive_actions,
)
from xnatctl.services.transfer.scan_pipeline_finalize import _ExperimentFinalizeMixin
from xnatctl.services.transfer.scan_transfer import ScanTransfer
from xnatctl.services.transfer.verifier import Verifier

if TYPE_CHECKING:
    from xnatctl.services.transfer.orchestrator import TransferResult

logger = logging.getLogger(__name__)


class ScanPipeline(_ExperimentFinalizeMixin):
    """Transfers one subject's experiments, scans, and resources.

    Args:
        executor: Shared TransferExecutor for HTTP operations.
        filter_engine: Shared FilterEngine for inclusion decisions.
        conflict_checker: Shared ConflictChecker for subject conflicts.
        discovery: Shared DiscoveryService, used to list a subject's
            experiments.
        state_store: SQLite state store.
        verifier: Shared Verifier for post-transfer verification.
        config: Transfer configuration.
    """

    def __init__(
        self,
        executor: TransferExecutor,
        filter_engine: FilterEngine,
        conflict_checker: ConflictChecker,
        discovery: DiscoveryService,
        state_store: TransferStateStore,
        verifier: Verifier,
        config: TransferConfig,
    ) -> None:
        self.executor = executor
        self.filter_engine = filter_engine
        self.conflict_checker = conflict_checker
        self.discovery = discovery
        self.state_store = state_store
        self.verifier = verifier
        self.config = config
        self.scan_transfer = ScanTransfer(executor, filter_engine, config)

    def transfer_subject(
        self,
        subject: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Transfer a single subject and its experiments.

        Args:
            subject: Discovered subject entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        if progress_callback:
            progress_callback(f"Transferring subject {subject.local_label}...")

        if self._resolve_or_skip_conflicting_subject(subject, sync_id, dest_project, result):
            return

        self._create_and_record_subject(subject, sync_id, dest_project)

        experiments = self._discover_filtered_experiments(subject)
        if not experiments:
            return

        poller = ArchivePoller(self.executor, self.config.archive_poll_interval)
        poller.start()
        deferred: deque[DeferredExperiment] = deque()

        try:
            pipelining_disabled = self._dispatch_experiments(
                experiments,
                poller,
                deferred,
                subject,
                sync_id,
                dest_project,
                result,
                progress_callback,
            )
            self._drain_remaining_after_dispatch(
                poller, deferred, pipelining_disabled, result, progress_callback
            )
        finally:
            poller.stop()
            # Clean up any remaining temp directories
            for ctx in deferred:
                try:
                    ctx.work_dir_handle.cleanup()
                except Exception:  # noqa: BLE001  # best-effort cleanup: temp work_dir removal must not fail the transfer
                    pass

    def _resolve_or_skip_conflicting_subject(
        self,
        subject: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        result: TransferResult,
    ) -> bool:
        """Check a previously mapped subject for label conflicts on the destination.

        Args:
            subject: Discovered subject entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            result: Mutable result to update.

        Returns:
            True if the subject conflicts and must be skipped.
        """
        remote_id = self.state_store.get_remote_id(
            str(self.executor.source.base_url),
            self.config.source_project,
            str(self.executor.dest.base_url),
            dest_project,
            subject.local_id,
        )
        if not remote_id:
            return False

        conflict = self.conflict_checker.check_subject(remote_id, subject.local_label, dest_project)
        if not conflict.has_conflict:
            return False

        result.subjects_skipped += 1
        self.state_store.record_entity(
            sync_id=sync_id,
            entity_type="subject",
            local_id=subject.local_id,
            local_label=subject.local_label,
            remote_id=remote_id,
            status=EntityStatus.CONFLICT,
            message=conflict.reason,
        )
        return True

    def _create_and_record_subject(
        self,
        subject: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
    ) -> None:
        """Create the subject on the destination and record its ID mapping.

        Args:
            subject: Discovered subject entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
        """
        # Create subject and store ACTUAL remote ID from response
        remote_uri = self.executor.create_subject(dest_project, subject.local_label)
        actual_remote_id = remote_uri.split("/")[-1]

        self.state_store.save_id_mapping(
            str(self.executor.source.base_url),
            self.config.source_project,
            str(self.executor.dest.base_url),
            dest_project,
            subject.local_id,
            actual_remote_id,
            "subject",
        )

        self.state_store.record_entity(
            sync_id=sync_id,
            entity_type="subject",
            local_id=subject.local_id,
            local_label=subject.local_label,
            remote_id=actual_remote_id,
            status=EntityStatus.SYNCED,
        )

    def _discover_filtered_experiments(self, subject: DiscoveredEntity) -> list[DiscoveredEntity]:
        """List the subject's experiments that pass the filter engine.

        Args:
            subject: Discovered subject entity.

        Returns:
            Experiments to transfer, in discovery order.
        """
        all_experiments = self.discovery.discover_experiments(
            self.config.source_project,
            subject.local_id,
            last_sync_time=None,
        )
        return [e for e in all_experiments if self.filter_engine.should_include_experiment(e)]

    def _dispatch_experiments(
        self,
        experiments: list[DiscoveredEntity],
        poller: ArchivePoller,
        deferred: deque[DeferredExperiment],
        subject: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None,
    ) -> bool:
        """Upload each experiment's DICOM, pipelining archive waits via the poller.

        Args:
            experiments: Filtered experiments to transfer.
            poller: Background archive poller for this subject.
            deferred: Queue of deferred experiments (mutated in place).
            subject: Parent subject entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            result: Mutable result to update.
            progress_callback: Optional progress callback.

        Returns:
            The final pipelining_disabled flag, needed by the post-loop drain.
        """
        pipelining_disabled = False
        for exp in experiments:
            # Service prearchive actions from poller signals
            if not pipelining_disabled:
                service_prearchive_actions(self.executor, deferred)

            # Drain ready experiments before next upload
            drain_ready(
                self.state_store, self._finalize_experiment, deferred, result, progress_callback
            )

            # Throttle: block if too many pending archives
            pipelining_disabled = self._throttle_pending_archives(
                poller, deferred, pipelining_disabled, result, progress_callback
            )

            # Upload DICOM phase
            try:
                ctx = self._upload_dicom_phase(
                    exp, sync_id, dest_project, subject, result, progress_callback
                )
                if ctx is not None:
                    if pipelining_disabled:
                        # No poller: block on archive then finalize immediately
                        self.executor.wait_for_archive(
                            ctx.dest_project,
                            ctx.subject.local_label,
                            ctx.exp.local_label,
                            ctx.dicom_scan_count,
                            timeout=self.config.archive_wait_timeout,
                            interval=self.config.archive_poll_interval,
                        )
                        self._finalize_experiment(ctx, result, progress_callback)
                    else:
                        poller.enqueue(ctx)
                        deferred.append(ctx)
                else:
                    # No DICOM: finalize immediately
                    self._finalize_experiment_no_dicom(
                        exp, sync_id, dest_project, subject, result, progress_callback
                    )
            except Exception as e:  # noqa: BLE001  # per-experiment isolation: one experiment's failure must not abort the subject's dispatch loop
                result.experiments_failed += 1
                result.success = False
                result.errors.append(f"Experiment {exp.local_label}: {e}")
                self.state_store.record_entity(
                    sync_id=sync_id,
                    entity_type="experiment",
                    local_id=exp.local_id,
                    local_label=exp.local_label,
                    xsi_type=exp.xsi_type,
                    parent_local_id=subject.local_id,
                    status=EntityStatus.FAILED,
                    message=str(e),
                )
        return pipelining_disabled

    def _throttle_pending_archives(
        self,
        poller: ArchivePoller,
        deferred: deque[DeferredExperiment],
        pipelining_disabled: bool,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None,
    ) -> bool:
        """Block while too many archives are pending; disable pipelining if the poller dies.

        Args:
            poller: Background archive poller for this subject.
            deferred: Queue of deferred experiments.
            pipelining_disabled: Current pipelining-disabled flag.
            result: Mutable result to update.
            progress_callback: Optional progress callback.

        Returns:
            The (possibly updated) pipelining_disabled flag.
        """
        while not pipelining_disabled and len(deferred) >= self.config.max_pending_archives:
            service_prearchive_actions(self.executor, deferred)
            drained = drain_ready(
                self.state_store, self._finalize_experiment, deferred, result, progress_callback
            )
            if drained:
                break
            if not poller.is_alive:
                logger.warning("Poller died during throttle; draining with blocking wait")
                drain_all_blocking(
                    self.executor,
                    self.state_store,
                    self._finalize_experiment,
                    deferred,
                    result,
                    self.config,
                    progress_callback,
                )
                pipelining_disabled = True
                break
            time.sleep(0.5)
        return pipelining_disabled

    def _drain_remaining_after_dispatch(
        self,
        poller: ArchivePoller,
        deferred: deque[DeferredExperiment],
        pipelining_disabled: bool,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None,
    ) -> None:
        """Drain all remaining deferred experiments after the dispatch loop ends.

        Args:
            poller: Background archive poller for this subject.
            deferred: Queue of deferred experiments.
            pipelining_disabled: Whether pipelining was disabled mid-run.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        if pipelining_disabled:
            drain_all_blocking(
                self.executor,
                self.state_store,
                self._finalize_experiment,
                deferred,
                result,
                self.config,
                progress_callback,
            )
            return

        while deferred:
            service_prearchive_actions(self.executor, deferred)
            drain_ready(
                self.state_store, self._finalize_experiment, deferred, result, progress_callback
            )

            if not deferred:
                break

            if not poller.is_alive:
                logger.warning("Poller died; falling back to blocking wait")
                drain_all_blocking(
                    self.executor,
                    self.state_store,
                    self._finalize_experiment,
                    deferred,
                    result,
                    self.config,
                    progress_callback,
                )
                break

            # Wait briefly for any experiment to become ready
            deferred[0].archive_ready.wait(timeout=1.0)

    def _upload_dicom_phase(
        self,
        exp: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        subject: DiscoveredEntity,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DeferredExperiment | None:
        """Upload DICOM resources for an experiment (Phase 1).

        Returns DeferredExperiment context if DICOM was uploaded (needs archive
        wait), or None if experiment has no DICOM (should be finalized immediately).

        Args:
            exp: Discovered experiment entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            result: Mutable result to update.
            progress_callback: Optional progress callback.

        Returns:
            DeferredExperiment if DICOM uploaded, None otherwise.
        """
        if progress_callback:
            progress_callback(f"  Experiment {exp.local_label}...")

        work_dir_handle = tempfile.TemporaryDirectory()
        try:
            work_dir = Path(work_dir_handle.name)

            scans = self.executor.discover_scans(exp.local_id)
            scan_resources_cache: dict[str, list[dict[str, Any]]] = {}

            existing_id = self.executor.check_experiment_exists(dest_project, exp.local_label)
            if existing_id:
                logger.info(
                    "Experiment %s already exists on destination as %s; skipping DICOM upload",
                    exp.local_label,
                    existing_id,
                )
                work_dir_handle.cleanup()
                return None

            if not existing_id and self._scans_have_no_dicom(scans, exp, scan_resources_cache):
                self._create_experiment_shell(dest_project, subject, exp)
                work_dir_handle.cleanup()
                return None

            # Phase 1: Upload DICOM
            dicom_scan_count = self.scan_transfer.transfer_scans(
                scans,
                exp,
                dest_project,
                subject,
                work_dir,
                result,
                progress_callback,
                dicom_only=True,
                scan_resources_cache=scan_resources_cache,
            )

            if dicom_scan_count == 0:
                # All DICOM transfers failed or were skipped. Ensure experiment
                # exists on dest so the no-DICOM finalize path can proceed.
                if not existing_id:
                    self._create_experiment_shell(dest_project, subject, exp)
                work_dir_handle.cleanup()
                return None

            return DeferredExperiment(
                exp=exp,
                subject=subject,
                scans=scans,
                scan_resources_cache=scan_resources_cache,
                dicom_scan_count=dicom_scan_count,
                sync_id=sync_id,
                dest_project=dest_project,
                work_dir=work_dir,
                work_dir_handle=work_dir_handle,
                archive_timeout_at=time.monotonic() + self.config.archive_wait_timeout,
            )
        except Exception:  # noqa: BLE001  # cleanup-then-reraise: temp dir must not leak on failure; error not swallowed
            work_dir_handle.cleanup()
            raise

    def _scans_have_no_dicom(
        self,
        scans: list[dict[str, Any]],
        exp: DiscoveredEntity,
        scan_resources_cache: dict[str, list[dict[str, Any]]],
    ) -> bool:
        """Report whether none of the experiment's scans have transferable DICOM.

        Args:
            scans: Discovered scans for the experiment.
            exp: Discovered experiment entity.
            scan_resources_cache: Per-scan resource cache (populated as a side
                effect and reused by later phases).

        Returns:
            True if no scan has transferable DICOM.
        """
        return not self.scan_transfer.scans_have_transferable_dicom(
            scans, exp, scan_resources_cache
        )
