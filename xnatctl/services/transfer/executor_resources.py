"""Non-DICOM resource transfer: download, flatten, and upload a resource ZIP.

Split out of :class:`~xnatctl.services.transfer.executor.TransferExecutor`.
Covers downloading a scan- or session-level resource as a ZIP, stripping the
XNAT directory hierarchy so files land at the resource root, uploading the
flattened ZIP to the destination, and the ZIP-integrity check both this and
the DICOM transfer mixin rely on.
"""

from __future__ import annotations

import shutil
import zipfile
import zlib
from pathlib import Path

from xnatctl.core.validation import validate_local_path_component
from xnatctl.services.downloads import stream_to_file
from xnatctl.services.transfer.executor_base import _ExecutorAttrs


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


class _ResourceTransferMixin(_ExecutorAttrs):
    """Download-flatten-upload transfer of a scan/session resource ZIP."""

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
        safe_label = validate_local_path_component(resource_label, "resource_label")
        zip_path = work_dir / f"{safe_label}.zip"

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

        flat_zip_path = work_dir / f"{safe_label}_flat.zip"
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
