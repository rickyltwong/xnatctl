"""Post-transfer verification for XNAT resource transfers.

Provides two-tier verification: scan-set comparison (Tier 1) and
per-scan file count comparison (Tier 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationResult:
    """Result of a resource verification check.

    Attributes:
        verified: Whether source and destination match.
        source_count: Number of files on the source.
        dest_count: Number of files on the destination.
        message: Human-readable status message.
        missing_scans: Scan IDs present on source but missing on dest.
        mismatched_resources: List of (scan_id, resource, src_count, dst_count).
    """

    verified: bool
    source_count: int = 0
    dest_count: int = 0
    message: str = ""
    missing_scans: tuple[str, ...] = ()
    mismatched_resources: tuple[tuple[str, str, int, int], ...] = ()


class Verifier:
    """Compare source and destination resources after transfer.

    Args:
        source_client: Authenticated client for the source XNAT instance.
        dest_client: Authenticated client for the destination XNAT instance.
    """

    def __init__(self, source_client: Any, dest_client: Any) -> None:
        self._source = source_client
        self._dest = dest_client

    def _get_file_count(self, client: Any, path: str) -> int:
        """Fetch file listing from an XNAT resource path and return the count.

        Args:
            client: XNAT client with a ``get`` method.
            path: REST path to a resource's ``/files`` endpoint.

        Returns:
            Number of files listed at the path.
        """
        resp = client.get(path, params={"format": "json"})
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return len(results)

    def _get_scan_ids(self, client: Any, experiment_path: str) -> set[str]:
        """Fetch scan IDs for an experiment.

        Args:
            client: XNAT client.
            experiment_path: REST path to the experiment.

        Returns:
            Set of scan ID strings.
        """
        resp = client.get(f"{experiment_path}/scans", params={"format": "json"})
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return {r["ID"] for r in results if "ID" in r}

    def _get_resource_labels(self, client: Any, scan_path: str) -> list[str]:
        """Fetch resource labels for a scan.

        Args:
            client: XNAT client.
            scan_path: REST path to the scan.

        Returns:
            List of resource label strings.
        """
        resp = client.get(f"{scan_path}/resources", params={"format": "json"})
        data = resp.json()
        results: list[dict[str, Any]] = data.get("ResultSet", {}).get("Result", [])
        return [r["label"] for r in results if "label" in r]

    def verify_resource(
        self,
        source_path: str,
        dest_path: str,
    ) -> VerificationResult:
        """Verify a resource transfer by comparing file counts.

        Args:
            source_path: REST path to the source resource files endpoint.
            dest_path: REST path to the destination resource files endpoint.

        Returns:
            VerificationResult indicating whether counts match.
        """
        source_count = self._get_file_count(self._source, source_path)
        dest_count = self._get_file_count(self._dest, dest_path)
        verified = source_count == dest_count

        if verified:
            message = f"Verified: {source_count} files match"
        else:
            message = (
                f"Mismatch: source has {source_count} files, destination has {dest_count} files"
            )

        return VerificationResult(
            verified=verified,
            source_count=source_count,
            dest_count=dest_count,
            message=message,
        )

    def verify_scan_set(
        self,
        source_experiment_path: str,
        dest_experiment_path: str,
    ) -> VerificationResult:
        """Compare scan ID sets between source and destination (Tier 1).

        Args:
            source_experiment_path: REST path to source experiment.
            dest_experiment_path: REST path to destination experiment.

        Returns:
            VerificationResult with missing_scans populated on mismatch.
        """
        source_scans = self._get_scan_ids(self._source, source_experiment_path)
        dest_scans = self._get_scan_ids(self._dest, dest_experiment_path)
        missing = source_scans - dest_scans

        if not missing:
            return VerificationResult(
                verified=True,
                source_count=len(source_scans),
                dest_count=len(dest_scans),
                message=f"All {len(source_scans)} scans present on destination",
            )

        return VerificationResult(
            verified=False,
            source_count=len(source_scans),
            dest_count=len(dest_scans),
            message=(f"Missing {len(missing)} scans on destination: {', '.join(sorted(missing))}"),
            missing_scans=tuple(sorted(missing)),
        )

    def verify_experiment(
        self,
        source_experiment_path: str,
        dest_experiment_path: str,
    ) -> VerificationResult:
        """Two-tier verification: scan set + per-scan file counts.

        Args:
            source_experiment_path: REST path to source experiment.
            dest_experiment_path: REST path to destination experiment.

        Returns:
            VerificationResult summarizing both tiers.
        """
        # Tier 1: scan-set comparison
        source_scans = self._get_scan_ids(self._source, source_experiment_path)
        dest_scans = self._get_scan_ids(self._dest, dest_experiment_path)
        missing = source_scans - dest_scans

        if missing:
            return VerificationResult(
                verified=False,
                source_count=len(source_scans),
                dest_count=len(dest_scans),
                message=(
                    f"Missing {len(missing)} scans on destination: {', '.join(sorted(missing))}"
                ),
                missing_scans=tuple(sorted(missing)),
            )

        # Tier 2: per-scan file count comparison (reuse source_scans)
        mismatched: list[tuple[str, str, int, int]] = []

        for scan_id in sorted(source_scans):
            src_scan_path = f"{source_experiment_path}/scans/{scan_id}"
            dst_scan_path = f"{dest_experiment_path}/scans/{scan_id}"

            src_resources = self._get_resource_labels(self._source, src_scan_path)

            for res_label in src_resources:
                src_count = self._get_file_count(
                    self._source, f"{src_scan_path}/resources/{res_label}/files"
                )
                try:
                    dst_count = self._get_file_count(
                        self._dest, f"{dst_scan_path}/resources/{res_label}/files"
                    )
                except Exception as e:
                    logger.debug(
                        "Could not fetch dest file count for scan %s/%s: %s",
                        scan_id,
                        res_label,
                        e,
                    )
                    dst_count = 0

                if src_count != dst_count:
                    mismatched.append((scan_id, res_label, src_count, dst_count))

        if not mismatched:
            return VerificationResult(
                verified=True,
                source_count=len(source_scans),
                dest_count=len(source_scans),
                message=(f"Verified: all scans and resources match ({len(source_scans)} scans)"),
            )

        details = "; ".join(f"scan {s}/{r}: {sc} vs {dc}" for s, r, sc, dc in mismatched)
        return VerificationResult(
            verified=False,
            source_count=len(source_scans),
            dest_count=len(source_scans),
            message=f"File count mismatches: {details}",
            mismatched_resources=tuple(mismatched),
        )
