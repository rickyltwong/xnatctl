#!/usr/bin/env python3
"""Fail if CLAUDE.md drifts from the package tree or CLI command inventory (MAINT-03).

Two checks:

1. Every ``xnatctl/**/*.py`` module (excluding ``__pycache__`` and ubiquitous
   boilerplate) is named somewhere in CLAUDE.md's directory tree.
2. Every Click group/command reachable from the root ``cli`` group is named in
   CLAUDE.md. This is the check that catches the drift the tree-only view misses
   (e.g. ``upload-exam``/``transfer``/``local``/``xsync`` were all absent).

CLAUDE.md is gitignored in this repo (local per-checkout navigation doc), so when
it is absent -- e.g. a fresh CI checkout -- this exits 0 with a notice rather than
failing. The unit tests exercise the logic against synthetic fixtures, so coverage
does not depend on the real file being present.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "xnatctl"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Boilerplate whose basename is not itemized in the directory tree.
_SKIP_BASENAMES = {"__init__.py", "__main__.py"}


def package_py_files(package_dir: Path) -> list[Path]:
    """Return every non-cache ``*.py`` file under *package_dir*, sorted."""
    return sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts)


def missing_files(text: str, package_dir: Path) -> list[str]:
    """Return package modules whose filename is absent from *text*."""
    missing: list[str] = []
    for f in package_py_files(package_dir):
        if f.name in _SKIP_BASENAMES:
            continue
        if f.name not in text:
            missing.append(str(f.relative_to(package_dir.parent)))
    return missing


def command_names(cli_group: object) -> set[str]:
    """Return every user-facing group/command name reachable from *cli_group*.

    Underscore-prefixed names are skipped: no real command uses that form, and
    it filters out hidden test-probe commands (e.g. ``__probe_global__``) that
    the test suite registers on the shared root group at import time.
    """
    import click

    names: set[str] = set()

    def walk(cmd: object) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                if not name.startswith("_"):
                    names.add(name)
                walk(sub)

    walk(cli_group)
    return names


def missing_commands(text: str, cli_group: object) -> list[str]:
    """Return command/group names absent from *text*."""
    return sorted(name for name in command_names(cli_group) if name not in text)


def check(text: str) -> list[str]:
    """Return a list of drift problems (empty means in sync)."""
    from xnatctl.cli.main import cli

    problems: list[str] = []
    for module in missing_files(text, PACKAGE_DIR):
        problems.append(f"module not in CLAUDE.md tree: {module}")
    for name in missing_commands(text, cli):
        problems.append(f"command/group not in CLAUDE.md: {name}")
    return problems


def main() -> int:
    if not CLAUDE_MD.exists():
        print(f"check_claude_md_drift: {CLAUDE_MD.name} not found (gitignored); skipping.")
        return 0

    problems = check(CLAUDE_MD.read_text())
    if problems:
        print("CLAUDE.md is out of sync with the codebase:")
        for p in problems:
            print(f"  - {p}")
        print("\nUpdate CLAUDE.md's directory tree / command inventory to match.")
        return 1

    print("CLAUDE.md is in sync with the package tree and command inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
