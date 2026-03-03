"""Transfer orchestrator -- coordinates the 8-stage transfer pipeline.

Wires together discovery, filtering, conflict checking, execution,
verification, and state storage into a single run() entry point.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

            consecutive_failures = 0
            for subject in subjects:
                if self._should_abort(consecutive_failures):
                    result.errors.append(
                        f"Circuit breaker: {consecutive_failures} consecutive failures"
                    )
                    result.success = False
                    break

                try:
                    self._transfer_subject(
                        subject,
                        sync_id,
                        dst_proj,
                        result,
                        progress_callback,
                    )
                    consecutive_failures = 0
                    result.subjects_synced += 1
                except Exception as e:
                    consecutive_failures += 1
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

        experiments = self.discovery.discover_experiments(
            src_proj,
            subject.local_id,
            last_sync_time=None,
        )

        for exp in experiments:
            if not self.filter_engine.should_include_experiment(exp):
                continue

            try:
                self._transfer_experiment(
                    exp,
                    sync_id,
                    dest_project,
                    subject,
                    result,
                    progress_callback,
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

    def _transfer_experiment(
        self,
        exp: DiscoveredEntity,
        sync_id: int,
        dest_project: str,
        subject: DiscoveredEntity,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Transfer a single experiment with per-scan decomposition.

        Args:
            exp: Discovered experiment entity.
            sync_id: Current sync run ID.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        if progress_callback:
            progress_callback(f"  Experiment {exp.local_label}...")

        # Check experiment existence on destination
        existing_id = self.executor.check_experiment_exists(dest_project, exp.local_label)
        if not existing_id:
            self.executor.create_experiment(
                dest_project,
                subject.local_label,
                exp.local_label,
                exp.xsi_type or "xnat:imageSessionData",
            )

        with tempfile.TemporaryDirectory() as work_dir_str:
            work_dir = Path(work_dir_str)

            # Discover and transfer scans
            scans = self.executor.discover_scans(exp.local_id)
            self._transfer_scans(
                scans,
                exp,
                dest_project,
                subject,
                work_dir,
                result,
                progress_callback,
            )

            # Transfer session-level resources
            self._transfer_session_resources(
                exp,
                dest_project,
                subject,
                work_dir,
                result,
                progress_callback,
            )

        # Verification
        if self.config.verify_after_transfer:
            self._verify_and_record_experiment(
                exp,
                sync_id,
                dest_project,
                subject,
                result,
            )
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

    def _transfer_scans(
        self,
        scans: list[dict[str, Any]],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Transfer all scans for an experiment, in parallel.

        Args:
            scans: List of scan dicts from source.
            exp: Parent experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            work_dir: Temporary working directory.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
        """
        # Filter scans
        xsi_type = exp.xsi_type or ""
        filtered_scans = [
            s for s in scans if self.filter_engine.should_include_scan(xsi_type, s.get("type", ""))
        ]

        if not filtered_scans:
            return

        workers = min(self.config.scan_workers, len(filtered_scans))

        def transfer_single_scan(
            scan: dict[str, Any],
        ) -> tuple[str, bool, str]:
            scan_id = scan.get("ID", "")
            scan_work_dir = work_dir / f"scan_{scan_id}"
            try:
                self._transfer_single_scan(
                    scan,
                    exp,
                    dest_project,
                    subject,
                    scan_work_dir,
                )
                return scan_id, True, ""
            except Exception as e:
                return scan_id, False, str(e)

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(transfer_single_scan, s): s.get("ID", "") for s in filtered_scans
                }
                for future in as_completed(futures):
                    scan_id, success, error = future.result()
                    if success:
                        result.scans_synced += 1
                    else:
                        result.scans_failed += 1
                        result.errors.append(f"Scan {scan_id} ({exp.local_label}): {error}")
        else:
            for scan in filtered_scans:
                scan_id, success, error = transfer_single_scan(scan)
                if success:
                    result.scans_synced += 1
                else:
                    result.scans_failed += 1
                    result.errors.append(f"Scan {scan_id} ({exp.local_label}): {error}")

    def _transfer_single_scan(
        self,
        scan: dict[str, Any],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
    ) -> None:
        """Transfer a single scan: DICOM + non-DICOM resources.

        Args:
            scan: Scan dict from source.
            exp: Parent experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            work_dir: Temporary working directory for this scan.
        """
        scan_id = scan.get("ID", "")
        xsi_type = exp.xsi_type or ""

        resources = self.executor.discover_scan_resources(exp.local_id, scan_id)

        for res in resources:
            res_label = res.get("label", "")
            if not self.filter_engine.should_include_scan_resource(xsi_type, res_label):
                continue

            if res_label == "DICOM":
                self.executor.transfer_scan_dicom(
                    source_experiment_id=exp.local_id,
                    scan_id=scan_id,
                    dest_project=dest_project,
                    dest_subject=subject.local_label,
                    dest_experiment_label=exp.local_label,
                    work_dir=work_dir,
                    retry_count=self.config.scan_retry_count,
                    retry_delay=self.config.scan_retry_delay,
                )
            else:
                src_path = (
                    f"/data/experiments/{exp.local_id}/scans/{scan_id}/resources/{res_label}/files"
                )
                dst_path = (
                    f"/data/projects/{dest_project}"
                    f"/subjects/{subject.local_label}"
                    f"/experiments/{exp.local_label}"
                    f"/scans/{scan_id}"
                    f"/resources/{res_label}/files"
                )
                self.executor.transfer_resource(
                    source_path=src_path,
                    dest_path=dst_path,
                    resource_label=f"{scan_id}_{res_label}",
                    work_dir=work_dir,
                )

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
                src_path = f"/data/experiments/{exp.local_id}/resources/{res_label}/files"
                dst_path = (
                    f"/data/projects/{dest_project}"
                    f"/subjects/{subject.local_label}"
                    f"/experiments/{exp.local_label}"
                    f"/resources/{res_label}/files"
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
        src_path = f"/data/experiments/{exp.local_id}"
        dst_path = (
            f"/data/projects/{dest_project}"
            f"/subjects/{subject.local_label}"
            f"/experiments/{exp.local_label}"
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
