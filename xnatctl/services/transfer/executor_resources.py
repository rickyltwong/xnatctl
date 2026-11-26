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
    parts = filename.split("/files/", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return Path(filename).name


class _ResourceTransferMixin(_ExecutorAttrs):
    def download_resource(
        self,
        source_path: str,
        resource_label: str,
        work_dir: Path,
    ) -> tuple[Path, int]:
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_label = validate_local_path_component(resource_label, "resource_label")
        zip_path = work_dir / f"{safe_label}.zip"

        total_bytes = stream_to_file(
            self.source, source_path, zip_path, params={"format": "zip"}
        ).bytes_written

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
        flat_zip_path, total_bytes = self.download_resource(source_path, resource_label, work_dir)
        self.upload_resource(flat_zip_path, dest_path)
        return total_bytes

    @staticmethod
    def _flatten_zip(source_zip: Path, dest_zip: Path) -> None:
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
        try:
            with zipfile.ZipFile(zip_path) as zf:
                return zf.testzip() is None
        except (zipfile.BadZipFile, OSError, zlib.error):
            return False
