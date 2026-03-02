"""Post-transfer verification for XNAT resource transfers.

Compares file counts between source and destination resources to verify
that transfers completed successfully.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerificationResult:
    """Result of a resource verification check.

    Args:
        verified: Whether source and destination match.
        source_count: Number of files on the source.
        dest_count: Number of files on the destination.
        message: Human-readable status message.
    """

    verified: bool
    source_count: int
    dest_count: int
    message: str = ""


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
