"""Zip-slip coverage for the DownloadService extraction guard.

The streaming write is covered by ``tests/test_downloads_atomic.py`` (atomic
``stream_to_file``) and the per-scan engine by ``tests/test_downloads.py``
(``download_session_fast``). What remains here is the extraction guard shared
by ``download_resource`` / ``download_scans``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from xnatctl.services.downloads import _safe_extract_zip


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


# =============================================================================
# Zip-slip guard
# =============================================================================


def test_parent_traversal_member_cannot_escape_the_extraction_root(tmp_path: Path) -> None:
    """A malicious archive must not write outside the directory the user
    asked for. Today's policy is to skip such members silently.
    """
    archive = tmp_path / "evil.zip"
    archive.write_bytes(build_zip({"../../escaped.txt": b"pwned", "safe.txt": b"fine"}))
    extract_dir = tmp_path / "out" / "nested"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "safe.txt").read_bytes() == b"fine"
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "out" / "escaped.txt").exists()


def test_absolute_path_member_cannot_escape_the_extraction_root(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        # zipfile strips a leading "/" on write, so the member name is forged
        # directly into the archive.
        info = zipfile.ZipInfo("/tmp/xnatctl_escaped.txt")
        zf.writestr(info, b"pwned")
        zf.writestr("safe.txt", b"fine")
    archive.write_bytes(payload.getvalue())
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "safe.txt").exists()
    assert not Path("/tmp/xnatctl_escaped.txt").exists()


def test_a_hostile_member_does_not_abort_the_whole_extraction(tmp_path: Path) -> None:
    """One bad entry must not cost the user the rest of a large session."""
    archive = tmp_path / "mixed.zip"
    archive.write_bytes(build_zip({"../evil.txt": b"pwned", "a.txt": b"one", "sub/b.txt": b"two"}))
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "a.txt").read_bytes() == b"one"
    assert (extract_dir / "sub" / "b.txt").read_bytes() == b"two"


def test_directory_entries_are_skipped_without_error(tmp_path: Path) -> None:
    archive = tmp_path / "dirs.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("scans/", b"")
        zf.writestr("scans/file.dcm", b"data")
    archive.write_bytes(payload.getvalue())
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "scans" / "file.dcm").read_bytes() == b"data"


def test_nested_directories_are_created_as_needed(tmp_path: Path) -> None:
    archive = tmp_path / "deep.zip"
    archive.write_bytes(build_zip({"a/b/c/d/file.dcm": b"deep"}))
    extract_dir = tmp_path / "out"

    _safe_extract_zip(archive, extract_dir)

    assert (extract_dir / "a" / "b" / "c" / "d" / "file.dcm").read_bytes() == b"deep"
