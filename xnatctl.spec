# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building a standalone xnatctl binary."""

import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

is_windows = sys.platform == "win32"

hiddenimports = (
    collect_submodules("xnatctl")
    + collect_submodules("pydantic")
    + collect_submodules("rich")
)

binaries: list = []
datas: list = []
for pkg in ("pydicom", "pynetdicom"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["xnatctl/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "keyring",
        "tkinter",
        "pytest",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="xnatctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=not is_windows,
    upx=not is_windows,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
