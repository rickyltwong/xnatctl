"""Destination hierarchy CRUD, source discovery, and XML metadata overlay.

Split out of :class:`~xnatctl.services.transfer.executor.TransferExecutor`.
Covers listing and creating subjects/experiments/scans on the destination,
discovering scans and resources on the source, and rewriting/applying the
source experiment's XML onto the destination experiment.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from xnatctl.core.validation import quote_path_segment
from xnatctl.core.validation import quote_prearchive_segment as _quote_path_segment
from xnatctl.services.transfer.executor_base import _ExecutorAttrs
from xnatctl.services.transfer.xml_overlay import rewrite_experiment_xml

logger = logging.getLogger(__name__)


class _HierarchyMixin(_ExecutorAttrs):
    """Subject/experiment/scan CRUD, discovery, and XML overlay operations."""

    def list_dest_subjects(self, dest_project: str) -> set[str]:
        """List all subject accession IDs on the destination project.

        Args:
            dest_project: Destination project ID.

        Returns:
            Set of subject accession IDs present on the destination.
        """
        resp = self.dest.get(
            f"/data/projects/{quote_path_segment(dest_project)}/subjects",
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
            f"/data/projects/{quote_path_segment(dest_project)}/experiments",
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
        resp = self.dest.put(
            f"/data/archive/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(label)}"
        )
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
            f"/data/archive/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(dest_subject)}"
            f"/experiments/{quote_path_segment(label)}",
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
            f"/data/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(dest_subject)}"
            f"/experiments/{quote_path_segment(dest_experiment)}"
            f"/scans/{quote_path_segment(scan_id)}",
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
            f"/data/projects/{quote_path_segment(dest_project)}/experiments",
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
            f"/data/experiments/{quote_path_segment(experiment_id)}/scans",
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
            f"/data/experiments/{quote_path_segment(experiment_id)}"
            f"/scans/{quote_path_segment(scan_id)}/resources",
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
            f"/data/experiments/{quote_path_segment(experiment_id)}/resources",
            params={"format": "json"},
        )
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return results

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
            f"/data/experiments/{quote_path_segment(experiment_id)}",
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
            f"/data/projects/{quote_path_segment(dest_project)}"
            f"/subjects/{quote_path_segment(dest_subject)}"
            f"/experiments/{quote_path_segment(dest_experiment_label)}"
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
