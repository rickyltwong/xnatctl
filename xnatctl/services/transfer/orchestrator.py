"""Transfer orchestrator -- coordinates the 8-stage transfer pipeline.

Wires together discovery, filtering, conflict checking, execution,
verification, and state storage into a single run() entry point.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from xnatctl.core.state import EntityStatus, SyncStatus, TransferStateStore
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.conflicts import ConflictChecker
from xnatctl.services.transfer.discovery import DiscoveredEntity, DiscoveryService
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
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
        experiments_synced: Number of experiments transferred.
        experiments_failed: Number of experiments that failed.
        verified_count: Number of resources verified.
        not_verified_count: Number of resources that failed verification.
        success: Overall success flag.
        errors: List of error messages.
        dry_run: Whether this was a dry run.
    """

    subjects_synced: int = 0
    subjects_failed: int = 0
    subjects_skipped: int = 0
    experiments_synced: int = 0
    experiments_failed: int = 0
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

    def _should_abort(self, consecutive_failures: int) -> bool:
        """Check if the circuit breaker should trip.

        Args:
            consecutive_failures: Number of consecutive subject failures.

        Returns:
            True if we should abort.
        """
        return consecutive_failures >= self.config.max_failures

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
        sync_id = self.state_store.start_sync(src_url, src_proj, dst_url, dst_proj)

        try:
            if progress_callback:
                progress_callback("Discovering subjects...")

            subjects = self.discovery.discover_subjects(src_proj, last_sync_time=last_sync)

            if dry_run:
                result.subjects_skipped = len(subjects)
                if progress_callback:
                    progress_callback(f"[DRY RUN] Found {len(subjects)} subjects to transfer")
                self.state_store.end_sync(
                    sync_id,
                    SyncStatus.COMPLETED,
                    subjects_skipped=len(subjects),
                )
                return result

            consecutive_failures = 0
            for subject in subjects:
                if self._should_abort(consecutive_failures):
                    result.errors.append(
                        f"Circuit breaker: {consecutive_failures} consecutive failures"
                    )
                    result.success = False
                    break

                try:
                    self._transfer_subject(subject, sync_id, dst_proj, result, progress_callback)
                    consecutive_failures = 0
                    result.subjects_synced += 1
                except Exception as e:
                    consecutive_failures += 1
                    result.subjects_failed += 1
                    result.errors.append(f"Subject {subject.local_label}: {e}")
                    self.state_store.record_entity(
                        sync_id=sync_id,
                        entity_type="subject",
                        local_id=subject.local_id,
                        local_label=subject.local_label,
                        status=EntityStatus.FAILED,
                        message=str(e),
                    )

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

    def _transfer_subject(
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

        src_url = str(self.source_client.base_url)
        dst_url = str(self.dest_client.base_url)
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

        self.executor.create_subject(dest_project, subject.local_label)

        self.state_store.save_id_mapping(
            src_url,
            src_proj,
            dst_url,
            dest_project,
            subject.local_id,
            subject.local_id,
            "subject",
        )

        self.state_store.record_entity(
            sync_id=sync_id,
            entity_type="subject",
            local_id=subject.local_id,
            local_label=subject.local_label,
            status=EntityStatus.SYNCED,
        )

        experiments = self.discovery.discover_experiments(
            src_proj,
            subject.local_id,
            last_sync_time=None,
        )

        for exp in experiments:
            if not self.filter_engine.should_include_experiment(exp):
                continue

            try:
                with tempfile.TemporaryDirectory() as work_dir:
                    self.executor.transfer_experiment_zip(
                        source_experiment_id=exp.local_id,
                        dest_project=dest_project,
                        dest_subject=subject.local_label,
                        dest_experiment_label=exp.local_label,
                        work_dir=Path(work_dir),
                    )

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
            except Exception as e:
                result.experiments_failed += 1
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
