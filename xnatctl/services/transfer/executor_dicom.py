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
    """Whether a failed import POST is worth another attempt.

    ``dest.post`` goes through ``XNATClient._request``, which raises typed
    errors -- so what reaches this predicate has already been through the
    client's own ladder (429/503 for a POST). What this ladder adds:

    * Transient import-race 400s, which the client cannot know about -- only
      the import service's 400s are retryable, discriminated by body.
    * Server-side failures the client refuses to repeat for a non-idempotent
      method (500/502/504) plus exhausted/connection failures. Re-importing
      the same DICOM ZIP with ``overwrite=append`` re-sends the same SOP
      instances, so a repeat is safe here even if the first attempt partially
      ran.
    * Local file reads (OSError) and any raw httpx failure from a
      non-XNATClient transport.

    Fail-fast cases: permanent 400s, auth/permission/not-found errors, a ZIP
    that is missing or unreadable, transport errors that cannot self-heal, and
    programming errors -- retrying a bug just makes it a slower bug. The
    connection family is deliberately retried whole even though a slice of it
    (e.g. a connect timeout the client already declared unrecoverable) is
    likely permanent: the wrapped cause is not distinguishable by type, and a
    transfer pipeline prefers a few bounded retries over dropping a scan.
    """
    if isinstance(exc, ClientRequestError):
        return exc.status_code == 400 and not is_permanent_400(exc.body)
    if isinstance(exc, FileNotFoundError | PermissionError | IsADirectoryError):
        # The local ZIP is gone or unreadable; no backoff brings it back.
        return False
    if isinstance(exc, PERMANENT_TRANSPORT_ERRORS):
        # Wrong scheme, malformed URL, redirect loop: same on every attempt.
        return False
    return isinstance(exc, XNATConnectionError | ServerError | httpx.HTTPError | OSError)


class _DicomTransferMixin(_ExecutorAttrs):
    """Download-then-import a scan's DICOM resource from source to destination."""

    def download_scan_dicom(
        self,
        source_experiment_id: str,
        scan_id: str,
        work_dir: Path,
        resource_label: str = "DICOM",
    ) -> Path:
        """Download and validate a DICOM ZIP from a source scan.

        Args:
            source_experiment_id: Source experiment accession ID.
            scan_id: Scan ID to download.
            work_dir: Temporary working directory for this scan.
            resource_label: Scan resource label containing DICOM data.

        Returns:
            Path to the validated ZIP file on disk.

        Raises:
            ValueError: If ZIP validation fails.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        # Both are server-reported (the source XNAT), not caller input, but
        # still local-path components -- validated, not mangled: a
        # character-whitelist substitution (the previous approach here) is
        # not injective, so two differently-hostile values could still
        # collide on the same local ZIP name.
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

        # stream_to_file already enforced the Content-Length match; validate_zip
        # adds the zipfile-integrity check on top.
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
        """Import a validated DICOM ZIP to the destination.

        Args:
            zip_path: Path to the validated DICOM ZIP file.
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            dest_experiment_label: Destination experiment label.
            retry_count: Number of import retries.
            retry_delay: Base delay between retries (exponential backoff).

        Returns:
            Response text from import (usually URI of imported data).

        Raises:
            Exception: If all retries exhausted.
        """
        scan_id = zip_path.stem.removeprefix("scan_").removesuffix("_DICOM")
        params = build_import_params(
            import_handler="DICOM-zip",
            project=dest_project,
            subject=dest_subject,
            session=dest_experiment_label,
            entity_keys="experiment",
            # append, never the CLI default "delete": a transfer must not wipe
            # scans that already arrived in an earlier run of the same session.
            overwrite="append",
            destination="/archive",
        )

        def _import() -> str:
            # Reopened per attempt: a retried POST must not resend an
            # exhausted file handle.
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
        except Exception as e:  # noqa: BLE001  # log-and-reraise: ZIP retained for debugging regardless of failure type, then propagates
            # Retain ZIP on failure for debugging
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
        """Download DICOM ZIP from a source scan and import to destination.

        Convenience wrapper that calls :meth:`download_scan_dicom` followed
        by :meth:`upload_scan_dicom`.

        Args:
            source_experiment_id: Source experiment accession ID.
            scan_id: Scan ID to transfer.
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            dest_experiment_label: Destination experiment label.
            work_dir: Temporary working directory for this scan.
            retry_count: Number of import retries.
            retry_delay: Base delay between retries (exponential backoff).

        Returns:
            Response text from import (usually URI of imported data).

        Raises:
            ValueError: If ZIP validation fails.
            Exception: If all retries exhausted.
        """
        zip_path = self.download_scan_dicom(source_experiment_id, scan_id, work_dir)
        return self.upload_scan_dicom(
            zip_path,
            dest_project,
            dest_subject,
            dest_experiment_label,
            retry_count,
            retry_delay,
        )
