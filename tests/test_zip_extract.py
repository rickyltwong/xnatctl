"""Tests for xnatctl.services.zip_extract: ZIP extraction into XNAT's on-disk layout.

Consolidates what were three separate files before the module split:
``_safe_extract_zip`` (the zip-slip guard shared by ``download_resource`` /
``download_scans``), ``_extract_scan_zip`` (the per-scan engine's extractor),
and ``extract_session_zips`` (the sequential session-download extractor).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from xnatctl.core.exceptions import DownloadError
from xnatctl.services.zip_extract import _extract_scan_zip, _safe_extract_zip, extract_session_zips


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


# =============================================================================
# _safe_extract_zip: zip-slip guard shared by download_resource / download_scans
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


# =============================================================================
# _extract_scan_zip tests (migrated with the engine from cli/session.py)
# =============================================================================


class TestExtractScanZip:
    """Tests for the _extract_scan_zip helper function."""

    def test_unfiltered_zip_multi_resource(self, tmp_path: Path) -> None:
        """Unfiltered ZIP with multiple resources preserves resource structure."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom data",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/thumb.jpg",
                b"jpeg data",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/brain.nii.gz",
                b"nifti data",
            )

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 3
        assert renamed == 0
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()
        assert (scan_base / "resources" / "SNAPSHOTS" / "files" / "thumb.jpg").exists()
        assert (scan_base / "resources" / "NII" / "files" / "brain.nii.gz").exists()

    def test_filtered_zip_single_resource(self, tmp_path: Path) -> None:
        """Filtered ZIP with resource_label puts all files under that label."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom data",
            )

        extracted, renamed = _extract_scan_zip(
            zip_path,
            scan_base,
            resource_label="DICOM",
        )

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()

    def test_exclude_resources(self, tmp_path: Path) -> None:
        """Excluded resources are not extracted."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/thumb.jpg",
                b"snap",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/brain.nii.gz",
                b"nifti",
            )

        extracted, _ = _extract_scan_zip(
            zip_path,
            scan_base,
            exclude_resources=frozenset({"SNAPSHOTS"}),
        )

        assert extracted == 2
        assert (scan_base / "resources" / "DICOM" / "files" / "img001.dcm").exists()
        assert (scan_base / "resources" / "NII" / "files" / "brain.nii.gz").exists()
        assert not (scan_base / "resources" / "SNAPSHOTS").exists()

    def test_exclude_multiple_resources(self, tmp_path: Path) -> None:
        """Multiple resources can be excluded simultaneously."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm",
                b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/SNAPSHOTS/files/t.jpg",
                b"snap",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/NII/files/b.nii.gz",
                b"nii",
            )

        extracted, _ = _extract_scan_zip(
            zip_path,
            scan_base,
            exclude_resources=frozenset({"SNAPSHOTS", "NII"}),
        )

        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "img.dcm").exists()

    def test_skips_hidden_files(self, tmp_path: Path) -> None:
        """Hidden files (starting with .) are not extracted."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img001.dcm",
                b"dicom",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/.DS_Store",
                b"macos",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert not (scan_base / "resources" / "DICOM" / "files" / ".DS_Store").exists()

    def test_duplicate_filenames_renamed(self, tmp_path: Path) -> None:
        """Duplicate filenames are renamed with __dup suffix."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        # Pre-create a file to trigger duplicate handling
        target = scan_base / "resources" / "DICOM" / "files"
        target.mkdir(parents=True)
        (target / "img.dcm").write_bytes(b"existing")

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm",
                b"new data",
            )

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert renamed == 1
        assert (target / "img.dcm").read_bytes() == b"existing"
        assert (target / "img__dup1.dcm").read_bytes() == b"new data"

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Path traversal attempts are silently skipped."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/../../evil.txt",
                b"evil",
            )
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/good.dcm",
                b"good",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        # Only the safe file should be extracted
        assert extracted == 1
        assert (scan_base / "resources" / "DICOM" / "files" / "good.dcm").exists()
        assert not (tmp_path / "evil.txt").exists()

    def test_unknown_label_uses_fallback(self, tmp_path: Path) -> None:
        """Files without detectable resource label use UNKNOWN."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            # No resources/ or files/ in path
            zf.writestr("some/random/path/data.dat", b"data")

        extracted, _ = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 1
        assert (
            scan_base / "resources" / "UNKNOWN" / "files" / "random" / "path" / "data.dat"
        ).exists()

    def test_empty_zip(self, tmp_path: Path) -> None:
        """Empty ZIP produces zero extractions."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w"):
            pass

        extracted, renamed = _extract_scan_zip(zip_path, scan_base)

        assert extracted == 0
        assert renamed == 0

    def test_directory_entries_skipped(self, tmp_path: Path) -> None:
        """Directory entries in ZIP are skipped."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("XNAT_E00001/scans/1/resources/DICOM/", b"")
            zf.writestr("XNAT_E00001/scans/1/resources/DICOM/files/", b"")
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/img.dcm",
                b"data",
            )

        extracted, _ = _extract_scan_zip(zip_path, scan_base)
        assert extracted == 1

    def test_preserves_binary_content(self, tmp_path: Path) -> None:
        """Binary content is preserved through extraction."""
        zip_path = tmp_path / "scan.zip"
        scan_base = tmp_path / "scans" / "1"
        binary_content = bytes(range(256))

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "XNAT_E00001/scans/1/resources/DICOM/files/binary.dcm",
                binary_content,
            )

        _extract_scan_zip(zip_path, scan_base)

        result = scan_base / "resources" / "DICOM" / "files" / "binary.dcm"
        assert result.read_bytes() == binary_content


# =============================================================================
# extract_session_zips: the sequential session-download extractor
# =============================================================================


def test_extract_strips_session_label(tmp_path: Path) -> None:
    """Test that extraction strips the first path component (session label)."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    # Create ZIP with structure: SESSION01/scans/1/resources/DICOM/files/test.dcm
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/1/resources/DICOM/files/test.dcm", b"test content")

    extract_session_zips(session_dir, cleanup=False)

    # Verify the output is stripped: session_dir/scans/1/resources/DICOM/files/test.dcm
    expected_file = session_dir / "scans" / "1" / "resources" / "DICOM" / "files" / "test.dcm"
    assert expected_file.exists()
    assert expected_file.read_bytes() == b"test content"


def test_extract_cleanup_removes_zip(tmp_path: Path) -> None:
    """Test that ZIP is deleted when cleanup=True."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/1/test.dcm", b"test")

    assert zip_path.exists()

    extract_session_zips(session_dir, cleanup=True)

    # ZIP should be removed
    assert not zip_path.exists()

    # But extracted files should exist
    extracted = session_dir / "scans" / "1" / "test.dcm"
    assert extracted.exists()


def test_extract_no_cleanup_keeps_zip(tmp_path: Path) -> None:
    """Test that ZIP is kept when cleanup=False."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/1/test.dcm", b"test")

    extract_session_zips(session_dir, cleanup=False)

    # ZIP should still exist
    assert zip_path.exists()

    # And extracted files should exist
    extracted = session_dir / "scans" / "1" / "test.dcm"
    assert extracted.exists()


def test_extract_skips_hidden_files(tmp_path: Path) -> None:
    """Test that hidden files (starting with .) are not extracted."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/.hidden", b"hidden")
        zf.writestr("SESSION01/scans/.DS_Store", b"macos")
        zf.writestr("SESSION01/.gitkeep", b"git")
        zf.writestr("SESSION01/scans/visible.dcm", b"visible")

    extract_session_zips(session_dir, cleanup=False)

    # Hidden files should not be extracted
    assert not (session_dir / "scans" / ".hidden").exists()
    assert not (session_dir / "scans" / ".DS_Store").exists()
    assert not (session_dir / ".gitkeep").exists()

    # Visible file should be extracted
    assert (session_dir / "scans" / "visible.dcm").exists()


def test_extract_handles_single_component_path(tmp_path: Path) -> None:
    """Test that files with just one path component are still extracted."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "data.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # File with only one component (no session label to strip)
        zf.writestr("file.dcm", b"single component")

    extract_session_zips(session_dir, cleanup=False)

    # Should extract as-is
    extracted = session_dir / "file.dcm"
    assert extracted.exists()
    assert extracted.read_bytes() == b"single component"


def test_extract_no_zips_is_noop(tmp_path: Path) -> None:
    """Test that empty directory does nothing."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # Create a regular file to ensure directory isn't empty
    (session_dir / "readme.txt").write_text("test")

    # Should not raise error and should be a no-op
    extract_session_zips(session_dir, cleanup=True)

    # Regular file should still exist
    assert (session_dir / "readme.txt").exists()


def test_extract_skips_directories(tmp_path: Path) -> None:
    """Test that directory entries in ZIP are skipped."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Add directory entries (ending with /)
        zf.writestr("SESSION01/", b"")
        zf.writestr("SESSION01/scans/", b"")
        zf.writestr("SESSION01/scans/1/", b"")
        # Add actual file
        zf.writestr("SESSION01/scans/1/test.dcm", b"content")

    extract_session_zips(session_dir, cleanup=False)

    # File should be extracted
    extracted = session_dir / "scans" / "1" / "test.dcm"
    assert extracted.exists()
    assert extracted.read_bytes() == b"content"


def test_extract_multiple_zips(tmp_path: Path) -> None:
    """Test extraction of multiple ZIP files in the same directory."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # Create multiple ZIPs
    zip1 = session_dir / "scans.zip"
    zip2 = session_dir / "resources_QC.zip"

    with zipfile.ZipFile(zip1, "w") as zf:
        zf.writestr("SESSION01/scans/1/file1.dcm", b"scan data")

    with zipfile.ZipFile(zip2, "w") as zf:
        zf.writestr("SESSION01/resources/QC/report.pdf", b"qc data")

    extract_session_zips(session_dir, cleanup=True)

    # Both should be extracted
    assert (session_dir / "scans" / "1" / "file1.dcm").exists()
    assert (session_dir / "resources" / "QC" / "report.pdf").exists()

    # Both ZIPs should be removed
    assert not zip1.exists()
    assert not zip2.exists()


def test_extract_handles_deep_nesting(tmp_path: Path) -> None:
    """Test extraction of deeply nested paths."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "data.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/a/b/c/d/e/f/g/deep.txt", b"deeply nested")

    extract_session_zips(session_dir, cleanup=False)

    # Should strip only first component
    extracted = session_dir / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "deep.txt"
    assert extracted.exists()
    assert extracted.read_bytes() == b"deeply nested"


def test_extract_preserves_binary_content(tmp_path: Path) -> None:
    """Test that binary content is preserved correctly."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    # Create binary content with various byte values
    binary_content = bytes(range(256))

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/1/binary.dcm", binary_content)

    extract_session_zips(session_dir, cleanup=False)

    extracted = session_dir / "scans" / "1" / "binary.dcm"
    assert extracted.exists()
    assert extracted.read_bytes() == binary_content


def test_extract_raises_on_bad_zip(tmp_path: Path) -> None:
    """A corrupt ZIP fails the extraction instead of being silently skipped."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # Create a valid ZIP
    good_zip = session_dir / "good.zip"
    with zipfile.ZipFile(good_zip, "w") as zf:
        zf.writestr("SESSION01/good.txt", b"good content")

    # Create a bad ZIP (just write garbage)
    bad_zip = session_dir / "bad.zip"
    bad_zip.write_bytes(b"not a zip file")

    with pytest.raises(DownloadError, match="bad.zip"):
        extract_session_zips(session_dir, cleanup=False)

    # Corrupt ZIP is left on disk, not cleaned up.
    assert bad_zip.exists()


def test_extract_raises_on_truncated_zip(tmp_path: Path) -> None:
    """A truncated ZIP (parseable structure, bad member CRC) fails the command.

    testzip() catches the corruption that opening the archive does not.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "truncated.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SESSION01/scans/1/data.dcm", b"a" * 4096)

    # Corrupt the stored member payload (located by content, not a guessed
    # offset -- flipping a header byte would exercise BadZipFile instead)
    # while leaving the ZIP structure parseable, so the archive opens fine
    # but fails the CRC check.
    raw = bytearray(zip_path.read_bytes())
    payload_at = raw.find(b"a" * 64)
    assert payload_at > 0, "stored payload not found in the archive bytes"
    raw[payload_at] ^= 0xFF
    zip_path.write_bytes(raw)

    with pytest.raises(DownloadError, match="truncated.zip"):
        extract_session_zips(session_dir, cleanup=True)

    # Nothing was extracted from the corrupt archive.
    assert not (session_dir / "scans").exists()


def test_extract_handles_special_characters_in_filenames(tmp_path: Path) -> None:
    """Test extraction of files with special characters in names."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    zip_path = session_dir / "scans.zip"

    with zipfile.ZipFile(zip_path, "w") as zf:
        # Files with spaces, hyphens, underscores
        zf.writestr("SESSION01/scans/1/file with spaces.dcm", b"spaces")
        zf.writestr("SESSION01/scans/1/file-with-hyphens.dcm", b"hyphens")
        zf.writestr("SESSION01/scans/1/file_with_underscores.dcm", b"underscores")

    extract_session_zips(session_dir, cleanup=False)

    assert (session_dir / "scans" / "1" / "file with spaces.dcm").exists()
    assert (session_dir / "scans" / "1" / "file-with-hyphens.dcm").exists()
    assert (session_dir / "scans" / "1" / "file_with_underscores.dcm").exists()
