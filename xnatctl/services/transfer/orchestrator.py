"""Transfer orchestrator -- coordinates the 8-stage transfer pipeline.

Wires together discovery, filtering, conflict checking, execution,
verification, and state storage into a single run() entry point. Owns
*which* subjects get transferred (discovery, filtering, reconciliation of
subjects/experiments deleted from the destination); the actual per-subject
transfer -- including DICOM archive-wait pipelining -- is delegated to
:class:`~xnatctl.services.transfer.scan_pipeline.ScanPipeline`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from xnatctl.core.state import EntityStatus, SyncStatus, TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.conflicts import ConflictChecker
from xnatctl.services.transfer.discovery import ChangeType, DiscoveredEntity, DiscoveryService
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
from xnatctl.services.transfer.scan_pipeline import ScanPipeline
from xnatctl.services.transfer.verifier import Verifier

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)


@dataclass
class TransferResult:
    """Summary of a transfer run.

    Attributes:
        subjects_synced: Number of subjects transferred.
        subjects_failed: Number of subjects that failed.
        subjects_skipped: Number of subjects skipped.
        experiments_synced: Number of experiments transferred and verified.
        experiments_failed: Number of experiments that failed.
        scans_synced: Number of scans transferred.
        scans_failed: Number of scans that failed.
        resources_synced: Number of non-DICOM resources transferred.
        resources_failed: Number of non-DICOM resources that failed.
        verified_count: Number of experiments verified.
        not_verified_count: Number of experiments that failed verification.
        success: Overall success flag.
        errors: List of error messages.
        dry_run: Whether this was a dry run.
    """

    subjects_synced: int = 0
    subjects_failed: int = 0
    subjects_skipped: int = 0
    experiments_synced: int = 0
    experiments_failed: int = 0
    scans_synced: int = 0
    scans_failed: int = 0
    resources_synced: int = 0
    resources_failed: int = 0
    verified_count: int = 0
    not_verified_count: int = 0
    success: bool = True
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


class TransferOrchestrator:
    """Orchestrates incremental project transfer between XNAT instances.

    Args:
        source_client: Authenticated source XNATClient.
        dest_client: Authenticated destination XNATClient.
        state_store: SQLite state store.
        config: Transfer configuration.
    """

    def __init__(
        self,
        source_client: XNATClient,
        dest_client: XNATClient,
        state_store: TransferStateStore,
        config: TransferConfig,
    ) -> None:
        self.source_client = source_client
        self.dest_client = dest_client
        self.state_store = state_store
        self.config = config

        self.discovery = DiscoveryService(source_client)
        self.filter_engine = FilterEngine(config.filtering)
        self.conflict_checker = ConflictChecker(dest_client)
        self.executor = TransferExecutor(source_client, dest_client)
        self.verifier = Verifier(source_client, dest_client)
        self.scan_pipeline = ScanPipeline(
            executor=self.executor,
            filter_engine=self.filter_engine,
            conflict_checker=self.conflict_checker,
            discovery=self.discovery,
            state_store=self.state_store,
            verifier=self.verifier,
            config=self.config,
        )

    def _should_abort(self, consecutive_failures: int) -> bool:
        """Check if the circuit breaker should trip.

        Args:
            consecutive_failures: Number of consecutive subject failures.

        Returns:
            True if we should abort.
        """
        return consecutive_failures >= self.config.max_failures

    def _reconcile_with_dest(
        self,
        incremental_subjects: list[DiscoveredEntity],
        src_proj: str,
        dst_proj: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[DiscoveredEntity]:
        """Find previously-synced subjects that no longer exist on the destination.

        Compares id_mapping entries against actual destination subjects. Any
        mapped subject missing from the destination is returned for re-sync.

        Args:
            incremental_subjects: Subjects from incremental discovery.
            src_proj: Source project ID.
            dst_proj: Destination project ID.
            progress_callback: Optional progress callback.

        Returns:
            List of subjects to re-sync (ChangeType.RETRY).
        """
        src_url = str(self.source_client.base_url)
        dst_url = str(self.dest_client.base_url)

        mappings = self.state_store.get_all_mappings(src_url, src_proj, dst_url, dst_proj)
        subject_mappings = [m for m in mappings if m["entity_type"] == "subject"]
        if not subject_mappings:
            return []

        included_ids = {s.local_id for s in incremental_subjects}
        stale_candidates = [m for m in subject_mappings if m["local_id"] not in included_ids]
        if not stale_candidates:
            return []

        # Single batch query: all subject IDs on dest
        try:
            dest_subject_ids = self.executor.list_dest_subjects(dst_proj)
        except Exception:
            logger.warning("Failed to query dest subjects for reconciliation", exc_info=True)
            return []

        missing_local_ids = {
            m["local_id"] for m in stale_candidates if m["remote_id"] not in dest_subject_ids
        }
        if not missing_local_ids:
            return []

        # Re-discover missing subjects from source (full discovery, no cutoff)
        all_source = self.discovery.discover_subjects(src_proj, last_sync_time=None)
        source_by_id = {s.local_id: s for s in all_source}

        reconciled: list[DiscoveredEntity] = []
        for local_id in missing_local_ids:
            source = source_by_id.get(local_id)
            if source is None:
                continue
            reconciled.append(
                DiscoveredEntity(
                    local_id=source.local_id,
                    local_label=source.local_label,
                    change_type=ChangeType.RETRY,
                    xsi_type=source.xsi_type,
                    insert_date=source.insert_date,
                    last_modified=source.last_modified,
                )
            )
            if progress_callback:
                progress_callback(f"  Subject {source.local_label} missing on dest, will re-sync")

        return reconciled

    def _reconcile_experiments_with_dest(
        self,
        current_subjects: list[DiscoveredEntity],
        src_proj: str,
        dst_proj: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[DiscoveredEntity]:
        """Find subjects whose experiments were deleted from dest.

        Checks experiment ID mappings against dest, finds missing experiments,
        looks up their parent subjects, and returns parents not already in the
        transfer list so they get re-processed.

        Args:
            current_subjects: Subjects already queued for transfer.
            src_proj: Source project ID.
            dst_proj: Destination project ID.
            progress_callback: Optional progress callback.

        Returns:
            List of parent subjects to re-sync (ChangeType.RETRY).
        """
        src_url = str(self.source_client.base_url)
        dst_url = str(self.dest_client.base_url)

        mappings = self.state_store.get_all_mappings(src_url, src_proj, dst_url, dst_proj)
        exp_mappings = [m for m in mappings if m["entity_type"] == "experiment"]
        if not exp_mappings:
            return []

        try:
            dest_exp_ids = self.executor.list_dest_experiments(dst_proj)
        except Exception:
            logger.warning("Failed to query dest experiments for reconciliation", exc_info=True)
            return []

        missing = [m for m in exp_mappings if m["remote_id"] not in dest_exp_ids]
        if not missing:
            return []

        missing_local_ids = {m["local_id"] for m in missing}
        parent_subject_ids = self.state_store.get_experiment_parents(missing_local_ids)
        if not parent_subject_ids:
            return []

        current_subject_ids = {s.local_id for s in current_subjects}
        needed_parents = parent_subject_ids - current_subject_ids
        if not needed_parents:
            return []

        all_source = self.discovery.discover_subjects(src_proj, last_sync_time=None)
        source_by_id = {s.local_id: s for s in all_source}

        reconciled: list[DiscoveredEntity] = []
        for pid in needed_parents:
            source = source_by_id.get(pid)
            if source is None:
                continue
            reconciled.append(
                DiscoveredEntity(
                    local_id=source.local_id,
                    local_label=source.local_label,
                    change_type=ChangeType.RETRY,
                    xsi_type=source.xsi_type,
                    insert_date=source.insert_date,
                    last_modified=source.last_modified,
                )
            )
            if progress_callback:
                progress_callback(
                    f"  Subject {source.local_label} has experiment(s) missing on dest,"
                    " will re-sync"
                )

        return reconciled

    def _run_reconciliation(
        self,
        subjects: list[DiscoveredEntity],
        src_proj: str,
        dst_proj: str,
        progress_callback: Callable[[str], None] | None,
    ) -> None:
        """Extend *subjects* in place with any subjects reconciled from the destination.

        Args:
            subjects: Subjects from incremental discovery; extended in place.
            src_proj: Source project ID.
            dst_proj: Destination project ID.
            progress_callback: Optional progress callback.
        """
        reconciled = self._reconcile_with_dest(subjects, src_proj, dst_proj, progress_callback)
        if reconciled:
            if progress_callback:
                progress_callback(f"  Reconciliation: {len(reconciled)} subject(s) missing on dest")
            subjects.extend(reconciled)

        reconciled_exp = self._reconcile_experiments_with_dest(
            subjects, src_proj, dst_proj, progress_callback
        )
        if reconciled_exp:
            if progress_callback:
                progress_callback(
                    f"  Experiment reconciliation: {len(reconciled_exp)} "
                    f"subject(s) need experiment re-sync"
                )
            subjects.extend(reconciled_exp)

    def _transfer_one_subject(
        self,
        subject: DiscoveredEntity,
        sync_id: int,
        dst_proj: str,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None,
    ) -> bool:
        """Transfer one subject via the scan pipeline, recording failure state.

        Args:
            subject: Discovered subject entity.
            sync_id: Current sync run ID.
            dst_proj: Destination project ID.
            result: Mutable result to update.
            progress_callback: Optional progress callback.

        Returns:
            True on success, False if the subject failed.
        """
        try:
            self.scan_pipeline.transfer_subject(
                subject, sync_id, dst_proj, result, progress_callback
            )
            result.subjects_synced += 1
        except Exception as e:
            result.subjects_failed += 1
            result.success = False
            result.errors.append(f"Subject {subject.local_label}: {e}")
            self.state_store.record_entity(
                sync_id=sync_id,
                entity_type="subject",
                local_id=subject.local_id,
                local_label=subject.local_label,
                status=EntityStatus.FAILED,
                message=str(e),
            )
            return False
        return True

    def run(
        self,
        dry_run: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ) -> TransferResult:
        """Execute the transfer pipeline.

        Args:
            dry_run: If True, discover and filter but don't transfer.
            progress_callback: Optional callback for progress messages.

        Returns:
            TransferResult summarizing the run.
        """
        result = TransferResult(dry_run=dry_run)
        src_url = str(self.source_client.base_url)
        dst_url = str(self.dest_client.base_url)
        src_proj = self.config.source_project
        dst_proj = self.config.dest_project

        last_sync = self.state_store.get_last_sync_time(src_url, src_proj, dst_url, dst_proj)

        # Dry-run: discover only, never mutate state store
        if dry_run:
            if progress_callback:
                progress_callback("Discovering subjects...")
            subjects = self.discovery.discover_subjects(src_proj, last_sync_time=last_sync)
            result.subjects_skipped = len(subjects)
            if progress_callback:
                progress_callback(f"[DRY RUN] Found {len(subjects)} subjects to transfer")
            return result

        sync_id = self.state_store.start_sync(src_url, src_proj, dst_url, dst_proj)

        try:
            if progress_callback:
                progress_callback("Discovering subjects...")

            subjects = self.discovery.discover_subjects(src_proj, last_sync_time=last_sync)

            # Reconcile: re-include previously-synced subjects deleted from dest
            if last_sync:
                self._run_reconciliation(subjects, src_proj, dst_proj, progress_callback)

            consecutive_failures = 0
            for subject in subjects:
                if self._should_abort(consecutive_failures):
                    result.errors.append(
                        f"Circuit breaker: {consecutive_failures} consecutive failures"
                    )
                    result.success = False
                    break

                if self._transfer_one_subject(
                    subject, sync_id, dst_proj, result, progress_callback
                ):
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

            status = SyncStatus.COMPLETED if result.success else SyncStatus.FAILED
            self.state_store.end_sync(
                sync_id,
                status,
                subjects_synced=result.subjects_synced,
                subjects_failed=result.subjects_failed,
                subjects_skipped=result.subjects_skipped,
            )

        except Exception as e:
            result.success = False
            result.errors.append(str(e))
            self.state_store.end_sync(sync_id, SyncStatus.FAILED)

        return result
