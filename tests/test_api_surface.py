"""Guards on the package's on-disk surface.

These are cheap structural invariants, not behavior tests: they fail loudly if a
module is deleted but leaves a stale ``__pycache__``-only directory behind (which
still imports as an implicit namespace package and can mask refactors), and they
pin the single-source version mechanism.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

import xnatctl
from xnatctl.core import exceptions as _exceptions

_PACKAGE_ROOT = Path(xnatctl.__file__).resolve().parent

# The stdlib-shadowing aliases kept for one deprecation cycle. Instantiating
# them warns; they are exported but must not be raised internally.
_DEPRECATED_EXCEPTION_ALIASES = ("ConnectionError", "TimeoutError", "ValidationError")


def _public_exception_classes() -> list[str]:
    """Every public ``XNATCtlError`` subclass defined in ``core.exceptions``."""
    return [
        name
        for name, obj in inspect.getmembers(_exceptions, inspect.isclass)
        if not name.startswith("_")
        and issubclass(obj, _exceptions.XNATCtlError)
        and obj.__module__ == _exceptions.__name__
    ]


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


def test_download_sites_use_the_public_client_surface() -> None:
    """No service or CLI module reaches into XNATClient privates for streaming.

    ``_get_client()`` / ``_get_cookies()`` / ``_get_auth(`` are the internals
    that streamed downloads used to bypass the retry/auth/typed-error path
    with; they must stay inside ``core/``. The public ``stream()`` and
    ``stream_to_file`` are the sanctioned entry points now.
    """
    forbidden = ("_get_client()", "_get_cookies()", "_get_auth(")
    offenders: list[str] = []
    for area in ("services", "cli"):
        for source in (_PACKAGE_ROOT / area).rglob("*.py"):
            text = source.read_text()
            for token in forbidden:
                if token in text:
                    offenders.append(f"{source.relative_to(_PACKAGE_ROOT.parent)}: {token}")

    assert not offenders, f"private XNATClient access outside core/: {offenders}"


# =============================================================================
# Public export surface
# =============================================================================


def test_all_entries_resolve() -> None:
    """Every name in ``xnatctl.__all__`` is a real attribute of the package."""
    missing = [name for name in xnatctl.__all__ if not hasattr(xnatctl, name)]
    assert not missing, f"__all__ names with no attribute: {missing}"


@pytest.mark.parametrize("name", _public_exception_classes())
def test_every_exception_class_is_top_level_importable(name: str) -> None:
    """The full exception hierarchy is re-exported from the package root.

    A caller catching ``xnatctl.SomeError`` must not have to reach into
    ``xnatctl.core.exceptions``; every public class there is a top-level name.
    """
    assert hasattr(xnatctl, name), f"{name} is not exported from xnatctl"
    assert name in xnatctl.__all__, f"{name} is missing from __all__"
    assert getattr(xnatctl, name) is getattr(_exceptions, name)


@pytest.mark.parametrize("name", _DEPRECATED_EXCEPTION_ALIASES)
def test_deprecated_aliases_warn_on_instantiation(name: str) -> None:
    """The kept stdlib-shadowing aliases warn when constructed."""
    cls = getattr(xnatctl, name)
    with pytest.warns(DeprecationWarning):
        # TimeoutError takes (url, timeout); the other two take a message.
        if name == "TimeoutError":
            cls("x", 1)
        else:
            cls("x")


def test_import_is_clean_under_deprecation_warnings_as_errors() -> None:
    """``import xnatctl`` must not instantiate any deprecated alias at import time."""
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import xnatctl"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
