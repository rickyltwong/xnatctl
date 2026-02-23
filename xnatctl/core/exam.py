"""Exam filesystem classification utilities.

This module provides fast, single-pass classification of an "exam root" folder
into:

- DICOM-like files anywhere under the root (recursive)
- Top-level resource directories that contain no DICOM-like files
- Top-level miscellaneous files that are not DICOM-like
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DICOM_LIKE_SUFFIXES = {".dcm", ".dicom", ".ima", ".img"}


@dataclass(frozen=True)
class ExamRootClassification:
    """Classification result for an exam root directory.

    Attributes:
        dicom_files: Paths to DICOM-like files under the exam root (recursive).
        resource_dirs: Top-level directories under the exam root that contain no
            DICOM-like files.
        misc_files: Top-level non-hidden files under the exam root that are not
            DICOM-like.
    """

    dicom_files: tuple[Path, ...]
    resource_dirs: tuple[Path, ...]
    misc_files: tuple[Path, ...]


def classify_exam_root(root: Path) -> ExamRootClassification:
    """Classify an exam root directory.

    Rules:
    - Hidden entries are ignored (any path segment starting with '.')
    - DICOM-like files are detected by suffix in {'.dcm','.dicom','.ima','.img'}
      (case-insensitive) OR extensionless files (suffix == '')

    Args:
        root: Exam root directory.

    Returns:
        Classification containing DICOM-like files, resource directories, and
        miscellaneous top-level files.
    """
    root_path = Path(root)

    dicom_files: list[Path] = []
    misc_files: list[Path] = []

    # Only consider top-level directories (non-hidden). Track whether any
    # DICOM-like file appears within each.
    top_level_dir_has_dicom: dict[Path, bool] = {}

    root_str = os.fspath(root_path)
    for dirpath, dirs, files in os.walk(root_str, topdown=True):
        # Prune hidden directories in-place to avoid traversing them.
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        current_dir = Path(dirpath)
        is_root = current_dir == root_path

        if is_root and not top_level_dir_has_dicom:
            for d in dirs:
                top_level_dir_has_dicom[root_path / d] = False

        rel_dir_parts = current_dir.relative_to(root_path).parts if not is_root else ()
        top_level_dir = (root_path / rel_dir_parts[0]) if rel_dir_parts else None

        for filename in files:
            if filename.startswith("."):
                continue

            file_path = current_dir / filename
            if _is_dicom_like_file(file_path):
                dicom_files.append(file_path)
                if top_level_dir is not None and top_level_dir in top_level_dir_has_dicom:
                    top_level_dir_has_dicom[top_level_dir] = True
            elif is_root:
                misc_files.append(file_path)

    resource_dirs = [d for d, has_dicom in top_level_dir_has_dicom.items() if not has_dicom]

    return ExamRootClassification(
        dicom_files=tuple(dicom_files),
        resource_dirs=tuple(resource_dirs),
        misc_files=tuple(misc_files),
    )


def _is_dicom_like_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in _DICOM_LIKE_SUFFIXES or suffix == ""
