from __future__ import annotations

from pathlib import Path

import pytest

from xnatctl.services.zip_extract import _resource_label_and_relpath


@pytest.mark.parametrize(
    ("member", "label", "rel"),
    [
        ("E/scans/1/resources/DICOM/files/a.dcm", "DICOM", "a.dcm"),
        ("E/scans/1/resources/DICOM/files/sub/a.dcm", "DICOM", "sub/a.dcm"),
        ("E/scans/1/resources/DICOM/files", "DICOM", None),
        ("E/scans/1/files/a.dcm", None, "a.dcm"),
        ("E/scans/1/files", None, None),
        ("E/files/resources/X/a.dcm", None, "resources/X/a.dcm"),
        ("E/a.dcm", None, "a.dcm"),
        ("a.dcm", None, "a.dcm"),
    ],
    ids=[
        "resource-layout",
        "resource-layout-nested",
        "resource-layout-no-file",
        "files-only",
        "files-only-no-file",
        "files-before-resources",
        "drop-top-folder",
        "bare-name",
    ],
)
def test_resource_label_and_relpath(member: str, label: str | None, rel: str | None) -> None:
    expected_rel = Path(rel) if rel is not None else None

    assert _resource_label_and_relpath(Path(member)) == (label, expected_rel)
