"""Archive creation and ZIP-to-TAR conversion for the REST batch transport.

Pure file-system helpers with no XNAT-specific knowledge; kept apart from
:mod:`rest_batch` so that module stays under the line budget and so archive
mechanics can be tested without any HTTP mocking.
"""

from __future__ import annotations

import contextlib
import os
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def _create_tar_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a TAR archive from files, returning size in bytes."""
    with tarfile.open(output_path, "w") as tf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            tf.add(file_path, arcname=arcname)
    return output_path.stat().st_size


def _create_zip_archive(files: list[Path], output_path: Path, base_dir: Path) -> int:
    """Create a ZIP archive from files, returning size in bytes."""
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED, allowZip64=True) as zf:
        for file_path in files:
            arcname = os.path.relpath(file_path, base_dir)
            zf.write(file_path, arcname)
    return output_path.stat().st_size


def _create_archive(
    files: list[Path],
    output_path: Path,
    base_dir: Path,
    archive_format: str,
) -> int:
    """Create an archive from files.

    Args:
        files: List of file paths to include.
        output_path: Path for the output archive.
        base_dir: Base directory for relative paths in archive.
        archive_format: Format ("tar" or "zip").

    Returns:
        Size of created archive in bytes.

    Raises:
        ValueError: If archive format is unsupported.
    """
    if archive_format == "tar":
        return _create_tar_archive(files, output_path, base_dir)
    if archive_format == "zip":
        return _create_zip_archive(files, output_path, base_dir)
    raise ValueError(f"Unsupported archive format: {archive_format}")


def _safe_mtime(date_time: tuple[int, ...]) -> float:
    """Convert ZIP date_time tuple to timestamp safely.

    Args:
        date_time: 6-tuple (year, month, day, hour, minute, second)

    Returns:
        Unix timestamp, defaulting to 0 if conversion fails or date is invalid.
    """
    try:
        year = date_time[0]
        # Validate year is in reasonable range (ZIP format supports 1980-2107)
        if year < 1980 or year > 2107:
            return 0.0
        # mktime wants a full 9-tuple: the ZIP header carries 6 fields, and the
        # trailing three (weekday, yearday, DST) are filled with 0 -- 0 for DST
        # rather than -1 so the platform resolves it instead of guessing.
        y, mo, d, h, mi, sec = date_time[:6]
        return time.mktime((y, mo, d, h, mi, sec, 0, 0, 0))
    except (ValueError, OverflowError, OSError):
        # Invalid date - use epoch
        return 0.0


def _zip_to_tar(archive_path: Path, tar_path: Path) -> None:
    """Convert ZIP archive to TAR format.

    Args:
        archive_path: Source ZIP file
        tar_path: Destination TAR file

    Raises:
        zipfile.BadZipFile: If ZIP is corrupted
        OSError: If file operations fail
    """
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Validate ZIP integrity first
            bad_file = zf.testzip()
            if bad_file:
                raise zipfile.BadZipFile(f"Corrupted file in archive: {bad_file}")

            with tarfile.open(tar_path, "w") as tf:
                for info in zf.infolist():
                    name = info.filename
                    if info.is_dir():
                        tarinfo = tarfile.TarInfo(name.rstrip("/") + "/")
                        tarinfo.type = tarfile.DIRTYPE
                        tarinfo.mtime = _safe_mtime(info.date_time)
                        tarinfo.size = 0
                        tf.addfile(tarinfo)
                        continue

                    tarinfo = tarfile.TarInfo(name)
                    tarinfo.size = info.file_size
                    tarinfo.mtime = _safe_mtime(info.date_time)
                    with zf.open(info, "r") as src:
                        tf.addfile(tarinfo, fileobj=src)
    except zipfile.BadZipFile:
        raise
    except Exception as e:
        raise OSError(f"Failed to convert ZIP to TAR: {e}") from e


def _should_zip_to_tar(archive_path: Path, zip_to_tar: bool) -> bool:
    return zip_to_tar and archive_path.suffix.lower() == ".zip"


def _maybe_zip_to_tar(
    archive_path: Path, zip_to_tar: bool
) -> contextlib.AbstractContextManager[Path]:
    """Yield the path to upload, converting ZIP to TAR when asked.

    A context manager because the converted archive lives in a temporary
    directory that must outlive the conversion but not the upload. Content type
    is derived from the yielded path's name by the uploader.
    """

    @contextlib.contextmanager
    def _converter() -> Iterator[Path]:
        if _should_zip_to_tar(archive_path, zip_to_tar):
            with tempfile.TemporaryDirectory() as temp_dir:
                tar_path = Path(temp_dir) / f"{archive_path.stem}.tar"
                _zip_to_tar(archive_path, tar_path)
                yield tar_path
        else:
            yield archive_path

    return _converter()
