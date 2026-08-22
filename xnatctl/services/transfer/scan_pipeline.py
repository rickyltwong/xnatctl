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
from xnatctl.core.validation import quote_path_segment
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
from xnatctl.services.transfer.scan_transfer import ScanTransfer
from xnatctl.services.transfer.verifier import Verifier

if TYPE_CHECKING:
    from xnatctl.services.transfer.orchestrator import TransferResult

logger = logging.getLogger(__name__)


class ScanPipeline:
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

    def transfer_subject(  # noqa: C901  # pre-existing; see pyproject
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

        src_url = str(self.executor.source.base_url)
        dst_url = str(self.executor.dest.base_url)
        src_proj = self.config.source_project

        remote_id = self.state_store.get_remote_id(
            src_url, src_proj, dst_url, dest_project, subject.local_id
        )

        if remote_id:
            conflict = self.conflict_checker.check_subject(
                remote_id, subject.local_label, dest_project
            )
            if conflict.has_conflict:
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
                return

        # Create subject and store ACTUAL remote ID from response
        remote_uri = self.executor.create_subject(dest_project, subject.local_label)
        actual_remote_id = remote_uri.split("/")[-1]

        self.state_store.save_id_mapping(
            src_url,
            src_proj,
            dst_url,
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

        all_experiments = self.discovery.discover_experiments(
            src_proj,
            subject.local_id,
            last_sync_time=None,
        )
        experiments = [
            e for e in all_experiments if self.filter_engine.should_include_experiment(e)
        ]

        if not experiments:
            return

        poller = ArchivePoller(self.executor, self.config.archive_poll_interval)
        poller.start()
        deferred: deque[DeferredExperiment] = deque()
        pipelining_disabled = False

        try:
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
                except Exception as e:
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

            self._drain_remaining_after_dispatch(
                poller, deferred, pipelining_disabled, result, progress_callback
            )
        finally:
            poller.stop()
            # Clean up any remaining temp directories
            for ctx in deferred:
                try:
                    ctx.work_dir_handle.cleanup()
                except Exception:
                    pass

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

            if not existing_id:
                has_any_dicom = self.scan_transfer.scans_have_transferable_dicom(
                    scans, exp, scan_resources_cache
                )
                if not has_any_dicom:
                    self.executor.create_experiment(
                        dest_project,
                        subject.local_label,
                        exp.local_label,
                        exp.xsi_type or "xnat:imageSessionData",
                    )
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
                    self.executor.create_experiment(
                        dest_project,
                        subject.local_label,
                        exp.local_label,
                        exp.xsi_type or "xnat:imageSessionData",
                    )
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
        except Exception:
            work_dir_handle.cleanup()
            raise

    def _finalize_experiment(
        self,
        ctx: DeferredExperiment,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Complete deferred phases for an experiment after archive is ready.

        Runs Phase 2.5 (XML overlay), Phase 3 (non-DICOM resources),
        Phase 4 (session resources), and verification.

        Args:
            ctx: Deferred experiment context.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        try:
            exp, subject, dest_project = ctx.exp, ctx.subject, ctx.dest_project

            # Phase 2.5: XML metadata overlay
            self._apply_xml_overlay(
                exp,
                dest_project,
                subject,
                progress_callback,
                f"    XML overlay applied for {exp.local_label}",
                "XML overlay failed for %s",
            )

            # Phase 3: Transfer non-DICOM scan resources
            self.scan_transfer.transfer_scans(
                ctx.scans,
                exp,
                dest_project,
                subject,
                ctx.work_dir,
                result,
                progress_callback,
                dicom_only=False,
                scan_resources_cache=ctx.scan_resources_cache,
            )

            # Phase 4: Transfer session-level resources
            self._transfer_session_resources(
                exp, dest_project, subject, ctx.work_dir, result, progress_callback
            )

            # Verification
            if self.config.verify_after_transfer:
                self._verify_and_record_experiment(exp, ctx.sync_id, dest_project, subject, result)
            else:
                result.experiments_synced += 1
                self.state_store.record_entity(
                    sync_id=ctx.sync_id,
                    entity_type="experiment",
                    local_id=exp.local_id,
                    local_label=exp.local_label,
                    xsi_type=exp.xsi_type,
                    parent_local_id=subject.local_id,
                    status=EntityStatus.SYNCED,
                )

            # Save experiment ID mapping for future reconciliation
            self._save_experiment_mapping(exp, dest_project)
        finally:
            ctx.work_dir_handle.cleanup()

    def _finalize_experiment_no_dicom(
        self,
        exp: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        subject: DiscoveredEntity,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Full pipeline for experiments with no DICOM resources.

        Runs XML overlay, non-DICOM resources, session resources, and
        verification. Uses a fresh temp directory with context-manager scope.

        Args:
            exp: Discovered experiment entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        with tempfile.TemporaryDirectory() as work_dir_str:
            work_dir = Path(work_dir_str)

            scans = self.executor.discover_scans(exp.local_id)
            scan_resources_cache: dict[str, list[dict[str, Any]]] = {}

            # XML overlay
            self._apply_xml_overlay(
                exp,
                dest_project,
                subject,
                progress_callback,
                f"    XML overlay applied for {exp.local_label}",
                "XML overlay failed for %s",
            )

            # Non-DICOM resources
            self.scan_transfer.transfer_scans(
                scans,
                exp,
                dest_project,
                subject,
                work_dir,
                result,
                progress_callback,
                dicom_only=False,
                scan_resources_cache=scan_resources_cache,
                create_missing_scans=True,
            )

            # Session-level resources
            self._transfer_session_resources(
                exp, dest_project, subject, work_dir, result, progress_callback
            )

        # Verification
        if self.config.verify_after_transfer:
            self._verify_and_record_experiment(exp, sync_id, dest_project, subject, result)
        else:
            result.experiments_synced += 1
            self.state_store.record_entity(
                sync_id=sync_id,
                entity_type="experiment",
                local_id=exp.local_id,
                local_label=exp.local_label,
                xsi_type=exp.xsi_type,
                parent_local_id=subject.local_id,
                status=EntityStatus.SYNCED,
            )

        # Save experiment ID mapping for future reconciliation
        self._save_experiment_mapping(exp, dest_project)

    def _save_experiment_mapping(
        self,
        exp: DiscoveredEntity,
        dest_project: str,
    ) -> None:
        """Save experiment ID mapping after successful transfer.

        Args:
            exp: Transferred experiment entity.
            dest_project: Destination project ID.
        """
        dest_exp_id = self.executor.check_experiment_exists(dest_project, exp.local_label)
        if dest_exp_id:
            self.state_store.save_id_mapping(
                str(self.executor.source.base_url),
                self.config.source_project,
                str(self.executor.dest.base_url),
                dest_project,
                exp.local_id,
                dest_exp_id,
                "experiment",
            )

    def _apply_xml_overlay(
        self,
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        progress_callback: Callable[[str], None] | None,
        applied_message: str,
        failed_message: str,
    ) -> None:
        """Apply the XML metadata overlay if enabled; a failure is non-fatal.

        Args:
            exp: Experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            progress_callback: Optional progress callback.
            applied_message: Progress message reported on success.
            failed_message: ``%s``-experiment_label warning logged (with
                exc_info) on failure.
        """
        if not self.config.transfer_xml_metadata:
            return
        try:
            self.executor.apply_xml_overlay(
                source_experiment_id=exp.local_id,
                dest_project=dest_project,
                dest_subject=subject.local_label,
                dest_experiment_label=exp.local_label,
            )
            if progress_callback:
                progress_callback(applied_message)
        except Exception:
            logger.warning(failed_message, exp.local_label, exc_info=True)

    def _transfer_session_resources(
        self,
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Transfer session-level resources for an experiment.

        Args:
            exp: Experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            work_dir: Temporary working directory.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        xsi_type = exp.xsi_type or ""

        try:
            resources = self.executor.discover_session_resources(exp.local_id)
        except Exception as e:
            logger.warning(
                "Failed to discover session resources for %s: %s",
                exp.local_label,
                e,
            )
            return

        for res in resources:
            res_label = res.get("label", "")
            if not self.filter_engine.should_include_session_resource(xsi_type, res_label):
                continue

            try:
                src_path = (
                    f"/data/experiments/{quote_path_segment(exp.local_id)}"
                    f"/resources/{quote_path_segment(res_label)}/files"
                )
                dst_path = (
                    f"/data/projects/{quote_path_segment(dest_project)}"
                    f"/subjects/{quote_path_segment(subject.local_label)}"
                    f"/experiments/{quote_path_segment(exp.local_label)}"
                    f"/resources/{quote_path_segment(res_label)}/files"
                )
                self.executor.transfer_resource(
                    source_path=src_path,
                    dest_path=dst_path,
                    resource_label=f"session_{res_label}",
                    work_dir=work_dir,
                )
                result.resources_synced += 1
            except Exception as e:
                result.resources_failed += 1
                result.errors.append(f"Session resource {res_label} ({exp.local_label}): {e}")

    def _verify_and_record_experiment(
        self,
        exp: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        subject: DiscoveredEntity,
        result: TransferResult,
    ) -> None:
        """Verify an experiment transfer and record status.

        Args:
            exp: Experiment entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            result: Mutable result to update.
        """
        src_path = f"/data/experiments/{quote_path_segment(exp.local_id)}"
        dst_path = (
            f"/data/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(subject.local_label)}"
            f"/experiments/{quote_path_segment(exp.local_label)}"
        )

        try:
            verification = self.verifier.verify_experiment(src_path, dst_path)
        except Exception as e:
            logger.warning("Verification failed for %s: %s", exp.local_label, e)
            verification = None

        if verification and verification.verified:
            result.experiments_synced += 1
            result.verified_count += 1
            status = EntityStatus.VERIFIED
            message = verification.message
        else:
            result.experiments_failed += 1
            result.not_verified_count += 1
            result.success = False
            status = EntityStatus.FAILED
            message = verification.message if verification else "Verification error"
            result.errors.append(f"Verification failed for {exp.local_label}: {message}")

        self.state_store.record_entity(
            sync_id=sync_id,
            entity_type="experiment",
            local_id=exp.local_id,
            local_label=exp.local_label,
            xsi_type=exp.xsi_type,
            parent_local_id=subject.local_id,
            status=status,
            message=message,
        )
