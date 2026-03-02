"""Transfer executor for moving data between XNAT instances.

Handles the actual HTTP operations: creating subjects, downloading
experiment ZIPs, importing via services/import, and uploading resources.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

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

    def create_empty_experiment(
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
            Response text.
        """
        resp = self.dest.put(
            f"/data/archive/projects/{dest_project}/subjects/{dest_subject}/experiments/{label}",
            params={"xsiType": xsi_type},
        )
        return resp.text.strip()

    def transfer_experiment_zip(
        self,
        source_experiment_id: str,
        dest_project: str,
        dest_subject: str,
        dest_experiment_label: str,
        work_dir: Path,
    ) -> str:
        """Download experiment as ZIP from source and import to destination.

        Args:
            source_experiment_id: Source experiment accession ID.
            dest_project: Destination project ID.
            dest_subject: Destination subject label.
            dest_experiment_label: Destination experiment label.
            work_dir: Temporary working directory.

        Returns:
            Response text from import (usually URI of imported experiment).
        """
        zip_path = work_dir / f"{source_experiment_id}.zip"

        self._stream_download(
            self.source,
            f"/data/experiments/{source_experiment_id}/scans/ALL/files",
            {"format": "zip"},
            zip_path,
        )

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
            return resp.text.strip() if isinstance(resp.text, str) else str(resp)
        finally:
            zip_path.unlink(missing_ok=True)

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
        """
        zip_path = work_dir / f"{resource_label}.zip"

        total_bytes = self._stream_download(self.source, source_path, {"format": "zip"}, zip_path)

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
    def _stream_download(
        client: XNATClient,
        path: str,
        params: dict[str, str],
        dest: Path,
    ) -> int:
        """Stream a file download from an XNAT client.

        Args:
            client: XNATClient to download from.
            path: API endpoint path.
            params: Query parameters.
            dest: Local file path to write to.

        Returns:
            Total bytes written.
        """
        http_client = client._get_client()
        cookies = client._get_cookies()
        total_bytes = 0
        with http_client.stream("GET", path, params=params, cookies=cookies) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    f.write(chunk)
                    total_bytes += len(chunk)
        return total_bytes
