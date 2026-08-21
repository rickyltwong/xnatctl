"""Transfer executor for moving data between XNAT instances.

Handles the actual HTTP operations: creating subjects, per-scan downloads,
DICOM-zip imports with retry, non-DICOM resource uploads, and ZIP validation.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import zipfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from xnatctl.core.exceptions import ClientRequestError, ServerError, XNATConnectionError
from xnatctl.core.retry import PERMANENT_TRANSPORT_ERRORS, is_permanent_400, retry_call
from xnatctl.services.downloads import stream_to_file
from xnatctl.services.import_service import IMPORT_ENDPOINT, build_import_params
from xnatctl.services.transfer.xml_overlay import rewrite_experiment_xml

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

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


def _strip_xnat_prefix(filename: str) -> str:
    """Strip XNAT directory prefix from a ZIP entry path.

    Removes everything up to and including the ``files/`` segment,
    preserving any subdirectory structure within the resource.
    Falls back to the leaf filename if no ``files/`` segment is found.

    Args:
        filename: ZIP entry path (e.g. ``EXP/scans/1/resources/SNAP/files/img.gif``).

    Returns:
        Relative path after ``files/`` (e.g. ``img.gif``), or leaf filename.
    """
    parts = filename.split("/files/", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return Path(filename).name


def _quote_path_segment(value: str) -> str:
    """Encode a single REST path segment for XNAT service URIs."""
    return quote(value, safe="").replace(".", "%2E")


class TransferExecutor:
    """Execute individual transfer operations between two XNAT instances.

    Args:
        source_client: Authenticated source XNATClient.
        dest_client: Authenticated destination XNATClient.
    """

    def __init__(self, source_client: XNATClient, dest_client: XNATClient) -> None:
        self.source = source_client
        self.dest = dest_client

    def list_dest_subjects(self, dest_project: str) -> set[str]:
        """List all subject accession IDs on the destination project.

        Args:
            dest_project: Destination project ID.

        Returns:
            Set of subject accession IDs present on the destination.
        """
        resp = self.dest.get(
            f"/data/projects/{dest_project}/subjects",
            params={"format": "json", "columns": "ID"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return {r["ID"] for r in results if "ID" in r}

    def list_dest_experiments(self, dest_project: str) -> set[str]:
        """List all experiment accession IDs on the destination project.

        Args:
            dest_project: Destination project ID.

        Returns:
            Set of experiment accession IDs present on the destination.
        """
        resp = self.dest.get(
            f"/data/projects/{dest_project}/experiments",
            params={"format": "json", "columns": "ID"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return {r["ID"] for r in results if "ID" in r}

    def create_subject(self, dest_project: str, label: str) -> str:
        """Create a subject on the destination.

        Args:
            dest_project: Destination project ID.
            label: Subject label.

        Returns:
            Response text (usually URI of created subject).
        """
        resp = self.dest.put(f"/data/archive/projects/{dest_project}/subjects/{label}")
        return resp.text.strip()

    def create_experiment(
        self,
        dest_project: str,
        dest_subject: str,
        label: str,
        xsi_type: str,
    ) -> str:
        """Create an empty experiment on the destination.

        Args:
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            label: Experiment label.
            xsi_type: XSI type of the experiment.

        Returns:
            Response text (usually URI of created experiment).
        """
        resp = self.dest.put(
            f"/data/archive/projects/{dest_project}/subjects/{dest_subject}/experiments/{label}",
            params={"xsiType": xsi_type},
        )
        return resp.text.strip()

    def create_scan(
        self,
        dest_project: str,
        dest_subject: str,
        dest_experiment: str,
        scan_id: str,
        scan_type: str,
        xsi_type: str = "xnat:mrScanData",
    ) -> str:
        """Create an empty scan on the destination.

        Args:
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            dest_experiment: Destination experiment label.
            scan_id: Scan ID to create.
            scan_type: Scan type string.
            xsi_type: XSI type for the scan.

        Returns:
            Response text from PUT.
        """
        resp = self.dest.put(
            f"/data/projects/{dest_project}/subjects/{dest_subject}"
            f"/experiments/{dest_experiment}/scans/{scan_id}",
            params={"xsiType": xsi_type, "type": scan_type},
        )
        return resp.text.strip()

    def check_experiment_exists(self, dest_project: str, label: str) -> str | None:
        """Check if an experiment already exists on the destination.

        Args:
            dest_project: Destination project ID.
            label: Experiment label to check.

        Returns:
            Experiment ID if found, None otherwise.
        """
        resp = self.dest.get(
            f"/data/projects/{dest_project}/experiments",
            params={"format": "json", "label": label},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        if results:
            result: str = results[0].get("ID", "")
            return result
        return None

    def discover_scans(self, experiment_id: str) -> list[dict[str, Any]]:
        """List scans on a source experiment.

        Args:
            experiment_id: Source experiment accession ID.

        Returns:
            List of scan dicts with ID, type, series_description, etc.
        """
        resp = self.source.get(
            f"/data/experiments/{experiment_id}/scans",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

    def discover_scan_resources(self, experiment_id: str, scan_id: str) -> list[dict[str, Any]]:
        """List resources for a scan on the source.

        Args:
            experiment_id: Source experiment accession ID.
            scan_id: Scan ID within the experiment.

        Returns:
            List of resource dicts with label, file_count, etc.
        """
        resp = self.source.get(
            f"/data/experiments/{experiment_id}/scans/{scan_id}/resources",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

    def discover_session_resources(self, experiment_id: str) -> list[dict[str, Any]]:
        """List session-level resources on a source experiment.

        Args:
            experiment_id: Source experiment accession ID.

        Returns:
            List of resource dicts with label, file_count, etc.
        """
        resp = self.source.get(
            f"/data/experiments/{experiment_id}/resources",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

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
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", resource_label)
        zip_path = work_dir / f"scan_{scan_id}_{safe_label}.zip"
        encoded_label = _quote_path_segment(resource_label)

        stream_to_file(
            self.source,
            f"/data/experiments/{source_experiment_id}"
            f"/scans/{scan_id}/resources/{encoded_label}/files",
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
        except Exception as e:
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

    def download_resource(
        self,
        source_path: str,
        resource_label: str,
        work_dir: Path,
    ) -> tuple[Path, int]:
        """Download, validate, and flatten a resource ZIP from source.

        Downloads the resource as a ZIP, validates it, then flattens the
        XNAT directory hierarchy so files appear at the root level.

        Args:
            source_path: Source resource files REST path.
            resource_label: Resource label (for temp filename).
            work_dir: Temporary working directory.

        Returns:
            Tuple of (flat_zip_path, total_bytes_downloaded).

        Raises:
            ValueError: If ZIP validation fails.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        zip_path = work_dir / f"{resource_label}.zip"

        total_bytes = stream_to_file(
            self.source, source_path, zip_path, params={"format": "zip"}
        ).bytes_written

        # stream_to_file already enforced the Content-Length match; validate_zip
        # adds the zipfile-integrity check on top.
        if not self.validate_zip(zip_path):
            raise ValueError(
                f"ZIP validation failed for resource {resource_label}: "
                "downloaded content is not a valid ZIP"
            )

        flat_zip_path = work_dir / f"{resource_label}_flat.zip"
        try:
            self._flatten_zip(zip_path, flat_zip_path)
        finally:
            zip_path.unlink(missing_ok=True)

        return flat_zip_path, total_bytes

    def upload_resource(
        self,
        flat_zip_path: Path,
        dest_path: str,
    ) -> None:
        """Upload a flattened resource ZIP to the destination.

        Args:
            flat_zip_path: Path to the flattened ZIP file.
            dest_path: Destination resource files REST path.
        """
        try:
            with open(flat_zip_path, "rb") as f:
                self.dest.put(
                    dest_path,
                    params={"overwrite": "true", "extract": "true"},
                    data=f.read(),
                    headers={"Content-Type": "application/zip"},
                )
        finally:
            flat_zip_path.unlink(missing_ok=True)

    def transfer_resource(
        self,
        source_path: str,
        dest_path: str,
        resource_label: str,
        work_dir: Path,
    ) -> int:
        """Download a resource from source and upload to destination.

        Convenience wrapper that calls :meth:`download_resource` followed
        by :meth:`upload_resource`.

        Args:
            source_path: Source resource files REST path.
            dest_path: Destination resource files REST path.
            resource_label: Resource label (for temp filename).
            work_dir: Temporary working directory.

        Returns:
            Number of bytes transferred.

        Raises:
            ValueError: If ZIP validation fails.
        """
        flat_zip_path, total_bytes = self.download_resource(source_path, resource_label, work_dir)
        self.upload_resource(flat_zip_path, dest_path)
        return total_bytes

    @staticmethod
    def _flatten_zip(source_zip: Path, dest_zip: Path) -> None:
        """Strip XNAT directory prefix from ZIP entries.

        XNAT ZIP downloads include the full hierarchy
        (``experiment/scans/id/resources/label/files/...``).
        This strips everything up to and including the ``files/`` segment,
        preserving any subdirectory structure within the resource itself.

        Falls back to leaf filename for entries without a ``files/`` segment.

        Uses streaming copy to avoid loading entire members into memory.

        Args:
            source_zip: Path to source ZIP with nested dirs.
            dest_zip: Path to write stripped ZIP.

        Raises:
            ValueError: If duplicate relative paths are detected.
        """
        with (
            zipfile.ZipFile(source_zip, "r") as zf_in,
            zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf_out,
        ):
            seen: set[str] = set()
            for info in zf_in.infolist():
                if info.is_dir():
                    continue
                relative = _strip_xnat_prefix(info.filename)
                if not relative:
                    continue
                if relative in seen:
                    raise ValueError(f"Duplicate path '{relative}' in ZIP (from '{info.filename}')")
                seen.add(relative)
                with zf_in.open(info) as src, zf_out.open(relative, "w") as dst:
                    shutil.copyfileobj(src, dst)

    def list_prearchive_entries(self, dest_project: str) -> list[dict[str, Any]]:
        """List all prearchive entries for a project on the destination.

        Returns the full prearchive listing for a project. Used by
        ArchivePoller to fetch a single snapshot per poll cycle instead
        of N individual find_prearchive_entry() calls.

        Args:
            dest_project: Destination project ID.

        Returns:
            List of prearchive entry dicts with name, folderName, status, timestamp, etc.
        """
        encoded_project = _quote_path_segment(dest_project)
        resp = self.dest.get(
            f"/data/prearchive/projects/{encoded_project}",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

    def find_prearchive_entry(self, dest_project: str, session_label: str) -> dict[str, Any] | None:
        """Find a prearchive entry matching a session label on the destination.

        Args:
            dest_project: Destination project ID.
            session_label: Session label to search for.

        Returns:
            Prearchive entry dict with timestamp, status, name, etc., or None.
        """
        encoded_project = _quote_path_segment(dest_project)
        resp = self.dest.get(
            f"/data/prearchive/projects/{encoded_project}",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        for entry in results:
            if entry.get("name") == session_label or entry.get("folderName") == session_label:
                return entry
        return None

    def archive_prearchive(
        self,
        dest_project: str,
        timestamp: str,
        session_name: str,
        subject_label: str,
        experiment_label: str,
        overwrite: str | None = None,
    ) -> None:
        """Manually archive a prearchive entry on the destination.

        Args:
            dest_project: Destination project ID.
            timestamp: Prearchive entry timestamp.
            session_name: Session folder name in prearchive.
            subject_label: Subject label for archiving.
            experiment_label: Experiment label for archiving.
            overwrite: Overwrite mode (``"append"`` or ``"delete"``).
                Used to resolve prearchive CONFLICT entries.
        """
        encoded_project = _quote_path_segment(dest_project)
        encoded_timestamp = _quote_path_segment(timestamp)
        encoded_session_name = _quote_path_segment(session_name)
        encoded_subject = _quote_path_segment(subject_label)
        encoded_experiment = _quote_path_segment(experiment_label)
        src = f"/prearchive/projects/{encoded_project}/{encoded_timestamp}/{encoded_session_name}"
        data: dict[str, str] = {
            "src": src,
            "dest": (
                f"/archive/projects/{encoded_project}/subjects/{encoded_subject}"
                f"/experiments/{encoded_experiment}"
            ),
        }
        if overwrite is not None:
            data["overwrite"] = overwrite
        self.dest.post(
            "/data/services/archive",
            data=data,
        )

    def delete_prearchive_entry(
        self,
        dest_project: str,
        timestamp: str,
        session_name: str,
    ) -> None:
        """Delete a prearchive entry on the destination.

        Args:
            dest_project: Destination project ID.
            timestamp: Prearchive entry timestamp.
            session_name: Session folder name in prearchive.
        """
        encoded_project = _quote_path_segment(dest_project)
        encoded_timestamp = _quote_path_segment(timestamp)
        encoded_session_name = _quote_path_segment(session_name)
        self.dest.delete(
            f"/data/prearchive/projects/{encoded_project}/{encoded_timestamp}/{encoded_session_name}",
        )

    def count_dest_scans(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
    ) -> int:
        """Count scans in an archived experiment on the destination.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.

        Returns:
            Number of scans found.
        """
        encoded_project = _quote_path_segment(dest_project)
        encoded_subject = _quote_path_segment(subject_label)
        encoded_experiment = _quote_path_segment(experiment_label)
        resp = self.dest.get(
            f"/data/projects/{encoded_project}/subjects/{encoded_subject}"
            f"/experiments/{encoded_experiment}/scans",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return len(results)

    def fetch_experiment_xml(self, experiment_id: str) -> str:
        """Fetch experiment XML from source.

        Args:
            experiment_id: Source experiment accession ID.

        Returns:
            Raw XML string.
        """
        resp = self.source.get(
            f"/data/experiments/{experiment_id}",
            params={"format": "xml"},
        )
        return resp.text

    def apply_xml_overlay(
        self,
        source_experiment_id: str,
        dest_project: str,
        dest_subject: str,
        dest_experiment_label: str,
    ) -> None:
        """Fetch source experiment XML and overlay on destination.

        Args:
            source_experiment_id: Source experiment accession ID.
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            dest_experiment_label: Destination experiment label.
        """
        xml_text = self.fetch_experiment_xml(source_experiment_id)

        dest_experiment_id = self.check_experiment_exists(dest_project, dest_experiment_label)

        cleaned_xml = rewrite_experiment_xml(xml_text, dest_experiment_id, dest_project)

        dest_path = (
            f"/data/projects/{dest_project}/subjects/{dest_subject}"
            f"/experiments/{dest_experiment_label}"
        )
        logger.debug(
            "XML overlay PUT %s (payload %d bytes):\n%s",
            dest_path,
            len(cleaned_xml),
            cleaned_xml[:2000],
        )

        try:
            self.dest.put(
                dest_path,
                data=cleaned_xml.encode("utf-8"),
                headers={"Content-Type": "text/xml"},
            )
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            logger.error(
                "XML overlay PUT failed for %s -> %s: %s — response: %s",
                source_experiment_id,
                dest_path,
                e,
                body,
            )
            raise

        logger.info(
            "XML metadata overlay applied for %s -> %s/%s",
            source_experiment_id,
            dest_project,
            dest_experiment_label,
        )

    def _safe_count_dest_scans(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
        context: str,
    ) -> int:
        """Count dest scans, returning 0 on failure.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.
            context: Log context on failure.

        Returns:
            Scan count, or 0 if the query fails.
        """
        try:
            return self.count_dest_scans(dest_project, subject_label, experiment_label)
        except Exception as exc:
            logger.debug(
                "count_dest_scans failed for %s (%s): %s",
                experiment_label,
                context,
                exc,
            )
            return 0

    def wait_for_archive(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
        expected_scans: int,
        timeout: float = 300.0,
        interval: float = 5.0,
    ) -> int:
        """Wait for experiment scans to appear in archive after DICOM import.

        Polls the prearchive and archive until the expected number of scans
        are available, manually archiving READY entries found in prearchive.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.
            expected_scans: Number of scans expected in archive.
            timeout: Maximum seconds to wait.
            interval: Seconds between poll attempts.

        Returns:
            Actual scan count found in archive when done.
        """
        deadline = time.monotonic() + timeout
        prearchive_cleared = False

        while True:
            if not prearchive_cleared:
                prearchive_cleared = self._poll_and_resolve_prearchive(
                    dest_project, subject_label, experiment_label
                )

            if prearchive_cleared:
                actual = self._safe_count_dest_scans(
                    dest_project, subject_label, experiment_label, "polling"
                )
                if actual >= expected_scans:
                    logger.info(
                        "Archive has %d/%d scans for %s",
                        actual,
                        expected_scans,
                        experiment_label,
                    )
                    return actual

            if time.monotonic() >= deadline:
                actual = self._safe_count_dest_scans(
                    dest_project, subject_label, experiment_label, "timeout"
                )
                logger.warning(
                    "Archive wait timed out for %s: %d/%d scans after %.0fs",
                    experiment_label,
                    actual,
                    expected_scans,
                    timeout,
                )
                return actual

            time.sleep(interval)

    def _poll_and_resolve_prearchive(
        self,
        dest_project: str,
        subject_label: str,
        experiment_label: str,
    ) -> bool:
        """Fetch the prearchive entry once and resolve READY/CONFLICT status.

        Args:
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.

        Returns:
            True if the experiment has left the prearchive (no entry found).

        Raises:
            httpx.HTTPStatusError: If archiving a READY/CONFLICT entry fails.
        """
        try:
            entry = self.find_prearchive_entry(dest_project, experiment_label)
        except Exception:
            logger.debug(
                "Poll cycle error for %s, retrying next cycle",
                experiment_label,
                exc_info=True,
            )
            return False

        if entry is None:
            return True

        status = entry.get("status", "")
        if status == "RECEIVING":
            logger.debug("Prearchive entry for %s still RECEIVING, waiting...", experiment_label)
        elif status in ("READY", "CONFLICT"):
            self._archive_entry(status, entry, dest_project, subject_label, experiment_label)
        elif status == "_BUILDING":
            logger.debug("Prearchive entry for %s is building, waiting...", experiment_label)
        else:
            logger.debug(
                "Prearchive entry for %s has status=%s, waiting...",
                experiment_label,
                status,
            )
        return False

    def _archive_entry(
        self,
        status: str,
        entry: dict[str, Any],
        dest_project: str,
        subject_label: str,
        experiment_label: str,
    ) -> None:
        """Archive a READY prearchive entry, or resolve a CONFLICT one.

        A CONFLICT is resolved the same way as READY, just with
        ``overwrite="append"``. A missing timestamp skips the entry (with a
        warning for READY, silently for CONFLICT).

        Args:
            status: ``"READY"`` or ``"CONFLICT"``.
            entry: Prearchive entry dict.
            dest_project: Destination project ID.
            subject_label: Subject label.
            experiment_label: Experiment label.

        Raises:
            httpx.HTTPStatusError: If the archive POST fails.
        """
        timestamp = entry.get("timestamp", "")
        if not timestamp:
            if status == "READY":
                logger.warning(
                    "Prearchive entry for %s is READY but has no timestamp, skipping",
                    experiment_label,
                )
            return

        if status == "CONFLICT":
            logger.info("Resolving CONFLICT for %s by archiving with overwrite", experiment_label)
            error_prefix = "CONFLICT resolution failed"
            overwrite = "append"
        else:
            logger.info("Archiving prearchive entry for %s (status=READY)", experiment_label)
            error_prefix = "Archiving prearchive entry failed"
            overwrite = None

        try:
            self.archive_prearchive(
                dest_project=dest_project,
                timestamp=timestamp,
                session_name=entry.get("folderName") or entry.get("name", experiment_label),
                subject_label=subject_label,
                experiment_label=experiment_label,
                overwrite=overwrite,
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response else ""
            logger.error(
                "%s for %s: %s — response: %s",
                error_prefix,
                experiment_label,
                exc,
                body or "(no response body)",
            )
            raise
        except Exception:
            logger.error(
                "%s for %s",
                error_prefix,
                experiment_label,
                exc_info=True,
            )
            raise

    @staticmethod
    def validate_zip(zip_path: Path) -> bool:
        """Check that a downloaded file is a ZIP whose members pass their CRCs.

        Size verification against Content-Length happens in
        ``stream_to_file``; this guards the archive itself -- structure AND
        member checksums, because a same-length corrupt archive would
        otherwise be imported into the destination server.

        Args:
            zip_path: Path to the ZIP file.

        Returns:
            True if the ZIP is valid.
        """
        try:
            with zipfile.ZipFile(zip_path) as zf:
                return zf.testzip() is None
        except (zipfile.BadZipFile, OSError, zlib.error):
            # zlib.error: corruption inside a DEFLATED member surfaces from the
            # decompressor, not as BadZipFile.
            return False
