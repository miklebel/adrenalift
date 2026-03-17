# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for RDNA4 Overclock GUI.
Bundles as single .exe (onefile): upp, PySide6, driver DLLs, everything included.
Requires: inpoutx64.dll, WinRing0x64.dll, WinRing0x64.sys (and optionally
WinRing0x64_patched.sys) in drivers/ before building. At first run, the exe
extracts and copies drivers to its folder. Run as Administrator.
"""

import os

block_cipher = None

# Upp package for RDNA4 VBIOS parsing (sibling of driver_cache_overwrite)
upp_src = os.path.join(SPECPATH, "..", "upp", "src")
upp_available = os.path.isdir(upp_src)

# Collect InpOut32/WinRing0 driver files from drivers/ if present
driver_files = []
drivers_dir = os.path.join(SPECPATH, "drivers")
driver_names = [
    "inpoutx64.dll",
    "WinRing0x64.dll",
    "WinRing0x64.sys",
    "WinRing0x64_patched.sys",
]
for name in driver_names:
    path = os.path.join(drivers_dir, name)
    if os.path.isfile(path):
        driver_files.append((path, "."))

if not any("inpoutx64" in p for p, _ in driver_files):
    print("WARNING: inpoutx64.dll not found in drivers/. Copy driver files to drivers/ before building.")
if not upp_available:
    print("WARNING: upp package not found at ../upp/src. RDNA4 VBIOS parsing may fail when bundled.")

a = Analysis(
    ["src/app/main.py"],
    pathex=[SPECPATH, os.path.join(SPECPATH, "src")] + ([upp_src] if upp_available else []),
    binaries=[],
    datas=driver_files,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "shiboken6",
        "upp",
        "upp.decode",
        "upp.atom_gen",
        "upp.atom_gen.atombios",
        "upp.atom_gen.smu_v14_0_2_navi40",
        "src",
        "src.app",
        "src.app.main",
        "src.engine",
        "src.engine.overclock_engine",
        "src.engine.od_table",
        "src.engine.smu",
        "src.io",
        "src.io.mmio",
        "src.io.vbios_parser",
        "src.tools",
        "src.tools.reg_patch",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="RDNA4_Overclock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=None,
    manifest=os.path.join(SPECPATH, "app.manifest"),
)
