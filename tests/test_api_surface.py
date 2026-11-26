from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

import xnatctl
from xnatctl.core import exceptions as _exceptions

_PACKAGE_ROOT = Path(xnatctl.__file__).resolve().parent

_DEPRECATED_EXCEPTION_ALIASES = ("ConnectionError", "TimeoutError", "ValidationError")


def _public_exception_classes() -> list[str]:
    return [
        name
        for name, obj in inspect.getmembers(_exceptions, inspect.isclass)
        if not name.startswith("_")
        and issubclass(obj, _exceptions.XNATCtlError)
        and obj.__module__ == _exceptions.__name__
    ]


def test_no_pycache_only_directories() -> None:
    offenders: list[str] = []
    for pycache in _PACKAGE_ROOT.rglob("__pycache__"):
        parent = pycache.parent
        siblings = [p for p in parent.iterdir() if p.name != "__pycache__"]
        if not siblings:
            offenders.append(str(parent.relative_to(_PACKAGE_ROOT.parent)))

    assert not offenders, f"pycache-only (orphaned-module) directories found: {offenders}"


def test_uploaders_directory_is_gone() -> None:
    assert not (_PACKAGE_ROOT / "uploaders").exists()


def test_version_is_sourced_from_installed_metadata() -> None:
    assert xnatctl.__version__ == version("xnatctl")


def test_download_sites_use_the_public_client_surface() -> None:
    forbidden = ("_get_client()", "_get_cookies()", "_get_auth(")
    offenders: list[str] = []
    for area in ("services", "cli"):
        for source in (_PACKAGE_ROOT / area).rglob("*.py"):
            text = source.read_text()
            for token in forbidden:
                if token in text:
                    offenders.append(f"{source.relative_to(_PACKAGE_ROOT.parent)}: {token}")

    assert not offenders, f"private XNATClient access outside core/: {offenders}"


def test_no_string_matching_on_exception_messages() -> None:
    offenders: list[str] = []
    for source in _PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if not isinstance(op, ast.In):
                    continue
                calls_str = any(
                    isinstance(descendant, ast.Call)
                    and isinstance(descendant.func, ast.Name)
                    and descendant.func.id == "str"
                    for descendant in ast.walk(comparator)
                )
                if calls_str:
                    rel = source.relative_to(_PACKAGE_ROOT.parent)
                    offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, f"string-matching on exception messages found: {offenders}"


def test_all_entries_resolve() -> None:
    missing = [name for name in xnatctl.__all__ if not hasattr(xnatctl, name)]
    assert not missing, f"__all__ names with no attribute: {missing}"


@pytest.mark.parametrize("name", _public_exception_classes())
def test_every_exception_class_is_top_level_importable(name: str) -> None:
    assert hasattr(xnatctl, name), f"{name} is not exported from xnatctl"
    assert name in xnatctl.__all__, f"{name} is missing from __all__"
    assert getattr(xnatctl, name) is getattr(_exceptions, name)


@pytest.mark.parametrize("name", _DEPRECATED_EXCEPTION_ALIASES)
def test_deprecated_aliases_warn_on_instantiation(name: str) -> None:
    cls = getattr(xnatctl, name)
    with pytest.warns(DeprecationWarning):
        if name == "TimeoutError":
            cls("x", 1)
        else:
            cls("x")


def test_import_is_clean_under_deprecation_warnings_as_errors() -> None:
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import xnatctl"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
