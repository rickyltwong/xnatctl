"""Per-scan transfer mechanics: two-phase download-then-upload of scan resources.

Split out of :class:`~xnatctl.services.transfer.scan_pipeline.ScanPipeline`,
which owns the subject/experiment-level pipelining (archive-wait overlap,
deferred finalization) and delegates the actual scan resource decomposition
and DICOM/non-DICOM transfer to :class:`ScanTransfer`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from xnatctl.core.cancellation import cancellable_pool
from xnatctl.core.logging import FILE_ONLY_ATTR
from xnatctl.core.validation import (
    check_no_casefold_collision,
    quote_path_segment,
    validate_local_path_component,
)
from xnatctl.models.transfer import TransferConfig
from xnatctl.services.transfer.discovery import DiscoveredEntity
from xnatctl.services.transfer.executor import TransferExecutor
from xnatctl.services.transfer.filter import FilterEngine

if TYPE_CHECKING:
    from xnatctl.services.transfer.orchestrator import TransferResult

logger = logging.getLogger(__name__)


def _is_dicom_resource(resource: dict[str, Any]) -> bool:
    label = str(resource.get("label") or "")
    resource_format = str(resource.get("format") or "")
    return label == "DICOM" or resource_format.upper() == "DICOM"


@dataclass
class _DownloadedScan:
    scan_id: str
    zip_path: Path
    is_dicom: bool
    resource_label: str
    dest_path: str
    dest_project: str
    dest_subject: str
    dest_experiment: str


class ScanTransfer:
    """Transfers scan resources for one experiment, DICOM and non-DICOM.

    Args:
        executor: Shared TransferExecutor for HTTP operations.
        filter_engine: Shared FilterEngine for inclusion decisions.
        config: Transfer configuration.
    """

    def __init__(
        self,
        executor: TransferExecutor,
        filter_engine: FilterEngine,
        config: TransferConfig,
    ) -> None:
        self.executor = executor
        self.filter_engine = filter_engine
        self.config = config

    def scans_have_transferable_dicom(
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
                if _is_dicom_resource(r) and self._should_include_scan_resource(
                    xsi_type,
                    r,
                ):
                    return True
        return False

    def _should_include_scan_resource(
        self,
        session_xsi_type: str,
        resource: dict[str, Any],
    ) -> bool:
        label = str(resource.get("label") or "")
        if _is_dicom_resource(resource):
            return self.filter_engine.should_include_scan_resource(
                session_xsi_type,
                "DICOM",
            )
        return self.filter_engine.should_include_scan_resource(session_xsi_type, label)

    def transfer_scans(
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
        create_missing_scans: bool = False,
    ) -> int:
        """Transfer scans for an experiment using two-phase download-then-upload.

        Phase A downloads and validates all scan resources in parallel.
        Phase B uploads all successfully downloaded resources in parallel.
        This ensures all downloads complete before any upload begins.

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
            create_missing_scans: Create/tolerate scan shells before generic
                resource upload when DICOM import is not expected to create them.

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

        self._casefold_preflight(filtered_scans)

        workers = min(self.config.scan_workers, len(filtered_scans))

        downloaded, download_failures = self._download_all_scans(
            filtered_scans,
            exp,
            dest_project,
            subject,
            work_dir,
            dicom_only,
            scan_resources_cache,
            create_missing_scans,
            workers,
        )

        self._record_download_failures(download_failures, exp, result, dicom_only)

        if not downloaded:
            return 0

        return self._upload_downloaded_scans(downloaded, exp, result, dicom_only, workers)

    @staticmethod
    def _casefold_preflight(filtered_scans: list[dict[str, Any]]) -> None:
        seen_scan_dirs: set[str] = set()
        for s in filtered_scans:
            check_no_casefold_collision(s.get("ID", ""), seen_scan_dirs, "scan_id")

    def _download_all_scans(
        self,
        filtered_scans: list[dict[str, Any]],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        dicom_only: bool,
        scan_resources_cache: dict[str, list[dict[str, Any]]],
        create_missing_scans: bool,
        workers: int,
    ) -> tuple[list[_DownloadedScan], list[tuple[str, str]]]:
        downloaded: list[_DownloadedScan] = []
        download_failures: list[tuple[str, str]] = []

        if workers > 1:
            with cancellable_pool(workers) as (pool, _token):
                futures = {
                    pool.submit(
                        self._download_scan_task,
                        s,
                        exp,
                        dest_project,
                        subject,
                        work_dir,
                        dicom_only,
                        scan_resources_cache,
                        create_missing_scans,
                    ): s.get("ID", "")
                    for s in filtered_scans
                }
                for future in as_completed(futures):
                    scan_id, items, error = future.result()
                    if items is not None:
                        downloaded.extend(items)
                    else:
                        download_failures.append((scan_id, error))
        else:
            for scan in filtered_scans:
                scan_id, items, error = self._download_scan_task(
                    scan,
                    exp,
                    dest_project,
                    subject,
                    work_dir,
                    dicom_only,
                    scan_resources_cache,
                    create_missing_scans,
                )
                if items is not None:
                    downloaded.extend(items)
                else:
                    download_failures.append((scan_id, error))

        return downloaded, download_failures

    @staticmethod
    def _record_download_failures(
        download_failures: list[tuple[str, str]],
        exp: DiscoveredEntity,
        result: TransferResult,
        dicom_only: bool,
    ) -> None:
        for scan_id, error in download_failures:
            if dicom_only:
                result.scans_failed += 1
            else:
                result.resources_failed += 1
            result.errors.append(f"Scan {scan_id} ({exp.local_label}): {error}")

    def _upload_downloaded_scans(
        self,
        downloaded: list[_DownloadedScan],
        exp: DiscoveredEntity,
        result: TransferResult,
        dicom_only: bool,
        workers: int,
    ) -> int:
        processed = 0

        if workers > 1:
            with cancellable_pool(workers) as (upload_pool, _upload_token):
                upload_futures = {
                    upload_pool.submit(self._upload_scan_task, d, exp): d.scan_id
                    for d in downloaded
                }
                for fut in as_completed(upload_futures):
                    uid, ok, uerr = fut.result()
                    if ok:
                        if dicom_only:
                            result.scans_synced += 1
                        else:
                            result.resources_synced += 1
                        processed += 1
                    else:
                        if dicom_only:
                            result.scans_failed += 1
                        else:
                            result.resources_failed += 1
                        result.errors.append(f"Scan {uid} ({exp.local_label}): {uerr}")
        else:
            for item in downloaded:
                uid, ok, uerr = self._upload_scan_task(item, exp)
                if ok:
                    if dicom_only:
                        result.scans_synced += 1
                    else:
                        result.resources_synced += 1
                    processed += 1
                else:
                    if dicom_only:
                        result.scans_failed += 1
                    else:
                        result.resources_failed += 1
                    result.errors.append(f"Scan {uid} ({exp.local_label}): {uerr}")

        return processed

    def _download_scan_task(
        self,
        scan: dict[str, Any],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        dicom_only: bool,
        scan_resources_cache: dict[str, list[dict[str, Any]]],
        create_missing_scans: bool,
    ) -> tuple[str, list[_DownloadedScan] | None, str]:
        scan_id = scan.get("ID", "")
        try:
            safe_scan_id = validate_local_path_component(scan_id, "scan_id")
            scan_work_dir = work_dir / f"scan_{safe_scan_id}"
            items = self._download_single_scan(
                scan,
                exp,
                dest_project,
                subject,
                scan_work_dir,
                dicom_only=dicom_only,
                scan_resources_cache=scan_resources_cache,
                create_missing_scan=create_missing_scans,
            )
            return scan_id, items, ""
        except Exception as e:
            logger.exception("Scan download failed", extra={FILE_ONLY_ATTR: True})
            return scan_id, None, str(e)

    def _upload_scan_task(
        self,
        item: _DownloadedScan,
        exp: DiscoveredEntity,
    ) -> tuple[str, bool, str]:
        try:
            self._upload_single_scan(item, exp)
            return item.scan_id, True, ""
        except Exception as e:
            logger.exception("Scan upload failed", extra={FILE_ONLY_ATTR: True})
            return item.scan_id, False, str(e)

    def _download_single_scan(
        self,
        scan: dict[str, Any],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        dicom_only: bool = True,
        scan_resources_cache: dict[str, list[dict[str, Any]]] | None = None,
        create_missing_scan: bool = False,
    ) -> list[_DownloadedScan]:
        scan_id = scan.get("ID", "")
        xsi_type = exp.xsi_type or ""

        resources = self._resolve_scan_resources(scan_id, exp, scan_resources_cache)

        self._ensure_scan_shell(
            scan,
            exp,
            dest_project,
            subject,
            scan_id,
            xsi_type,
            resources,
            dicom_only,
            create_missing_scan,
        )

        items: list[_DownloadedScan] = []
        seen_resource_labels: set[str] = set()
        for res, is_dicom in self._filter_scan_resources_for_phase(resources, xsi_type, dicom_only):
            check_no_casefold_collision(
                str(res.get("label", "")), seen_resource_labels, "resource label"
            )

            items.append(
                self._download_scan_resource(
                    res, scan_id, exp, dest_project, subject, work_dir, is_dicom
                )
            )

        return items

    def _resolve_scan_resources(
        self,
        scan_id: str,
        exp: DiscoveredEntity,
        scan_resources_cache: dict[str, list[dict[str, Any]]] | None,
    ) -> list[dict[str, Any]]:
        cached = (scan_resources_cache or {}).get(scan_id)
        if cached is not None:
            return cached
        resources = self.executor.discover_scan_resources(exp.local_id, scan_id)
        if scan_resources_cache is not None:
            scan_resources_cache[scan_id] = resources
        return resources

    def _filter_scan_resources_for_phase(
        self,
        resources: list[dict[str, Any]],
        xsi_type: str,
        dicom_only: bool,
    ) -> list[tuple[dict[str, Any], bool]]:
        selected: list[tuple[dict[str, Any], bool]] = []
        for res in resources:
            if not self._should_include_scan_resource(xsi_type, res):
                continue
            is_dicom = _is_dicom_resource(res)
            if is_dicom != dicom_only:
                continue
            selected.append((res, is_dicom))
        return selected

    def _ensure_scan_shell(
        self,
        scan: dict[str, Any],
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        scan_id: str,
        xsi_type: str,
        resources: list[dict[str, Any]],
        dicom_only: bool,
        create_missing_scan: bool,
    ) -> None:
        has_dicom = any(_is_dicom_resource(r) for r in resources)
        will_upload_generic_resource = not dicom_only and any(
            self._should_include_scan_resource(xsi_type, r) and not _is_dicom_resource(r)
            for r in resources
        )

        if not (will_upload_generic_resource and (create_missing_scan or not has_dicom)):
            return

        scan_type = scan.get("type", "")
        scan_xsi = (
            xsi_type.replace("SessionData", "ScanData").replace("sessionData", "scanData")
            if xsi_type
            else "xnat:mrScanData"
        )
        try:
            self.executor.create_scan(
                dest_project=dest_project,
                dest_subject=subject.local_label,
                dest_experiment=exp.local_label,
                scan_id=scan_id,
                scan_type=scan_type,
                xsi_type=scan_xsi,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                logger.debug(
                    "Scan %s already exists on destination, continuing",
                    scan_id,
                )
            else:
                raise

    def _download_scan_resource(
        self,
        res: dict[str, Any],
        scan_id: str,
        exp: DiscoveredEntity,
        dest_project: str,
        subject: DiscoveredEntity,
        work_dir: Path,
        is_dicom: bool,
    ) -> _DownloadedScan:
        res_label = res.get("label", "")

        if is_dicom:
            zip_path = self.executor.download_scan_dicom(
                source_experiment_id=exp.local_id,
                scan_id=scan_id,
                work_dir=work_dir,
                resource_label=res_label,
            )
            return _DownloadedScan(
                scan_id=scan_id,
                zip_path=zip_path,
                is_dicom=True,
                resource_label=res_label,
                dest_path="",
                dest_project=dest_project,
                dest_subject=subject.local_label,
                dest_experiment=exp.local_label,
            )

        src_path = (
            f"/data/experiments/{quote_path_segment(exp.local_id)}"
            f"/scans/{quote_path_segment(scan_id)}/resources/{quote_path_segment(res_label)}/files"
        )
        dst_path = (
            f"/data/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(subject.local_label)}"
            f"/experiments/{quote_path_segment(exp.local_label)}"
            f"/scans/{quote_path_segment(scan_id)}"
            f"/resources/{quote_path_segment(res_label)}/files"
        )
        flat_zip_path, _total = self.executor.download_resource(
            source_path=src_path,
            resource_label=f"{scan_id}_{res_label}",
            work_dir=work_dir,
        )
        return _DownloadedScan(
            scan_id=scan_id,
            zip_path=flat_zip_path,
            is_dicom=False,
            resource_label=res_label,
            dest_path=dst_path,
            dest_project=dest_project,
            dest_subject=subject.local_label,
            dest_experiment=exp.local_label,
        )

    def _upload_single_scan(
        self,
        item: _DownloadedScan,
        exp: DiscoveredEntity,
    ) -> None:
        if item.is_dicom:
            self.executor.upload_scan_dicom(
                zip_path=item.zip_path,
                dest_project=item.dest_project,
                dest_subject=item.dest_subject,
                dest_experiment_label=item.dest_experiment,
                retry_count=self.config.scan_retry_count,
                retry_delay=self.config.scan_retry_delay,
            )
        else:
            self.executor.upload_resource(
                flat_zip_path=item.zip_path,
                dest_path=item.dest_path,
            )
