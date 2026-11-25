from __future__ import annotations

import zipfile
from pathlib import Path

from click.testing import CliRunner

from xnatctl.cli.main import cli


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_extract_strips_the_top_folder_and_skips_unsafe_members(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    _write_zip(
        root / "XNAT_E1.zip",
        {
            "XNAT_E1/scans/": b"",
            "XNAT_E1/scans/1/files/a.dcm": b"dcm",
            "XNAT_E1/.hidden/x.dcm": b"hidden",
            "top.txt": b"no top folder",
            "XNAT_E1/../../evil.txt": b"pwned",
        },
    )

    result = CliRunner().invoke(cli, ["local", "extract", str(root), "--no-cleanup"])

    assert result.exit_code == 0, result.output
    assert (root / "scans" / "1" / "files" / "a.dcm").read_bytes() == b"dcm"
    assert not (root / ".hidden").exists()
    assert not (root / "top.txt").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert (root / "XNAT_E1.zip").exists()
    assert "Extracted 1 ZIP file(s)" in result.output


def test_extract_removes_the_zip_by_default(tmp_path: Path) -> None:
    _write_zip(tmp_path / "XNAT_E1.zip", {"XNAT_E1/scans/1/files/a.dcm": b"dcm"})

    result = CliRunner().invoke(cli, ["local", "extract", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "XNAT_E1.zip").exists()
    assert (tmp_path / "scans" / "1" / "files" / "a.dcm").read_bytes() == b"dcm"
    assert "Removed XNAT_E1.zip" in result.output
