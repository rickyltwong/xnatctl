"""Tests for extract_session_zips in xnatctl.services.downloads."""

import zipfile
from pathlib import Path

import pytest

from xnatctl.core.exceptions import DownloadError
from xnatctl.services.downloads import extract_session_zips


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
