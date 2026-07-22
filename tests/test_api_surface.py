"""Guards on the package's on-disk surface (LIB-11).

These are cheap structural invariants, not behavior tests: they fail loudly if a
module is deleted but leaves a stale ``__pycache__``-only directory behind (which
still imports as an implicit namespace package and can mask refactors), and they
pin the single-source version mechanism.
"""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import xnatctl

_PACKAGE_ROOT = Path(xnatctl.__file__).resolve().parent


def test_no_pycache_only_directories() -> None:
    """No package subdirectory may contain only ``__pycache__``.

    A directory whose sole content is ``__pycache__`` is the fingerprint of a
    deleted module (e.g. the old ``xnatctl/uploaders/``): the source is gone but
    stale ``.pyc`` files linger and still import. Fail naming the offenders.
    """
    offenders: list[str] = []
    for pycache in _PACKAGE_ROOT.rglob("__pycache__"):
        parent = pycache.parent
        siblings = [p for p in parent.iterdir() if p.name != "__pycache__"]
        if not siblings:
            offenders.append(str(parent.relative_to(_PACKAGE_ROOT.parent)))

    assert not offenders, f"pycache-only (orphaned-module) directories found: {offenders}"


def test_uploaders_directory_is_gone() -> None:
    """The orphaned ``xnatctl/uploaders/`` package must not reappear."""
    assert not (_PACKAGE_ROOT / "uploaders").exists()


def test_version_is_sourced_from_installed_metadata() -> None:
    """``__version__`` comes from distribution metadata, not a hand-edited literal."""
    assert xnatctl.__version__ == version("xnatctl")
