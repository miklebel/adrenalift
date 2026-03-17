"""
Compare SMU Table Contents: TABLE_PPTABLE vs TABLE_COMBO_PPTABLE
==================================================================

Reads both TABLE_PPTABLE (id=0) and TABLE_COMBO_PPTABLE (id=1) from the
SMU firmware and does a byte-by-byte diff to show exactly which fields
the SMU modified when building the combo table from the VBIOS.

This is the most direct way to see what the SMU firmware changes.

The TABLE_COMBO_PPTABLE is a wrapped struct:
  struct smu_14_0_2_powerplay_table {
      struct atom_common_table_header header;
      ... wrapper fields ...
      PPTable_t smc_pptable;   // <-- this part maps to driver_pptable
  };

The TABLE_PPTABLE (id=0) is what the driver uploaded back to SMU after
copying from the combo table. They should be identical to the smc_pptable
portion of the combo table.

If the driver (or registry override) modified any values, the TABLE_PPTABLE
will differ from the combo table's smc_pptable.

Usage (run as admin):
  py -m src.tools.compare_smu_tables
  py -m src.tools.compare_smu_tables --save-raw   (save raw dumps to files)
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _hex_preview(data: bytes, n: int = 32) -> str:
    return " ".join(f"{b:02X}" for b in data[:n])


def _print_section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def _print_kv(k: str, v) -> None:
    print(f"  {k:40s}: {v}")


def _diff_blobs(a: bytes, b: bytes, label_a: str, label_b: str,
                max_diffs: int = 50) -> int:
    """Print byte-by-byte differences between two blobs."""
    min_len = min(len(a), len(b))
    diffs = 0

    print(f"\n  {'Offset':>8s}  {label_a:>20s}  {label_b:>20s}  {'u16_a':>8s}  {'u16_b':>8s}")
    print(f"  {'-' * 8}  {'-' * 20}  {'-' * 20}  {'-' * 8}  {'-' * 8}")

    for i in range(min_len):
        if a[i] != b[i]:
            diffs += 1
            if diffs <= max_diffs:
                u16_a = struct.unpack_from("<H", a, i & ~1)[0] if (i & ~1) + 2 <= len(a) else "?"
                u16_b = struct.unpack_from("<H", b, i & ~1)[0] if (i & ~1) + 2 <= len(b) else "?"
                print(f"  0x{i:06X}  {a[i]:02X} (byte {a[i]:3d})       "
                      f"  {b[i]:02X} (byte {b[i]:3d})       "
                      f"  {u16_a!s:>8s}  {u16_b!s:>8s}")

    if len(a) != len(b):
        print(f"\n  Size difference: {label_a}={len(a)} bytes, {label_b}={len(b)} bytes")

    if diffs > max_diffs:
        print(f"\n  ... and {diffs - max_diffs} more differences (showing first {max_diffs})")

    return diffs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare SMU TABLE_PPTABLE vs TABLE_COMBO_PPTABLE")
    ap.add_argument("--save-raw", action="store_true",
                    help="Save raw table dumps to files")
    ap.add_argument("--rom", default="bios/vbios.rom",
                    help="VBIOS ROM for comparison")
    ap.add_argument("--read-size", type=int, default=0x3000,
                    help="Bytes to read from DMA buffer per table")
    args = ap.parse_args()

    print("SMU Table Comparison Tool")
    print("=" * 70)

    from src.engine.overclock_engine import (
        init_hardware, cleanup_hardware, read_buf,
    )
    from src.engine.od_table import TABLE_PPTABLE, TABLE_COMBO_PPTABLE

    _print_section("Initializing hardware")
    hw = init_hardware()
    smu = hw['smu']
    virt = hw['virt']
    _print_kv("DMA path", hw['dma_path'])

    tables = {}

    for table_id, name in [(TABLE_PPTABLE, "TABLE_PPTABLE"),
                            (TABLE_COMBO_PPTABLE, "TABLE_COMBO_PPTABLE")]:
        _print_section(f"Reading {name} (id={table_id})")
        try:
            resp, ret = smu.send_msg(smu.transfer_read, table_id)
            _print_kv("SMU response", f"resp=0x{resp:X} ret=0x{ret:X}")

            raw = read_buf(virt, args.read_size)
            _print_kv("Read size", f"{len(raw)} bytes")
            _print_kv("First 32 bytes", _hex_preview(raw, 32))

            if len(raw) >= 4:
                hdr_size = struct.unpack_from("<H", raw, 0)[0]
                _print_kv("Header size field", str(hdr_size))

            tables[name] = raw

            if args.save_raw:
                fname = f"smu_{name.lower()}_raw.bin"
                with open(fname, "wb") as f:
                    f.write(raw)
                _print_kv("Saved to", fname)

        except Exception as e:
            print(f"  ERROR: {e}")

    # Also read VBIOS for triple comparison
    vbios_pp = None
    try:
        from src.tools.sppt_cache import SpptCache
        cache = SpptCache.from_vbios(args.rom)
        if cache:
            vbios_pp = cache.to_bytes()
            _print_section("VBIOS PP Table")
            _print_kv("Size", f"{len(vbios_pp)} bytes")
            _print_kv("First 32 bytes", _hex_preview(vbios_pp, 32))
            if args.save_raw:
                with open("vbios_pp_table.bin", "wb") as f:
                    f.write(vbios_pp)
    except Exception:
        pass

    # --- Diff ---
    if "TABLE_PPTABLE" in tables and "TABLE_COMBO_PPTABLE" in tables:
        _print_section("Diff: TABLE_PPTABLE vs TABLE_COMBO_PPTABLE")
        ppt = tables["TABLE_PPTABLE"]
        combo = tables["TABLE_COMBO_PPTABLE"]

        # The combo table has a wrapper header; the smc_pptable starts
        # after the powerplay_table header. Try to find where ppt data
        # starts in combo by searching for the first few bytes of ppt.
        # The smc_pptable offset in the combo table depends on the header size.
        if len(ppt) >= 16 and len(combo) >= 16:
            # Try common offsets for smc_pptable within combo
            # smu_14_0_2_powerplay_table header is typically small
            best_offset = 0
            best_matches = 0
            for test_off in range(0, min(256, len(combo) - 16), 2):
                matches = sum(1 for i in range(min(64, len(ppt), len(combo) - test_off))
                             if ppt[i] == combo[test_off + i])
                if matches > best_matches:
                    best_matches = matches
                    best_offset = test_off

            _print_kv("Best alignment offset", f"0x{best_offset:X} ({best_matches}/64 bytes match)")

            if best_offset > 0:
                combo_aligned = combo[best_offset:]
                diffs = _diff_blobs(ppt, combo_aligned, "PPTable", f"Combo+0x{best_offset:X}")
            else:
                diffs = _diff_blobs(ppt, combo, "PPTable", "Combo")
            _print_kv("Total differing bytes", str(diffs))

    # --- Compare with VBIOS ---
    if vbios_pp and "TABLE_PPTABLE" in tables:
        _print_section("Diff: VBIOS PP Table vs SMU TABLE_PPTABLE")
        ppt = tables["TABLE_PPTABLE"]
        compare_len = min(len(vbios_pp), len(ppt))
        diffs = _diff_blobs(vbios_pp[:compare_len], ppt[:compare_len], "VBIOS", "SMU_PPT")
        _print_kv("Total differing bytes", str(diffs))

    if vbios_pp and "TABLE_COMBO_PPTABLE" in tables:
        _print_section("Diff: VBIOS PP Table vs SMU TABLE_COMBO_PPTABLE")
        combo = tables["TABLE_COMBO_PPTABLE"]

        # Try alignment
        best_offset = 0
        best_matches = 0
        for test_off in range(0, min(256, len(combo) - 16), 2):
            matches = sum(1 for i in range(min(64, len(vbios_pp), len(combo) - test_off))
                         if vbios_pp[i] == combo[test_off + i])
            if matches > best_matches:
                best_matches = matches
                best_offset = test_off

        if best_offset > 0:
            combo_aligned = combo[best_offset:]
        else:
            combo_aligned = combo
        compare_len = min(len(vbios_pp), len(combo_aligned))
        diffs = _diff_blobs(vbios_pp[:compare_len], combo_aligned[:compare_len],
                           "VBIOS", f"Combo+0x{best_offset:X}")
        _print_kv("Total differing bytes", str(diffs))

    cleanup_hardware(hw)

    _print_section("INTERPRETATION")
    print("  - TABLE_COMBO_PPTABLE is built by SMU firmware from VBIOS + chip fusing")
    print("  - TABLE_PPTABLE is the driver's working copy (copied from combo)")
    print("  - If COMBO differs from VBIOS at DriverReportedClocks offsets,")
    print("    the SMU firmware is adjusting clocks based on silicon quality")
    print("  - If PPTable differs from COMBO, the driver or a registry override")
    print("    modified the table after reading it from SMU")
    print("  - Writing PP_PhmSoftPowerPlayTable to registry with VBIOS values")
    print("    can override the SMU's adjustments on next driver load")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
