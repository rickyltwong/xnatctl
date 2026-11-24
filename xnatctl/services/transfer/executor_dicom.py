"""DICOM scan transfer: download a scan's DICOM ZIP and import it to destination.

Split out of :class:`~xnatctl.services.transfer.executor.TransferExecutor`.
Covers the download-validate-import path for a single scan's DICOM resource,
including the import-retry predicate that discriminates transient failures
(import-race 400s, retryable server/connection errors) from permanent ones.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from xnatctl.core.exceptions import ClientRequestError, ServerError, XNATConnectionError
from xnatctl.core.retry import PERMANENT_TRANSPORT_ERRORS, is_permanent_400, retry_call
from xnatctl.core.validation import quote_path_segment, validate_local_path_component
from xnatctl.core.validation import quote_prearchive_segment as _quote_path_segment
from xnatctl.services.downloads import stream_to_file
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params
from xnatctl.services.transfer.executor_base import _ExecutorAttrs

logger = logging.getLogger(__name__)


def _retryable_import_failure(exc: Exception) -> bool:
    if isinstance(exc, ClientRequestError):
        return exc.status_code == 400 and not is_permanent_400(exc.body)
    if isinstance(exc, FileNotFoundError | PermissionError | IsADirectoryError):
        return False
    if isinstance(exc, PERMANENT_TRANSPORT_ERRORS):
        return False
    return isinstance(exc, XNATConnectionError | ServerError | httpx.HTTPError | OSError)


class _DicomTransferMixin(_ExecutorAttrs):
    def download_scan_dicom(
        self,
        source_experiment_id: str,
        scan_id: str,
        work_dir: Path,
        resource_label: str = "DICOM",
    ) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_scan_id = validate_local_path_component(scan_id, "scan_id")
        safe_label = validate_local_path_component(resource_label, "resource_label")
        zip_path = work_dir / f"scan_{safe_scan_id}_{safe_label}.zip"
        encoded_label = _quote_path_segment(resource_label)

        stream_to_file(
            self.source,
            f"/data/experiments/{quote_path_segment(source_experiment_id)}"
            f"/scans/{quote_path_segment(scan_id)}/resources/{encoded_label}/files",
            zip_path,
            params={"format": "zip"},
        )

        if not self.validate_zip(zip_path):
            raise ValueError(
                f"ZIP validation failed for scan {scan_id}/{resource_label}: "
                "downloaded content is not a valid ZIP"
            )

        return zip_path

    def upload_scan_dicom(
        self,
        zip_path: Path,
        dest_project: str,
        dest_subject: str,
        dest_experiment_label: str,
        retry_count: int = 3,
        retry_delay: float = 5.0,
    ) -> str:
        scan_id = zip_path.stem.removeprefix("scan_").removesuffix("_DICOM")
        params = build_import_params(
            import_handler="DICOM-zip",
            project=dest_project,
            subject=dest_subject,
            session=dest_experiment_label,
            entity_keys="experiment",
            overwrite="append",
            destination="/archive",
        )

        def _import() -> str:
            with open(zip_path, "rb") as f:
                resp = self.dest.post(
                    IMPORT_ENDPOINT,
                    params=params,
                    files={"file": (zip_path.name, f, "application/zip")},
                )
            return resp.text.strip() if isinstance(resp.text, str) else str(resp)

        try:
            result = retry_call(
                _import,
                retryable=_retryable_import_failure,
                max_attempts=retry_count,
                backoff_base=retry_delay,
                label=f"scan {scan_id} DICOM import",
            )
        except Exception as e:
            logger.error(
                "Scan %s DICOM import failed. ZIP retained at %s for debugging: %s",
                scan_id,
                zip_path,
                e,
            )
            raise

        zip_path.unlink(missing_ok=True)
        return result

    def transfer_scan_dicom(
        self,
        source_experiment_id: str,
        scan_id: str,
        dest_project: str,
        dest_subject: str,
        dest_experiment_label: str,
        work_dir: Path,
        retry_count: int = 3,
        retry_delay: float = 5.0,
    ) -> str:
        zip_path = self.download_scan_dicom(source_experiment_id, scan_id, work_dir)
        return self.upload_scan_dicom(
            zip_path,
            dest_project,
            dest_subject,
            dest_experiment_label,
            retry_count,
            retry_delay,
        )
