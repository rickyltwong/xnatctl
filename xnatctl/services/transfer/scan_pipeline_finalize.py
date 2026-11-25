"""Experiment finalization for the transfer pipeline.

Mixin composed into :class:`~xnatctl.services.transfer.scan_pipeline.ScanPipeline`:
everything that happens to an experiment after its DICOM has archived (or when
it has none) -- shell creation, XML metadata overlay, session resource
transfer, verification, and state recording. The attributes below are
assigned once, in ``ScanPipeline.__init__``.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xnatctl.core.logging import FILE_ONLY_ATTR
from xnatctl.core.state import EntityStatus, TransferStateStore
from xnatctl.core.validation import quote_path_segment
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.discovery import DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine
from xnatctl.services.transfer.poller import DeferredExperiment
from xnatctl.services.transfer.scan_transfer import ScanTransfer
from xnatctl.services.transfer.verifier import Verifier

if TYPE_CHECKING:
    from xnatctl.services.transfer.orchestrator import TransferResult

logger = logging.getLogger(__name__)


class _ExperimentFinalizeMixin:
    executor: TransferExecutor
    filter_engine: FilterEngine
    scan_transfer: ScanTransfer
    state_store: TransferStateStore
    verifier: Verifier
    config: TransferConfig

    def _create_experiment_shell(
        self,
        dest_project: str,
        subject: DiscoveredEntity,
        exp: DiscoveredEntity,
    ) -> None:
        self.executor.create_experiment(
            dest_project,
            subject.local_label,
            exp.local_label,
            exp.xsi_type or "xnat:imageSessionData",
        )

    def _finalize_experiment(
        self,
        ctx: DeferredExperiment,
        result: TransferResult,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        try:
            exp, subject, dest_project = ctx.exp, ctx.subject, ctx.dest_project

            self._apply_xml_overlay(
                exp,
                dest_project,
                subject,
                progress_callback,
                f"    XML overlay applied for {exp.local_label}",
                "XML overlay failed for %s",
            )

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

            self._transfer_session_resources(
                exp, dest_project, subject, ctx.work_dir, result, progress_callback
            )

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
        with tempfile.TemporaryDirectory() as work_dir_str:
            work_dir = Path(work_dir_str)

            scans = self.executor.discover_scans(exp.local_id)
            scan_resources_cache: dict[str, list[dict[str, Any]]] = {}

            self._apply_xml_overlay(
                exp,
                dest_project,
                subject,
                progress_callback,
                f"    XML overlay applied for {exp.local_label}",
                "XML overlay failed for %s",
            )

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

            self._transfer_session_resources(
                exp, dest_project, subject, work_dir, result, progress_callback
            )

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

        self._save_experiment_mapping(exp, dest_project)

    def _save_experiment_mapping(
        self,
        exp: DiscoveredEntity,
        dest_project: str,
    ) -> None:
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
            logger.exception(
                "XML overlay failed for %s", exp.local_label, extra={FILE_ONLY_ATTR: True}
            )
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
        xsi_type = exp.xsi_type or ""

        try:
            resources = self.executor.discover_session_resources(exp.local_id)
        except Exception as e:
            logger.exception(
                "Session resource discovery failed for %s",
                exp.local_label,
                extra={FILE_ONLY_ATTR: True},
            )
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
                logger.exception(
                    "Session resource %s transfer failed for %s",
                    res_label,
                    exp.local_label,
                    extra={FILE_ONLY_ATTR: True},
                )
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
        src_path = f"/data/experiments/{quote_path_segment(exp.local_id)}"
        dst_path = (
            f"/data/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(subject.local_label)}"
            f"/experiments/{quote_path_segment(exp.local_label)}"
        )

        try:
            verification = self.verifier.verify_experiment(src_path, dst_path)
        except Exception as e:
            logger.exception(
                "Verification raised for %s", exp.local_label, extra={FILE_ONLY_ATTR: True}
            )
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
