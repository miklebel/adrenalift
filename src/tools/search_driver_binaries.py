"""
Search AMD driver binaries and firmware blobs for clock value patterns.

Searches:
  - C:\Windows\System32\drivers\*.sys (AMD driver files)
  - DriverStore\FileRepository\*amd*\* (full driver packages)
  - Any .bin/.fw firmware blobs in the driver directories

Looking for little-endian uint16 patterns:
  1900/2780/3320 (driver values from SMU combo table)
  1920/2840/3320 (VBIOS original values)
  1900/2780 pair (in case stored separately)
"""

import os
import struct
import sys

DRIVER_PATTERN = struct.pack("<3H", 1900, 2780, 3320)
VBIOS_PATTERN  = struct.pack("<3H", 1920, 2840, 3320)
PAIR_PATTERN   = struct.pack("<2H", 1900, 2780)

# Also search for individual u16 values near each other
VAL_1900 = struct.pack("<H", 1900)  # 6C 07
VAL_2780 = struct.pack("<H", 2780)  # DC 0A

MAX_FILE_SIZE = 200 * 1024 * 1024


def hex_preview(data, n=32):
    return " ".join(f"{b:02X}" for b in data[:n])


def search_file(fpath, show_all_u16_hits=False):
    """Search a single file for clock patterns."""
    try:
        sz = os.path.getsize(fpath)
    except OSError:
        return
    if sz > MAX_FILE_SIZE or sz < 6:
        return

    try:
        with open(fpath, "rb") as f:
            data = f.read()
    except (PermissionError, OSError):
        return

    results = []

    for pat, label in [
        (DRIVER_PATTERN, "DRIVER 1900/2780/3320"),
        (VBIOS_PATTERN,  "VBIOS  1920/2840/3320"),
        (PAIR_PATTERN,   "PAIR   1900/2780"),
    ]:
        pos = 0
        while True:
            idx = data.find(pat, pos)
            if idx < 0:
                break
            ctx_start = max(0, idx - 8)
            ctx_end = min(len(data), idx + len(pat) + 8)
            context = data[ctx_start:ctx_end]
            results.append((label, idx, context, ctx_start))
            pos = idx + 2

    if results:
        basename = os.path.basename(fpath)
        print(f"\n  FILE: {fpath}")
        print(f"  Size: {sz:,} bytes")
        for label, off, ctx, ctx_start in results[:20]:
            ctx_hex = " ".join(f"{b:02X}" for b in ctx)
            marker_pos = off - ctx_start
            print(f"    [{label}] offset 0x{off:08X}")
            print(f"      context: {ctx_hex}")
        if len(results) > 20:
            print(f"    ... and {len(results) - 20} more hits")


def main():
    print("AMD Driver Binary Search for Clock Patterns")
    print("=" * 70)
    print(f"  Pattern 1 (driver): {hex_preview(DRIVER_PATTERN)}")
    print(f"  Pattern 2 (vbios):  {hex_preview(VBIOS_PATTERN)}")
    print(f"  Pattern 3 (pair):   {hex_preview(PAIR_PATTERN)}")

    # --- 1. System32\drivers ---
    print("\n" + "=" * 70)
    print("  Searching C:\\Windows\\System32\\drivers\\")
    print("=" * 70)

    drivers_dir = r"C:\Windows\System32\drivers"
    driver_names = ["amdkmdag.sys", "atikmpag.sys", "amdkmdap.sys",
                    "amdppm.sys", "amdsmi.sys", "amdwddmgr.sys"]
    for name in driver_names:
        fp = os.path.join(drivers_dir, name)
        if os.path.isfile(fp):
            print(f"  Checking {name}...")
            search_file(fp)
        else:
            print(f"  {name}: not found")

    # Also check all *amd* and *ati* files
    if os.path.isdir(drivers_dir):
        for fname in os.listdir(drivers_dir):
            fl = fname.lower()
            if ("amd" in fl or "ati" in fl) and fl not in [n.lower() for n in driver_names]:
                search_file(os.path.join(drivers_dir, fname))

    # --- 2. DriverStore ---
    print("\n" + "=" * 70)
    print("  Searching DriverStore AMD directories")
    print("=" * 70)

    driver_store = r"C:\Windows\System32\DriverStore\FileRepository"
    amd_gpu_dirs = []
    if os.path.isdir(driver_store):
        for d in sorted(os.listdir(driver_store)):
            dl = d.lower()
            if any(x in dl for x in ["amd", "ati", "radeon", "u0", "c0"]):
                full = os.path.join(driver_store, d)
                if os.path.isdir(full):
                    amd_gpu_dirs.append(full)

    print(f"  Found {len(amd_gpu_dirs)} candidate directories:")
    for d in amd_gpu_dirs:
        print(f"    {os.path.basename(d)}")

    for amd_dir in amd_gpu_dirs:
        print(f"\n  --- {os.path.basename(amd_dir)} ---")
        file_count = 0
        hit_count = 0
        for root, dirs, files in os.walk(amd_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                file_count += 1
                search_file(fpath)
        print(f"  ({file_count} files searched)")

    # --- 3. System32 DLLs ---
    print("\n" + "=" * 70)
    print("  Searching AMD DLLs in System32")
    print("=" * 70)

    sys32 = r"C:\Windows\System32"
    amd_dlls = []
    if os.path.isdir(sys32):
        for fname in os.listdir(sys32):
            fl = fname.lower()
            if (("amd" in fl or "ati" in fl) and
                fl.endswith((".dll", ".exe", ".cpl", ".ocx"))):
                amd_dlls.append(os.path.join(sys32, fname))

    print(f"  Found {len(amd_dlls)} AMD-related files in System32")
    for fp in sorted(amd_dlls):
        search_file(fp)
    print(f"  ({len(amd_dlls)} files searched)")

    # --- 4. Adrenalin install dir ---
    print("\n" + "=" * 70)
    print("  Searching AMD Adrenalin install directories")
    print("=" * 70)

    adrenalin_dirs = [
        os.path.expandvars(r"%PROGRAMFILES%\AMD"),
        os.path.expandvars(r"%PROGRAMFILES(x86)%\AMD"),
        r"C:\AMD",
    ]
    for base_dir in adrenalin_dirs:
        if not os.path.isdir(base_dir):
            print(f"  {base_dir}: not found")
            continue
        print(f"  Searching {base_dir}...")
        file_count = 0
        for root, dirs, files in os.walk(base_dir):
            # Skip huge shader cache dirs
            dirs[:] = [d for d in dirs if "cache" not in d.lower()]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in (".bin", ".dat", ".rom", ".fw", ".sys", ".dll",
                           ".xml", ".json", ".cfg", ".ini", ".db", ".sbin", ""):
                    fpath = os.path.join(root, fname)
                    file_count += 1
                    search_file(fpath)
        print(f"  ({file_count} files searched)")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print("  If no matches in driver binaries, the 1900/2780 values")
    print("  are computed at runtime by the SMU firmware from chip")
    print("  fusing data (RTAVFS curves), not stored in any file.")
    print()
    print("  The SMU firmware runs on the GPU's embedded processor and")
    print("  has access to hardware fuse registers that encode silicon")
    print("  quality. It derives DriverReportedClocks from those fuses.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
