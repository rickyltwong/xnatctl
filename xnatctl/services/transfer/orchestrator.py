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

import httpx

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

        with tempfile.TemporaryDirectory() as work_dir_str:
            work_dir = Path(work_dir_str)

            # Discover scans
            scans = self.executor.discover_scans(exp.local_id)

            # Shared cache: scan resources discovered in phase 1 are
            # reused in phase 3 to avoid redundant API calls.
            scan_resources_cache: dict[str, list[dict[str, Any]]] = {}

            # Check experiment existence on destination.
            # Only pre-create when no DICOM scans exist (DICOM upload
            # auto-archives and creates the experiment itself; pre-creating
            # causes a prearchive CONFLICT).
            existing_id = self.executor.check_experiment_exists(dest_project, exp.local_label)
            if not existing_id:
                has_any_dicom = self._scans_have_transferable_dicom(
                    scans, exp, scan_resources_cache
                )
                if not has_any_dicom:
                    self.executor.create_experiment(
                        dest_project,
                        subject.local_label,
                        exp.local_label,
                        exp.xsi_type or "xnat:imageSessionData",
                    )

            # Phase 1: Transfer DICOM resources only (parallel)
            dicom_scan_count = self._transfer_scans(
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

            # Phase 2: Wait for prearchive resolution
            if dicom_scan_count > 0:
                self._wait_for_prearchive_resolution(
                    exp,
                    dest_project,
                    subject,
                    dicom_scan_count,
                    progress_callback,
                )

            # Phase 2.5: XML metadata overlay
            if self.config.transfer_xml_metadata:
                try:
                    self.executor.apply_xml_overlay(
                        source_experiment_id=exp.local_id,
                        dest_project=dest_project,
                        dest_subject=subject.local_label,
                        dest_experiment_label=exp.local_label,
                    )
                    if progress_callback:
                        progress_callback(f"    XML metadata overlay applied for {exp.local_label}")
                except Exception:
                    logger.warning(
                        "XML metadata overlay failed for %s, continuing...",
                        exp.local_label,
                        exc_info=True,
                    )

            # Phase 3: Transfer non-DICOM scan resources (parallel)
            self._transfer_scans(
                scans,
                exp,
                dest_project,
                subject,
                work_dir,
                result,
                progress_callback,
                dicom_only=False,
                scan_resources_cache=scan_resources_cache,
            )

            # Phase 4: Transfer session-level resources
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

    def _scans_have_transferable_dicom(
        self,
        scans: list[dict[str, Any]],
        exp: DiscoveredEntity,
        scan_resources_cache: dict[str, list[dict[str, Any]]],
    ) -> bool:
        """Check whether any scan has a DICOM resource that will be transferred.

        Consults the filter engine so that the decision to skip experiment
        pre-creation only applies when DICOM will actually be imported
        (triggering auto-archive).

        Populates *scan_resources_cache* as a side effect so Phase 1
        can reuse the results without redundant API calls.

        Args:
            scans: List of scan dicts from source.
            exp: Parent experiment entity.
            scan_resources_cache: Shared cache to populate.

        Returns:
            True if at least one scan has a DICOM resource that passes
            the resource filter.
        """
        xsi_type = exp.xsi_type or ""
        for scan in scans:
            if not self.filter_engine.should_include_scan(xsi_type, scan.get("type", "")):
                continue
            scan_id = scan.get("ID", "")
            resources = self.executor.discover_scan_resources(exp.local_id, scan_id)
            scan_resources_cache[scan_id] = resources
            for r in resources:
                label = r.get("label", "")
                if label == "DICOM" and self.filter_engine.should_include_scan_resource(
                    xsi_type, label
                ):
                    return True
        return False

    def _transfer_scans(
        self,
        scans: list[dict[str, Any]],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
        dicom_only: bool = True,
        scan_resources_cache: dict[str, list[dict[str, Any]]] | None = None,
    ) -> int:
        """Transfer scans for an experiment, in parallel.

        When dicom_only=True, only DICOM resources are transferred.
        When dicom_only=False, only non-DICOM resources are transferred.

        Args:
            scans: List of scan dicts from source.
            exp: Parent experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            work_dir: Temporary working directory.
            result: Mutable result to update.
            progress_callback: Optional progress callback.
            dicom_only: Phase selector (True=DICOM, False=non-DICOM).
            scan_resources_cache: Shared cache of scan resources across phases.

        Returns:
            Number of scans processed in this phase.
        """
        if scan_resources_cache is None:
            scan_resources_cache = {}

        xsi_type = exp.xsi_type or ""
        filtered_scans = [
            s for s in scans if self.filter_engine.should_include_scan(xsi_type, s.get("type", ""))
        ]

        if not filtered_scans:
            return 0

        workers = min(self.config.scan_workers, len(filtered_scans))
        processed = 0

        def transfer_single_scan(
            scan: dict[str, Any],
        ) -> tuple[str, bool, bool, str]:
            """Returns (scan_id, success, transferred, error)."""
            scan_id = scan.get("ID", "")
            scan_work_dir = work_dir / f"scan_{scan_id}"
            try:
                did_transfer = self._transfer_single_scan(
                    scan,
                    exp,
                    dest_project,
                    subject,
                    scan_work_dir,
                    dicom_only=dicom_only,
                    scan_resources_cache=scan_resources_cache,
                )
                return scan_id, True, did_transfer, ""
            except Exception as e:
                return scan_id, False, False, str(e)

        def record_result(scan_id: str, success: bool, transferred: bool, error: str) -> int:
            """Record scan result and return 1 if transferred, 0 otherwise."""
            if success:
                if dicom_only:
                    result.scans_synced += 1
                else:
                    result.resources_synced += 1
                return 1 if transferred else 0
            if dicom_only:
                result.scans_failed += 1
            else:
                result.resources_failed += 1
            result.errors.append(f"Scan {scan_id} ({exp.local_label}): {error}")
            return 0

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(transfer_single_scan, s): s.get("ID", "") for s in filtered_scans
                }
                for future in as_completed(futures):
                    processed += record_result(*future.result())
        else:
            for scan in filtered_scans:
                processed += record_result(*transfer_single_scan(scan))

        return processed

    def _transfer_single_scan(
        self,
        scan: dict[str, Any],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        dicom_only: bool = True,
        scan_resources_cache: dict[str, list[dict[str, Any]]] | None = None,
    ) -> bool:
        """Transfer a single scan's resources.

        Args:
            scan: Scan dict from source.
            exp: Parent experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            work_dir: Temporary working directory for this scan.
            dicom_only: Phase selector (True=DICOM, False=non-DICOM).
            scan_resources_cache: Shared cache of scan resources across phases.

        Returns:
            True if at least one resource was actually transferred.
        """
        scan_id = scan.get("ID", "")
        xsi_type = exp.xsi_type or ""
        transferred = False

        # Use cached resources or discover and cache.
        # Thread safety: each scan_id is processed by exactly one thread
        # within a phase, so dict keys are disjoint across workers (CPython GIL).
        cached = (scan_resources_cache or {}).get(scan_id)
        if cached is not None:
            resources = cached
        else:
            resources = self.executor.discover_scan_resources(exp.local_id, scan_id)
            if scan_resources_cache is not None:
                scan_resources_cache[scan_id] = resources

        has_dicom = any(r.get("label") == "DICOM" for r in resources)

        # Scans without DICOM won't be created by DICOM import;
        # create them explicitly before uploading non-DICOM resources.
        # 409 is tolerated: auto-archive may have already created the scan.
        if not has_dicom and not dicom_only:
            scan_type = scan.get("type", "")
            try:
                self.executor.create_scan(
                    dest_project=dest_project,
                    dest_subject=subject.local_label,
                    dest_experiment=exp.local_label,
                    scan_id=scan_id,
                    scan_type=scan_type,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    logger.debug(
                        "Scan %s already exists on destination, continuing",
                        scan_id,
                    )
                else:
                    raise

        for res in resources:
            res_label = res.get("label", "")
            if not self.filter_engine.should_include_scan_resource(xsi_type, res_label):
                continue

            is_dicom = res_label == "DICOM"

            # Phase filtering: skip resources not matching current phase
            if is_dicom != dicom_only:
                continue

            if is_dicom:
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
            transferred = True

        return transferred

    def _wait_for_prearchive_resolution(
        self,
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        expected_scans: int,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Wait for DICOM imports to resolve from prearchive to archive.

        Args:
            exp: Experiment entity.
            dest_project: Destination project ID.
            subject: Parent subject entity.
            expected_scans: Number of DICOM scans imported.
            progress_callback: Optional progress callback.
        """
        if progress_callback:
            progress_callback(
                f"    Waiting for {expected_scans} scans to archive for {exp.local_label}..."
            )

        actual = self.executor.wait_for_archive(
            dest_project=dest_project,
            subject_label=subject.local_label,
            experiment_label=exp.local_label,
            expected_scans=expected_scans,
            timeout=self.config.archive_wait_timeout,
            interval=self.config.archive_poll_interval,
        )

        if actual < expected_scans:
            logger.warning(
                "Only %d/%d scans archived for %s; non-DICOM uploads may partially fail",
                actual,
                expected_scans,
                exp.local_label,
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
