from __future__ import annotations

import pytest

from xnatctl.services.downloads.session import _scan_status


@pytest.mark.parametrize(
    ("label", "extracted", "renamed", "status"),
    [
        (None, 3, 0, ""),
        ("DICOM", 3, 0, "DICOM"),
        (None, 0, 0, "empty"),
        (None, 5, 1, "renamed 1 duplicates"),
        ("NIFTI", 0, 2, "NIFTI, empty, renamed 2 duplicates"),
    ],
    ids=["plain", "label", "empty", "renamed", "all"],
)
def test_scan_status(label: str | None, extracted: int, renamed: int, status: str) -> None:
    assert _scan_status(label, extracted, renamed) == status
