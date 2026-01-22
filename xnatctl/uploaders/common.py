"""Common utilities for uploader modules."""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

# File extensions recognized as DICOM
DICOM_EXTENSIONS = {".dcm", ".ima", ".img", ".dicom"}


def collect_dicom_files(
    root: Path,
    *,
    include_extensionless: bool = True,
) -> List[Path]:
    """Recursively collect DICOM-like files under a root directory.

    Args:
        root: Root directory to search.
        include_extensionless: If True, include files without extensions
            (common for raw DICOM from scanners).

    Returns:
        Sorted list of file paths.

    Raises:
        ValueError: If root is not a directory.
    """
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    files: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Skip hidden files
        if path.name.startswith("."):
            continue

        suffix = path.suffix.lower()
        if suffix in DICOM_EXTENSIONS:
            files.append(path)
        elif include_extensionless and suffix == "":
            files.append(path)

    return sorted(files)


def split_into_batches(
    files: Sequence[Path],
    batch_size: int,
) -> List[List[Path]]:
    """Split files into batches of specified size.

    Args:
        files: Sequence of file paths to split.
        batch_size: Maximum files per batch.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if batch_size <= 0:
        return [list(files)]

    batches: List[List[Path]] = []
    current_batch: List[Path] = []

    for file_path in files:
        current_batch.append(file_path)
        if len(current_batch) >= batch_size:
            batches.append(current_batch)
            current_batch = []

    # Add remaining files as final batch
    if current_batch:
        batches.append(current_batch)

    return batches


def split_into_n_batches(
    files: Sequence[Path],
    num_batches: int,
) -> List[List[Path]]:
    """Split files into N roughly equal batches using round-robin.

    This is useful when you want a fixed number of parallel workers
    regardless of file count.

    Args:
        files: Sequence of file paths to split.
        num_batches: Number of batches to create.

    Returns:
        List of batches, each batch being a list of paths.
    """
    if not files:
        return []

    if num_batches <= 0:
        return [list(files)]

    actual_batches = min(num_batches, len(files))
    batches: List[List[Path]] = [[] for _ in range(actual_batches)]

    for idx, file_path in enumerate(files):
        batches[idx % actual_batches].append(file_path)

    return batches
