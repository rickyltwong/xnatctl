"""Tests for the CLAUDE.md drift checker.

The checker's logic is exercised against synthetic fixtures so these run in CI
even though the real CLAUDE.md is gitignored and absent there.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import click
import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_claude_md_drift.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_claude_md_drift", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = _load_module()


# ---------------------------------------------------------------------------
# missing_files
# ---------------------------------------------------------------------------


def _make_pkg(tmp_path: Path, names: list[str]) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for name in names:
        target = pkg / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n")
    return pkg


def test_missing_files_flags_absent_module(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, ["alpha.py", "beta.py"])
    text = "tree mentions alpha.py only"
    missing = drift.missing_files(text, pkg)
    assert missing == ["pkg/beta.py"]


def test_missing_files_empty_when_all_present(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, ["alpha.py", "sub/beta.py"])
    text = "has alpha.py and beta.py somewhere"
    assert drift.missing_files(text, pkg) == []


def test_missing_files_ignores_dunder_and_pycache(tmp_path: Path) -> None:
    pkg = _make_pkg(tmp_path, ["__init__.py", "__main__.py", "real.py"])
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "real.cpython-312.pyc").write_text("x")
    # Only real.py must be documented; dunders and pycache are exempt.
    assert drift.missing_files("nothing here", pkg) == ["pkg/real.py"]


# ---------------------------------------------------------------------------
# command tree
# ---------------------------------------------------------------------------


@pytest.fixture
def toy_cli() -> click.Group:
    @click.group()
    def root() -> None:
        pass

    @root.command()
    def alpha() -> None:
        pass

    @root.group()
    def sub() -> None:
        pass

    @sub.command("beta-cmd")
    def beta() -> None:
        pass

    return root


def test_command_names_walks_nested_groups(toy_cli: click.Group) -> None:
    assert drift.command_names(toy_cli) == {"alpha", "sub", "beta-cmd"}


def test_missing_commands_flags_absent_name(toy_cli: click.Group) -> None:
    text = "docs mention alpha and sub but not the nested one"
    assert drift.missing_commands(text, toy_cli) == ["beta-cmd"]


def test_missing_commands_empty_when_all_present(toy_cli: click.Group) -> None:
    text = "alpha sub beta-cmd all listed"
    assert drift.missing_commands(text, toy_cli) == []


# ---------------------------------------------------------------------------
# integration: the real CLAUDE.md must stay in sync when present
# ---------------------------------------------------------------------------


def test_real_claude_md_in_sync_if_present() -> None:
    """When CLAUDE.md exists (local checkout), it must be drift-free."""
    if not drift.CLAUDE_MD.exists():
        pytest.skip("CLAUDE.md is gitignored and absent (e.g. CI)")
    assert drift.check(drift.CLAUDE_MD.read_text()) == []
