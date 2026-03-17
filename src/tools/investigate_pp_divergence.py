"""
Investigate PP Table Divergence: VBIOS vs SMU COMBO_PPTABLE vs RAM copies
==========================================================================

The AMD driver does NOT use the VBIOS PP table directly. The init flow is:

  1. SMU firmware reads the VBIOS PP table at boot
  2. SMU merges it with per-chip fusing/binning data into a "Combo PP Table"
  3. Driver reads TABLE_COMBO_PPTABLE (id=1) from SMU -> gets chip-specific values
  4. Driver copies smc_pptable portion into its driver_pptable allocation
  5. Driver uploads driver_pptable back to SMU as TABLE_PPTABLE (id=0)

The DriverReportedClocks in the Combo PP Table may differ from the VBIOS
because the SMU firmware adjusts them based on silicon quality (fusing data).

This script reads all three sources and compares them to identify exactly
where the clock values diverge.

Usage (run as admin):
  py -m src.tools.investigate_pp_divergence
  py -m src.tools.investigate_pp_divergence --rom bios/vbios.rom
  py -m src.tools.investigate_pp_divergence --no-smu   (registry + VBIOS only)
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.tools.sppt_cache import SpptCache


def _hex_preview(data: bytes, n: int = 32) -> str:
    return " ".join(f"{b:02X}" for b in data[:n])


def _print_section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def _print_kv(k: str, v) -> None:
    print(f"  {k:36s}: {v}")


def _extract_clocks_raw(blob: bytes, cache: SpptCache | None) -> dict | None:
    """Extract DriverReportedClocks from a PP table blob."""
    if cache and cache.fields:
        result = {}
        for name in ("BaseClockAc", "GameClockAc", "BoostClockAc",
                      "BaseClockDc", "GameClockDc", "BoostClockDc"):
            f = cache.get_field(name)
            if f:
                result[name] = f.value
        for name in ("Power_0_AC", "Power_0_DC", "Power_1_AC", "Power_1_DC",
                      "Tdc_GFX", "Tdc_SOC"):
            f = cache.get_field(name)
            if f:
                result[name] = f.value
        return result if result else None
    return None


def _compare_sources(sources: dict[str, dict]) -> None:
    """Print a side-by-side comparison table of clock/power values."""
    names = list(sources.keys())
    if len(names) < 2:
        print("  (Need at least 2 sources to compare)")
        return

    all_fields = set()
    for vals in sources.values():
        if vals:
            all_fields.update(vals.keys())

    field_order = [
        "BaseClockAc", "GameClockAc", "BoostClockAc",
        "BaseClockDc", "GameClockDc", "BoostClockDc",
        "Power_0_AC", "Power_0_DC", "Power_1_AC", "Power_1_DC",
        "Tdc_GFX", "Tdc_SOC",
    ]
    fields = [f for f in field_order if f in all_fields]

    _print_section("COMPARISON: " + " vs ".join(names))

    hdr = f"  {'Field':20s}"
    for name in names:
        hdr += f"  {name:>14s}"
    hdr += "  Delta"
    print(hdr)
    print(f"  {'-' * 20}" + f"  {'-' * 14}" * len(names) + f"  {'-' * 12}")

    for field in fields:
        row = f"  {field:20s}"
        values = []
        for name in names:
            v = sources[name].get(field) if sources[name] else None
            values.append(v)
            row += f"  {str(v) if v is not None else '---':>14s}"

        non_none = [v for v in values if v is not None]
        if len(non_none) >= 2 and len(set(non_none)) > 1:
            delta = non_none[-1] - non_none[0]
            row += f"  {delta:+d} {'<-- DIFFERS' if delta != 0 else ''}"
        elif len(non_none) >= 2:
            row += f"  (same)"
        print(row)


def _read_smu_pptable(smu, virt, table_id: int, label: str) -> SpptCache | None:
    """Read a PP table from SMU via DMA and return as SpptCache."""
    try:
        resp, ret = smu.send_msg(smu.transfer_read, table_id)
        _print_kv(f"SMU response (table {table_id})", f"resp=0x{resp:X} ret=0x{ret:X}")

        from src.engine.overclock_engine import read_buf
        raw = read_buf(virt, 0x3000)

        if len(raw) >= 4:
            hdr_size = struct.unpack_from("<H", raw, 0)[0]
            if 64 <= hdr_size <= len(raw):
                raw = raw[:hdr_size]
                _print_kv("Trimmed to header size", f"{hdr_size} bytes")
            else:
                _print_kv("Header size field", f"{hdr_size} (using full {len(raw)} bytes)")

        cache = SpptCache(raw, source=label)
        _print_kv("Parsed fields", str(len(cache.fields)))
        return cache
    except Exception as e:
        print(f"  ERROR reading {label}: {e}")
        return None


def _read_combo_pptable_direct(smu, virt) -> SpptCache | None:
    """Read TABLE_COMBO_PPTABLE (id=1) directly from SMU.

    The combo PP table is what the SMU firmware builds by merging the VBIOS
    PP table with per-chip fusing data. This is the authoritative source
    of what DriverReportedClocks the driver should use.
    """
    from src.engine.od_table import TABLE_COMBO_PPTABLE
    _print_section("SMU TABLE_COMBO_PPTABLE (id=1) — Chip-fused values")
    return _read_smu_pptable(smu, virt, TABLE_COMBO_PPTABLE, "smu_combo")


def _read_driver_pptable(smu, virt) -> SpptCache | None:
    """Read TABLE_PPTABLE (id=0) — the driver's working copy."""
    from src.engine.od_table import TABLE_PPTABLE
    _print_section("SMU TABLE_PPTABLE (id=0) — Driver's working copy")
    return _read_smu_pptable(smu, virt, TABLE_PPTABLE, "smu_driver")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Investigate PP Table divergence between VBIOS, SMU, and RAM")
    ap.add_argument("--rom", default="bios/vbios.rom",
                    help="Path to VBIOS ROM file")
    ap.add_argument("--no-smu", action="store_true",
                    help="Skip SMU reads (no admin required)")
    ap.add_argument("--no-registry", action="store_true",
                    help="Skip registry check")
    ap.add_argument("--dump-fields", action="store_true",
                    help="Dump all parsed fields for each source")
    args = ap.parse_args()

    print("PP Table Divergence Investigation")
    print("=" * 70)

    sources: dict[str, dict | None] = {}

    # --- 1. VBIOS ROM ---
    _print_section("VBIOS ROM (original silicon values)")
    vbios_cache = SpptCache.from_vbios(args.rom)
    if vbios_cache:
        _print_kv("Source", vbios_cache.source)
        _print_kv("Size", f"{vbios_cache.size} bytes")
        _print_kv("Fields", str(len(vbios_cache.fields)))
        vals = _extract_clocks_raw(vbios_cache.to_bytes(), vbios_cache)
        sources["VBIOS"] = vals
        if vals:
            for k, v in vals.items():
                _print_kv(k, str(v))
        if args.dump_fields:
            print(vbios_cache.dump_fields())
    else:
        print(f"  Could not read VBIOS from {args.rom}")
        sources["VBIOS"] = None

    # --- 2. Registry PP_PhmSoftPowerPlayTable ---
    if not args.no_registry:
        _print_section("Registry PP_PhmSoftPowerPlayTable")
        reg_cache = SpptCache.from_registry_scan()
        if reg_cache:
            _print_kv("Source", reg_cache.source)
            _print_kv("Size", f"{reg_cache.size} bytes")
            _print_kv("Fields", str(len(reg_cache.fields)))
            vals = _extract_clocks_raw(reg_cache.to_bytes(), reg_cache)
            sources["Registry"] = vals
            if vals:
                for k, v in vals.items():
                    _print_kv(k, str(v))
            if args.dump_fields:
                print(reg_cache.dump_fields())
        else:
            print("  No PP_PhmSoftPowerPlayTable found in registry (expected on clean install)")
            sources["Registry"] = None

    # --- 3. SMU reads (requires admin + hardware init) ---
    if not args.no_smu:
        try:
            from src.engine.overclock_engine import init_hardware, cleanup_hardware

            _print_section("Initializing hardware...")
            hw = init_hardware()
            smu = hw['smu']
            virt = hw['virt']
            _print_kv("DMA path", hw['dma_path'])
            _print_kv("VRAM BAR", f"0x{hw['vram_bar']:X}")

            # TABLE_PPTABLE (id=0) - driver's working copy
            driver_cache = _read_driver_pptable(smu, virt)
            if driver_cache:
                vals = _extract_clocks_raw(driver_cache.to_bytes(), driver_cache)
                sources["SMU_PPTable"] = vals
                if vals:
                    for k, v in vals.items():
                        _print_kv(k, str(v))
                if args.dump_fields:
                    print(driver_cache.dump_fields())

            # TABLE_COMBO_PPTABLE (id=1) - SMU's fused version
            combo_cache = _read_combo_pptable_direct(smu, virt)
            if combo_cache:
                vals = _extract_clocks_raw(combo_cache.to_bytes(), combo_cache)
                sources["SMU_Combo"] = vals
                if vals:
                    for k, v in vals.items():
                        _print_kv(k, str(v))
                if args.dump_fields:
                    print(combo_cache.dump_fields())

            # Also dump raw bytes around DriverReportedClocks for manual inspection
            _print_section("Raw byte comparison at DriverReportedClocks offset")
            if vbios_cache and driver_cache:
                bc_field = vbios_cache.get_field("BaseClockAc")
                if bc_field:
                    off = bc_field.offset
                    vbios_bytes = vbios_cache.to_bytes()
                    driver_bytes = driver_cache.to_bytes()
                    combo_bytes = combo_cache.to_bytes() if combo_cache else b''

                    region = min(14, len(vbios_bytes) - off)
                    if region > 0:
                        _print_kv(f"VBIOS   [0x{off:04X}..+{region}]",
                                  _hex_preview(vbios_bytes[off:off + region], region))
                    if off + region <= len(driver_bytes):
                        _print_kv(f"Driver  [0x{off:04X}..+{region}]",
                                  _hex_preview(driver_bytes[off:off + region], region))
                    if combo_bytes and off + region <= len(combo_bytes):
                        _print_kv(f"Combo   [0x{off:04X}..+{region}]",
                                  _hex_preview(combo_bytes[off:off + region], region))

            cleanup_hardware(hw)

        except Exception as e:
            import traceback
            print(f"  SMU access failed: {e}")
            traceback.print_exc()
            print("  (Run as administrator for SMU access)")

    # --- Comparison ---
    valid_sources = {k: v for k, v in sources.items() if v}
    if len(valid_sources) >= 2:
        _compare_sources(valid_sources)

    # --- Analysis ---
    _print_section("ANALYSIS")

    vbios_vals = sources.get("VBIOS")
    driver_vals = sources.get("SMU_PPTable") or sources.get("SMU_Combo")

    if vbios_vals and driver_vals:
        vb_game = vbios_vals.get("GameClockAc", 0)
        dr_game = driver_vals.get("GameClockAc", 0)
        vb_base = vbios_vals.get("BaseClockAc", 0)
        dr_base = driver_vals.get("BaseClockAc", 0)

        if vb_game != dr_game or vb_base != dr_base:
            print(f"  CONFIRMED: Driver PP table differs from VBIOS!")
            print(f"  BaseClock: {vb_base} (VBIOS) -> {dr_base} (driver)  delta={dr_base - vb_base}")
            print(f"  GameClock: {vb_game} (VBIOS) -> {dr_game} (driver)  delta={dr_game - vb_game}")
            print()
            print("  Root cause analysis:")
            print("  The SMU firmware's TABLE_COMBO_PPTABLE merges the VBIOS PP table")
            print("  with per-chip silicon fusing data. The SMU adjusts DriverReportedClocks")
            print("  based on the actual chip quality/binning, which is why the RAM values")
            print("  differ from the VBIOS 'marketing' values.")
            print()
            print("  Implications for overclocking:")
            print("  1. The VBIOS values represent the SKU's maximum rated frequency")
            print("  2. The driver values represent what the SMU thinks THIS chip can do")
            print("  3. Patching the RAM copies to VBIOS values (or higher) should work")
            print("     since our OD offset and SetSoftMax already exceed these clocks")
            print("  4. To make changes persistent, we could:")
            print("     a) Write PP_PhmSoftPowerPlayTable to registry with VBIOS values")
            print("     b) Keep patching the RAM copies at runtime (current approach)")
            print("     c) Upload a modified TABLE_PPTABLE back to SMU (experimental)")
        else:
            print("  VBIOS and driver clock values match — no divergence detected.")
    else:
        print("  Could not compare (need both VBIOS and SMU/driver data)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
