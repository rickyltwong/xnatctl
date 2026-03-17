"""Transfer executor for moving data between XNAT instances.

Handles the actual HTTP operations: creating subjects, per-scan downloads,
DICOM-zip imports with retry, non-DICOM resource uploads, and ZIP validation.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as DefusedET
import httpx

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient

logger = logging.getLogger(__name__)


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
    ) -> Path:
        """Download and validate a DICOM ZIP from a source scan.

        Args:
            source_experiment_id: Source experiment accession ID.
            scan_id: Scan ID to download.
            work_dir: Temporary working directory for this scan.

        Returns:
            Path to the validated ZIP file on disk.

        Raises:
            ValueError: If ZIP validation fails.
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
            except httpx.HTTPStatusError as e:
                last_error = e
                body = e.response.text[:500] if e.response else ""
                if attempt < retry_count - 1:
                    delay = retry_delay * (2**attempt)
                    logger.warning(
                        "Scan %s DICOM import failed (attempt %d/%d), "
                        "retrying in %.1fs: %s — response: %s",
                        scan_id,
                        attempt + 1,
                        retry_count,
                        delay,
                        e,
                        body,
                    )
                    time.sleep(delay)
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
        body = ""
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response:
            body = last_error.response.text[:500]
        logger.error(
            "Scan %s DICOM import failed after %d attempts. "
            "ZIP retained at %s for debugging. Last response: %s",
            scan_id,
            retry_count,
            zip_path,
            body or "(no response body)",
        )
        raise last_error  # type: ignore[misc]

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

        total_bytes, content_length = self._stream_download(
            self.source, source_path, {"format": "zip"}, zip_path
        )

        if not self.validate_zip(zip_path, content_length):
            raise ValueError(
                f"ZIP validation failed for resource {resource_label}: "
                f"downloaded {total_bytes} bytes, expected {content_length}"
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
        resp = self.dest.get(
            f"/data/prearchive/projects/{dest_project}",
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
        resp = self.dest.get(
            f"/data/prearchive/projects/{dest_project}",
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
        params: dict[str, str] = {
            "action": "commit",
            "SOURCE": "prearchive",
            "subject": subject_label,
            "label": experiment_label,
        }
        if overwrite is not None:
            params["overwrite"] = overwrite
        self.dest.post(
            f"/data/prearchive/projects/{dest_project}/{timestamp}/{session_name}",
            params=params,
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
        resp = self.dest.get(
            f"/data/projects/{dest_project}/subjects/{subject_label}"
            f"/experiments/{experiment_label}/scans",
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

    def _rewrite_experiment_xml(
        self,
        xml_text: str,
        dest_experiment_id: str | None = None,
        dest_project: str | None = None,
    ) -> str:
        """Strip internal references from experiment XML for overlay.

        Removes file/catalog elements, subject_ID, prearchivePath,
        image_session_ID, sharing, fields, session-level resources,
        schemaLocation, and label. Rewrites experiment ID and project
        if provided.

        The label attribute is always stripped because XNAT rejects PUT
        requests that include a label differing from the destination
        experiment's current label (400: "Label must be modified through
        separate URI."). Since xnatctl currently only supports same-label
        transfers, stripping it avoids the mismatch entirely.

        .. todo:: Support regex-based label transformation. When implemented,
           accept a ``dest_label`` parameter and rewrite instead of strip.

        Args:
            xml_text: Raw source experiment XML.
            dest_experiment_id: Destination experiment accession ID.
            dest_project: Destination project ID.

        Returns:
            Cleaned XML string suitable for PUT overlay.
        """
        # Strip HTML comments (hidden_fields, internal DB refs)
        xml_text = re.sub(r"<!--.*?-->", "", xml_text, flags=re.DOTALL)

        root = DefusedET.fromstring(xml_text)

        # Collect all namespace URIs used in the document (tags + attributes)
        ns_uris: set[str] = set()
        for elem in root.iter():
            tag = elem.tag
            if tag.startswith("{"):
                ns_uris.add(tag[1 : tag.index("}")])
            for attr_name in elem.attrib:
                if attr_name.startswith("{"):
                    ns_uris.add(attr_name[1 : attr_name.index("}")])

        # Build namespace map: prefix -> URI
        ns_map: dict[str, str] = {}
        for uri in ns_uris:
            if "xnat" in uri:
                ns_map["xnat"] = uri
            elif "XMLSchema-instance" in uri:
                ns_map["xsi"] = uri

        xnat_ns = ns_map.get("xnat", "")
        xsi_ns = ns_map.get("xsi", "")

        # Elements to remove (direct children or nested within scans)
        remove_local_names = {
            "file",
            "subject_ID",
            "prearchivePath",
            "image_session_ID",
            "sharing",
            "fields",
        }

        # Remove session-level resources (but not scan-level resources)
        # Session-level resources are direct children of root
        if xnat_ns:
            for tag_name in ("resources",):
                for child in root.findall(f"{{{xnat_ns}}}{tag_name}"):
                    root.remove(child)

        # Recursively remove targeted elements
        self._remove_elements_recursive(root, remove_local_names, xnat_ns)

        # Remove xsi:schemaLocation attribute
        if xsi_ns:
            schema_attr = f"{{{xsi_ns}}}schemaLocation"
            if schema_attr in root.attrib:
                del root.attrib[schema_attr]

        # Rewrite root ID and project attributes
        if dest_experiment_id is not None and "ID" in root.attrib:
            root.attrib["ID"] = dest_experiment_id
        if dest_project is not None and "project" in root.attrib:
            root.attrib["project"] = dest_project

        # Strip label to avoid 400 "Label must be modified through separate URI"
        # TODO: rewrite label instead of stripping when label transformation is supported
        if "label" in root.attrib:
            del root.attrib["label"]

        # Register namespaces to avoid ns0/ns1 prefixes in output
        for prefix, uri in ns_map.items():
            ET.register_namespace(prefix, uri)

        return ET.tostring(root, encoding="unicode", xml_declaration=True)

    @staticmethod
    def _remove_elements_recursive(
        parent: ET.Element,
        local_names: set[str],
        xnat_ns: str,
    ) -> None:
        """Remove elements matching local names from parent and descendants.

        Args:
            parent: Parent XML element.
            local_names: Set of local tag names to remove.
            xnat_ns: XNAT namespace URI.
        """
        to_remove: list[ET.Element] = []
        for child in parent:
            tag = child.tag
            local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
            if local in local_names:
                to_remove.append(child)
            else:
                TransferExecutor._remove_elements_recursive(child, local_names, xnat_ns)
        for child in to_remove:
            parent.remove(child)

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

        cleaned_xml = self._rewrite_experiment_xml(xml_text, dest_experiment_id, dest_project)

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
            try:
                if not prearchive_cleared:
                    entry = self.find_prearchive_entry(dest_project, experiment_label)
                    if entry is not None:
                        status = entry.get("status", "")
                        if status == "RECEIVING":
                            logger.debug(
                                "Prearchive entry for %s still RECEIVING, waiting...",
                                experiment_label,
                            )
                        elif status == "READY":
                            timestamp = entry.get("timestamp", "")
                            if not timestamp:
                                logger.warning(
                                    "Prearchive entry for %s is READY but has no timestamp,"
                                    " skipping",
                                    experiment_label,
                                )
                            else:
                                logger.info(
                                    "Archiving prearchive entry for %s (status=READY)",
                                    experiment_label,
                                )
                                self.archive_prearchive(
                                    dest_project=dest_project,
                                    timestamp=timestamp,
                                    session_name=entry.get("folderName")
                                    or entry.get("name", experiment_label),
                                    subject_label=subject_label,
                                    experiment_label=experiment_label,
                                )
                        elif status == "CONFLICT":
                            timestamp = entry.get("timestamp", "")
                            if timestamp:
                                logger.info(
                                    "Resolving CONFLICT for %s by archiving with overwrite",
                                    experiment_label,
                                )
                                self.archive_prearchive(
                                    dest_project=dest_project,
                                    timestamp=timestamp,
                                    session_name=entry.get("folderName")
                                    or entry.get("name", experiment_label),
                                    subject_label=subject_label,
                                    experiment_label=experiment_label,
                                    overwrite="append",
                                )
                        elif status == "_BUILDING":
                            logger.debug(
                                "Prearchive entry for %s is building, waiting...",
                                experiment_label,
                            )
                        else:
                            logger.debug(
                                "Prearchive entry for %s has status=%s, waiting...",
                                experiment_label,
                                status,
                            )
                    else:
                        prearchive_cleared = True

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
            except Exception:
                logger.debug(
                    "Poll cycle error for %s, retrying next cycle",
                    experiment_label,
                    exc_info=True,
                )

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
