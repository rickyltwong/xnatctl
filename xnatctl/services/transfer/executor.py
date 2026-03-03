"""Transfer executor for moving data between XNAT instances.

Handles the actual HTTP operations: creating subjects, per-scan downloads,
DICOM-zip imports with retry, non-DICOM resource uploads, and ZIP validation.
"""

from __future__ import annotations

import logging
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)


class TransferExecutor:
    """Execute individual transfer operations between two XNAT instances.

    Args:
        source_client: Authenticated source XNATClient.
        dest_client: Authenticated destination XNATClient.
    """

    def __init__(self, source_client: XNATClient, dest_client: XNATClient) -> None:
        self.source = source_client
        self.dest = dest_client

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
        work_dir.mkdir(parents=True, exist_ok=True)
        zip_path = work_dir / f"scan_{scan_id}_DICOM.zip"

        total_bytes, content_length = self._stream_download(
            self.source,
            f"/data/experiments/{source_experiment_id}/scans/{scan_id}/resources/DICOM/files",
            {"format": "zip"},
            zip_path,
        )

        if not self.validate_zip(zip_path, content_length):
            raise ValueError(
                f"ZIP validation failed for scan {scan_id}: "
                f"downloaded {total_bytes} bytes, expected {content_length}"
            )

        last_error: Exception | None = None
        for attempt in range(retry_count):
            try:
                with open(zip_path, "rb") as f:
                    resp = self.dest.post(
                        "/data/services/import",
                        params={
                            "import-handler": "DICOM-zip",
                            "PROJECT_ID": dest_project,
                            "SUBJECT_ID": dest_subject,
                            "EXPT_LABEL": dest_experiment_label,
                            "overwrite": "append",
                            "destination": "/archive",
                        },
                        files={"file": (zip_path.name, f, "application/zip")},
                    )
                zip_path.unlink(missing_ok=True)
                return resp.text.strip() if isinstance(resp.text, str) else str(resp)
            except Exception as e:
                last_error = e
                if attempt < retry_count - 1:
                    delay = retry_delay * (2**attempt)
                    logger.warning(
                        "Scan %s DICOM import failed (attempt %d/%d), retrying in %.1fs: %s",
                        scan_id,
                        attempt + 1,
                        retry_count,
                        delay,
                        e,
                    )
                    time.sleep(delay)

        # Retain ZIP on final failure for debugging
        logger.error(
            "Scan %s DICOM import failed after %d attempts. ZIP retained at %s for debugging.",
            scan_id,
            retry_count,
            zip_path,
        )
        raise last_error  # type: ignore[misc]

    def transfer_resource(
        self,
        source_path: str,
        dest_path: str,
        resource_label: str,
        work_dir: Path,
    ) -> int:
        """Download a resource from source and upload to destination.

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
        work_dir.mkdir(parents=True, exist_ok=True)
        zip_path = work_dir / f"{resource_label}.zip"

        total_bytes, content_length = self._stream_download(
            self.source, source_path, {"format": "zip"}, zip_path
        )

        if not self.validate_zip(zip_path, content_length):
            raise ValueError(
                f"ZIP validation failed for resource {resource_label}: "
                f"downloaded {total_bytes} bytes, expected {content_length}"
            )

        try:
            with open(zip_path, "rb") as f:
                self.dest.put(
                    dest_path,
                    params={"overwrite": "true", "extract": "true"},
                    data=f.read(),
                    headers={"Content-Type": "application/zip"},
                )
        finally:
            zip_path.unlink(missing_ok=True)

        return total_bytes

    @staticmethod
    def validate_zip(zip_path: Path, expected_size: int | None = None) -> bool:
        """Validate a downloaded ZIP file.

        Args:
            zip_path: Path to the ZIP file.
            expected_size: Expected file size from Content-Length header.

        Returns:
            True if the ZIP is valid.
        """
        if not zip_path.exists():
            return False
        if not zipfile.is_zipfile(zip_path):
            return False
        if expected_size is not None:
            actual_size = zip_path.stat().st_size
            if actual_size != expected_size:
                return False
        return True

    @staticmethod
    def _stream_download(
        client: XNATClient,
        path: str,
        params: dict[str, str],
        dest: Path,
    ) -> tuple[int, int | None]:
        """Stream a file download from an XNAT client.

        Args:
            client: XNATClient to download from.
            path: API endpoint path.
            params: Query parameters.
            dest: Local file path to write to.

        Returns:
            Tuple of (total_bytes_written, content_length_from_header).
        """
        http_client = client._get_client()
        cookies = client._get_cookies()
        total_bytes = 0
        content_length: int | None = None
        with http_client.stream("GET", path, params=params, cookies=cookies) as response:
            response.raise_for_status()
            cl_header = response.headers.get("content-length")
            if cl_header is not None:
                content_length = int(cl_header)
            with open(dest, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    f.write(chunk)
                    total_bytes += len(chunk)
        return total_bytes, content_length
