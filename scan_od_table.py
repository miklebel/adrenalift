"""
scan_od_table.py -- OD Table RAM Scan (Research)
================================================

Standalone script for scanning physical memory for the OD table using
SMU-extracted pattern. Uses the same strategy as PPTable scan:
probe cached addrs -> window scan -> full scan.

Usage:
  py scan_od_table.py              # Extract pattern, scan, validate
  py scan_od_table.py --dump-only  # Just dump OD from SMU (no scan)

Requires: Administrator privileges, AMD GPU with driver loaded.
"""

import sys, os

sys.stdout.reconfigure(line_buffering=True)
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from overclock_engine import (
    init_hardware, cleanup_hardware,
    extract_od_pattern, read_od, validate_od_candidate,
    scan_for_od_table, load_cached_addrs,
    ScanOptions,
)
from od_table import dump_od_table


def cli_progress(pct, msg):
    print(f"    {pct:5.1f}%  {msg}")


def main():
    if "--dump-only" in sys.argv:
        sys.argv.remove("--dump-only")
        dump_only = True
    else:
        dump_only = False

    print("=" * 62)
    print("  OD Table RAM Scan (Research)")
    print("=" * 62)

    print("\n  Initializing hardware...")
    try:
        hw = init_hardware()
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1

    try:
        smu = hw["smu"]
        virt = hw["virt"]
        inpout = hw["inpout"]

        if dump_only:
            print("\n  OD Table (from SMU):")
            od = read_od(smu, virt)
            if od is None:
                print("  ERROR: Failed to read OD table from SMU.")
                return 1
            dump_od_table(od)
            pattern = extract_od_pattern(smu, virt, 24)
            if pattern:
                print(f"\n  First 24 bytes (hex) for RAM search:")
                print("    " + pattern.hex(" "))
            return 0

        pattern = extract_od_pattern(smu, virt, 24)
        if not pattern:
            print("  ERROR: Could not read OD table from SMU.")
            return 1

        print(f"\n  Pattern ({len(pattern)} bytes): {pattern.hex(' ')}")

        scan_opts = ScanOptions()
        pptable_addrs = load_cached_addrs(max_entries=scan_opts.cache_max_addrs)
        if pptable_addrs:
            print(f"  Using {len(pptable_addrs)} cached PPTable addr(s) for proximity scan")

        result = scan_for_od_table(
            inpout, pattern,
            pptable_addrs=pptable_addrs,
            scan_opts=scan_opts,
            progress_callback=cli_progress,
        )

        if result.error and not result.valid_addrs:
            print(f"\n  {result.error}")
            return 1

        print(f"\n  Found {len(result.valid_addrs)} valid OD table(s) in RAM:")
        for i, (addr, od) in enumerate(zip(result.valid_addrs,
                                          result.valid_tables)):
            print(f"\n  Copy #{i+1} at 0x{addr:012X}:")
            print(f"    GfxclkFoffset={od.GfxclkFoffset} MHz  "
                  f"Ppt={od.Ppt}%  Tdc={od.Tdc}%")
            print(f"    Uclk {od.UclkFmin}-{od.UclkFmax} MHz  "
                  f"Fclk {od.FclkFmin}-{od.FclkFmax} MHz")

        return 0
    finally:
        cleanup_hardware(hw)


if __name__ == "__main__":
    sys.exit(main())
