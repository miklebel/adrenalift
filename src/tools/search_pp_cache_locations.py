"""
Search for PP Table cache locations in registry and on disk
=============================================================

The AMD Windows driver stores/caches PP table data in several places:

  Registry:
    - PP_PhmSoftPowerPlayTable  (SPPT override - user/tool written)
    - PP_PhmPowerPlayTable      (full PP table cache)
    - PP_ODPerformanceLevels    (OD clock levels)
    - PP_PowerTableOverride     (binary blob)
    - Other PP_* binary values

  Disk (Adrenalin / driver cache):
    - %LOCALAPPDATA%/AMD/CN/               (Adrenalin profiles/cache)
    - %PROGRAMDATA%/AMD/                    (system-wide AMD data)
    - %LOCALAPPDATA%/AMD/Radeonsoftware/    (settings cache)
    - Driver store: C:/Windows/System32/DriverStore/FileRepository/

This script searches all known locations and reports any binary data
that contains the driver's clock values (e.g., 1900/2780/3320).

Usage (run as admin for full registry access):
  py -m src.tools.search_pp_cache_locations
  py -m src.tools.search_pp_cache_locations --clocks 1900 2780 3320
  py -m src.tools.search_pp_cache_locations --vbios-clocks 1920 2840 3320
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path
from typing import Optional

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import winreg
except ImportError:
    winreg = None


def _hex_preview(data: bytes, n: int = 32) -> str:
    return " ".join(f"{b:02X}" for b in data[:n])


def _print_section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def _print_kv(k: str, v) -> None:
    print(f"  {k:40s}: {v}")


def _search_blob_for_clocks(blob: bytes, clocks: list[tuple[int, int, int]],
                             label: str = "") -> list[tuple[int, int, int, int]]:
    """Search a binary blob for clock triples. Returns (offset, base, game, boost) list."""
    results = []
    for base, game, boost in clocks:
        pattern = struct.pack("<3H", base, game, boost)
        pos = 0
        while True:
            idx = blob.find(pattern, pos)
            if idx < 0:
                break
            results.append((idx, base, game, boost))
            pos = idx + 2
    return results


# ---------------------------------------------------------------------------
# Registry search
# ---------------------------------------------------------------------------

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID

_PP_REG_NAMES = [
    "PP_PhmSoftPowerPlayTable",
    "PP_PhmPowerPlayTable",
    "PP_ODPerformanceLevels",
    "PP_PowerTableOverride",
    "PP_PhmPowerPlayTableRaw",
]


def _search_registry(driver_clocks: list[tuple[int, int, int]],
                      vbios_clocks: list[tuple[int, int, int]]) -> None:
    """Search AMD adapter registry keys for PP-related binary values."""
    _print_section("Registry Search")

    if winreg is None:
        print("  winreg not available (Windows only)")
        return

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_PATH, 0,
                           winreg.KEY_READ) as parent:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(parent, i)
                except OSError:
                    break
                i += 1

                if not subkey_name.isdigit():
                    continue

                sub_path = _DISPLAY_CLASS_PATH + "\\" + subkey_name
                _search_adapter_key(sub_path, driver_clocks, vbios_clocks)

    except OSError as e:
        print(f"  Could not open display class key: {e}")


def _search_adapter_key(key_path: str,
                         driver_clocks: list[tuple[int, int, int]],
                         vbios_clocks: list[tuple[int, int, int]]) -> None:
    """Search a single adapter key for PP values and clock patterns."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                           winreg.KEY_READ) as k:
            mdid_val, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
            if not isinstance(mdid_val, str) or "VEN_1002" not in mdid_val.upper():
                return
    except OSError:
        return

    print(f"\n  --- AMD Adapter: {key_path} ---")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                           winreg.KEY_READ) as k:
            try:
                desc, _ = winreg.QueryValueEx(k, "DriverDesc")
                _print_kv("DriverDesc", desc)
            except OSError:
                pass

            # Enumerate ALL values, look for PP_* binary blobs
            idx = 0
            pp_values_found = []
            all_binary_values = []

            while True:
                try:
                    name, value, vtype = winreg.EnumValue(k, idx)
                except OSError:
                    break
                idx += 1

                if vtype in (winreg.REG_BINARY,) and isinstance(value, (bytes, bytearray)):
                    blob = bytes(value)
                    all_binary_values.append((name, blob))

                    if name.startswith("PP_") or name in _PP_REG_NAMES:
                        pp_values_found.append((name, blob))

            _print_kv("Total binary values", str(len(all_binary_values)))
            _print_kv("PP_* binary values", str(len(pp_values_found)))

            # Search PP_* values for clock patterns
            all_clocks = driver_clocks + vbios_clocks
            for name, blob in pp_values_found:
                hits = _search_blob_for_clocks(blob, all_clocks, name)
                print(f"\n    {name}: {len(blob)} bytes, preview: {_hex_preview(blob, 16)}")
                if hits:
                    for off, base, game, boost in hits:
                        is_vbios = (base, game, boost) in vbios_clocks
                        is_driver = (base, game, boost) in driver_clocks
                        tag = "VBIOS" if is_vbios else ("DRIVER" if is_driver else "?")
                        print(f"      MATCH at 0x{off:04X}: {base}/{game}/{boost} MHz [{tag}]")
                else:
                    print(f"      (no clock patterns found)")

            # Also search ALL binary values for clock patterns (might find unexpected caches)
            print(f"\n  --- Scanning all {len(all_binary_values)} binary values for clock patterns ---")
            for name, blob in all_binary_values:
                if name in [n for n, _ in pp_values_found]:
                    continue
                hits = _search_blob_for_clocks(blob, all_clocks, name)
                if hits:
                    for off, base, game, boost in hits:
                        is_vbios = (base, game, boost) in vbios_clocks
                        is_driver = (base, game, boost) in driver_clocks
                        tag = "VBIOS" if is_vbios else ("DRIVER" if is_driver else "?")
                        print(f"    {name}: {len(blob)} bytes — "
                              f"MATCH at 0x{off:04X}: {base}/{game}/{boost} [{tag}]")

    except OSError as e:
        print(f"  Error reading key: {e}")


# ---------------------------------------------------------------------------
# Disk search
# ---------------------------------------------------------------------------

_DISK_LOCATIONS = [
    (os.path.expandvars(r"%LOCALAPPDATA%\AMD"), "Adrenalin local data"),
    (os.path.expandvars(r"%PROGRAMDATA%\AMD"), "AMD system data"),
    (os.path.expandvars(r"%LOCALAPPDATA%\AMD\Radeonsoftware"), "Radeon Software cache"),
    (os.path.expandvars(r"%LOCALAPPDATA%\AMD\CN"), "CN (cache/profiles)"),
    (os.path.expandvars(r"%LOCALAPPDATA%\AMD\DxCache"), "DX Shader cache"),
]

_INTERESTING_EXTENSIONS = {
    ".bin", ".dat", ".cfg", ".xml", ".json", ".db", ".blob",
    ".cache", ".ini", ".profile", ".pptable", ".rom",
}

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _search_disk(driver_clocks: list[tuple[int, int, int]],
                  vbios_clocks: list[tuple[int, int, int]]) -> None:
    """Search known AMD cache directories on disk for clock patterns."""
    _print_section("Disk Search")

    # Also check for driver store
    driver_store = Path(r"C:\Windows\System32\DriverStore\FileRepository")
    amd_dirs = []
    if driver_store.exists():
        try:
            amd_dirs = [d for d in driver_store.iterdir()
                       if d.is_dir() and "amd" in d.name.lower()]
        except PermissionError:
            pass

    all_locations = list(_DISK_LOCATIONS)
    for d in amd_dirs[:3]:
        all_locations.append((str(d), f"Driver store: {d.name}"))

    all_clocks = driver_clocks + vbios_clocks
    total_files = 0
    total_hits = 0

    for base_dir, desc in all_locations:
        if not os.path.isdir(base_dir):
            _print_kv(f"{desc}", "(not found)")
            continue

        print(f"\n  --- {desc}: {base_dir} ---")
        dir_files = 0
        dir_hits = 0

        for root, dirs, files in os.walk(base_dir):
            # Skip shader caches (huge, not relevant)
            dirs[:] = [d for d in dirs if "shader" not in d.lower()
                      and "dxcache" not in d.lower()
                      and "glcache" not in d.lower()]

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                fpath = os.path.join(root, fname)

                try:
                    fsize = os.path.getsize(fpath)
                except OSError:
                    continue

                if fsize > _MAX_FILE_SIZE or fsize < 6:
                    continue

                # Check interesting files (binary or known config formats)
                if ext not in _INTERESTING_EXTENSIONS and fsize > 100000:
                    continue

                try:
                    with open(fpath, "rb") as f:
                        blob = f.read()
                except (OSError, PermissionError):
                    continue

                dir_files += 1
                hits = _search_blob_for_clocks(blob, all_clocks)
                if hits:
                    dir_hits += 1
                    rel = os.path.relpath(fpath, base_dir)
                    for off, base, game, boost in hits:
                        is_vbios = (base, game, boost) in vbios_clocks
                        tag = "VBIOS" if is_vbios else "DRIVER"
                        print(f"    MATCH: {rel} ({fsize} bytes) "
                              f"at 0x{off:04X}: {base}/{game}/{boost} [{tag}]")

        total_files += dir_files
        total_hits += dir_hits
        _print_kv(f"  Files scanned", str(dir_files))
        _print_kv(f"  Files with hits", str(dir_hits))

    print(f"\n  Total: scanned {total_files} files, {total_hits} with clock pattern hits")


# ---------------------------------------------------------------------------
# Additional registry locations (ACPI / platform power)
# ---------------------------------------------------------------------------

def _search_acpi_registry() -> None:
    """Check for ACPI/platform power limit entries that might affect clocks."""
    _print_section("ACPI / Platform Power Registry")

    if winreg is None:
        print("  winreg not available")
        return

    acpi_paths = [
        (r"SYSTEM\CurrentControlSet\Control\Power", "Power control"),
        (r"SYSTEM\CurrentControlSet\Control\Power\PowerSettings", "Power settings"),
    ]

    for path, desc in acpi_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                               winreg.KEY_READ) as k:
                _print_kv(desc, "exists")
        except OSError:
            _print_kv(desc, "not found")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Search for PP Table cache locations in registry and on disk")
    ap.add_argument("--clocks", nargs=3, type=int, default=[1900, 2780, 3320],
                    metavar=("BASE", "GAME", "BOOST"),
                    help="Driver clock values to search for (default: 1900 2780 3320)")
    ap.add_argument("--vbios-clocks", nargs=3, type=int, default=[1920, 2840, 3320],
                    metavar=("BASE", "GAME", "BOOST"),
                    help="VBIOS clock values to search for (default: 1920 2840 3320)")
    ap.add_argument("--no-disk", action="store_true",
                    help="Skip disk search")
    ap.add_argument("--no-registry", action="store_true",
                    help="Skip registry search")
    args = ap.parse_args()

    driver_clocks = [tuple(args.clocks)]
    vbios_clocks = [tuple(args.vbios_clocks)]

    print("PP Table Cache Location Search")
    print("=" * 70)
    _print_kv("Driver clocks (searching for)", f"{args.clocks[0]}/{args.clocks[1]}/{args.clocks[2]}")
    _print_kv("VBIOS clocks (searching for)", f"{args.vbios_clocks[0]}/{args.vbios_clocks[1]}/{args.vbios_clocks[2]}")

    if not args.no_registry:
        _search_registry(driver_clocks, vbios_clocks)

    if not args.no_disk:
        _search_disk(driver_clocks, vbios_clocks)

    _search_acpi_registry()

    _print_section("SUMMARY")
    print("  If PP_PhmSoftPowerPlayTable exists in registry with VBIOS clock values,")
    print("  the driver uses those on next boot instead of the SMU combo table values.")
    print()
    print("  To make the VBIOS values persist, we can write a modified PP table blob")
    print("  to PP_PhmSoftPowerPlayTable in the adapter registry key. This blob")
    print("  contains the DriverReportedClocks with the desired frequencies.")
    print()
    print("  The sppt_cache.py tool can do this:")
    print("    cache = SpptCache.from_vbios('bios/vbios.rom')")
    print("    cache.write_to_registry(adapter_key_path)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
