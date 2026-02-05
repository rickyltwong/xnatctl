# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building a standalone xnatctl binary."""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("xnatctl")
    + collect_submodules("pydantic")
)

a = Analysis(
    ["xnatctl/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "keyring",
        "pydicom",
        "pynetdicom",
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
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
