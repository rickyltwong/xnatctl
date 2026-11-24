from __future__ import annotations

import subprocess
import sys


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.stdout


_HEAVY_MODULES = ("rich", "click", "httpx", "pydantic", "pydicom", "pynetdicom")


def test_import_xnatctl_does_not_load_rich_click_or_dicom() -> None:
    output = _run(
        "import sys\n"
        "import xnatctl\n"
        f"heavy = [m for m in {_HEAVY_MODULES!r} if m in sys.modules]\n"
        "print(heavy)\n"
    )
    assert output.strip() == "[]", (
        f"import xnatctl pulled in: {output.strip()} -- one of these heavy "
        "dependencies loaded eagerly instead of staying deferred behind "
        "xnatctl/__init__.py's lazy exports or a function-local import."
    )


def test_import_xnatctl_core_does_not_load_rich_click_or_dicom() -> None:
    output = _run(
        "import sys\n"
        "import xnatctl.core\n"
        f"heavy = [m for m in {_HEAVY_MODULES!r} if m in sys.modules]\n"
        "print(heavy)\n"
    )
    assert output.strip() == "[]", (
        f"import xnatctl.core pulled in: {output.strip()} -- see the comment "
        "on test_import_xnatctl_does_not_load_rich_click_or_dicom above."
    )


def test_update_check_child_import_path_stays_light() -> None:
    output = _run(
        "import sys\n"
        "import xnatctl.core.update_check\n"
        "heavy = [m for m in ('rich', 'click', 'httpx', 'pydicom', 'pynetdicom')"
        " if m in sys.modules]\n"
        "print(heavy)\n"
    )
    assert output.strip() == "[]"


def test_all_public_names_resolve_lazily() -> None:
    output = _run(
        "import sys\n"
        "import xnatctl\n"
        "import xnatctl.core\n"
        "assert 'httpx' not in sys.modules\n"
        "missing = [n for n in xnatctl.__all__ if not hasattr(xnatctl, n)]\n"
        "assert missing == [], missing\n"
        "assert set(xnatctl.__all__) <= set(dir(xnatctl))\n"
        "core_missing = [n for n in xnatctl.core.__all__ if not hasattr(xnatctl.core, n)]\n"
        "assert core_missing == [], core_missing\n"
        "assert set(xnatctl.core.__all__) <= set(dir(xnatctl.core))\n"
        "assert 'httpx' in sys.modules\n"
        "print('ok')\n"
    )
    assert output.strip() == "ok"


def test_from_xnatctl_core_import_star_names_work() -> None:
    output = _run(
        "from xnatctl.core import print_error, XNATClient, Config, AuthManager\n"
        "assert callable(print_error)\n"
        "assert XNATClient is not None\n"
        "assert Config is not None\n"
        "assert AuthManager is not None\n"
        "print('ok')\n"
    )
    assert output.strip() == "ok"


def test_repeated_attribute_access_is_cached() -> None:
    output = _run(
        "import xnatctl\n"
        "a = xnatctl.XNATClient\n"
        "b = xnatctl.XNATClient\n"
        "assert a is b\n"
        "assert 'XNATClient' in vars(xnatctl)\n"
        "print('ok')\n"
    )
    assert output.strip() == "ok"


def test_unknown_attribute_still_raises_attribute_error() -> None:
    output = _run(
        "import xnatctl\n"
        "try:\n"
        "    xnatctl.NotARealExport\n"
        "except AttributeError:\n"
        "    print('ok')\n"
        "else:\n"
        "    print('no error raised')\n"
    )
    assert output.strip() == "ok"
