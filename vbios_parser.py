"""
VBIOS ROM Parser -- Extract original clock, power, and TDC values.

Uses upp (Uplift Power Play) library for proper RDNA4/RDNA3 extraction when
available. upp locates the PP table via $PS1 magic in the VBIOS (RDNA3/4)
or via the ATOM Master Data Table (older GPUs), then parses the structured
PP table to read DriverReportedClocks and MsgLimits.

Falls back to heuristic clock-triple scan for older cards when upp is not
available or does not recognize the table format.

  - ORIG_BASECLOCK_AC, ORIG_GAMECLOCK_AC, ORIG_BOOSTCLOCK_AC
  - ORIG_POWER_AC, ORIG_POWER_DC
  - ORIG_TDC_GFX, ORIG_TDC_SOC

Usage:
  from vbios_parser import parse_vbios

  vals = parse_vbios("bios/vbios.rom")
  if vals:
      print(vals.gameclock_ac, vals.power_ac, vals.tdc_gfx)
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Try to use upp for proper RDNA4 extraction
_UPP_AVAILABLE = False
try:
    if not getattr(sys, "frozen", False):
        # Running from source: add sibling upp/src to path
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _upp_src = os.path.join(_script_dir, "..", "upp", "src")
        if os.path.isdir(_upp_src) and _upp_src not in sys.path:
            sys.path.insert(0, os.path.abspath(_upp_src))
    from upp import decode as _upp_decode
    _UPP_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# MsgLimits_t field offsets (legacy heuristic fallback)
# ---------------------------------------------------------------------------

_ML_PPT0_AC      = 0
_ML_PPT0_DC      = 2
_ML_PPT1_AC      = 4
_ML_PPT1_DC      = 6
_ML_TDC_GFX      = 16
_ML_TDC_SOC      = 18
_ML_TEMP_EDGE    = 20
_ML_TEMP_HOTSPOT = 22
_ML_TEMP_HSGFX   = 24
_ML_TEMP_HSSOC   = 26
_ML_TEMP_MEM     = 28
_ML_TEMP_VR_GFX  = 30
_ML_TEMP_VR_SOC  = 32

_MSGLIMITS_OFFSET    = 28
_MSGLIMITS_READ_SIZE = 44

# RDNA4 $PS1 magic (from upp decode.py)
_PS1_RDNA4 = b'\x24\x50\x53\x31\xe0\x16'  # $PS1à
_PS1_RDNA3 = b'\x24\x50\x53\x31\x50\x15'  # $PS1P
_LEGACY_VROM_OFFSET = 0x40000


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class VbiosValues:
    """Stock PPTable values extracted from a VBIOS ROM."""

    baseclock_ac:  int
    gameclock_ac:  int
    boostclock_ac: int
    power_ac:      int
    power_dc:      int
    tdc_gfx:       int
    tdc_soc:       int

    ppt1_ac:      int = 0
    ppt1_dc:      int = 0
    temp_edge:    int = 0
    temp_hotspot: int = 0
    temp_hsgfx:   int = 0
    temp_hssoc:   int = 0
    temp_mem:     int = 0
    temp_vr_gfx:  int = 0
    temp_vr_soc:  int = 0

    rom_offset: int = 0
    rom_path:   str = ""

    def clock_pattern(self) -> bytes:
        return struct.pack("<3H", self.baseclock_ac, self.gameclock_ac, self.boostclock_ac)

    def power_pattern(self) -> bytes:
        return struct.pack("<4H", self.power_ac, self.power_dc, self.ppt1_ac, self.ppt1_dc)

    def summary(self) -> str:
        return (
            f"Clocks: {self.baseclock_ac}/{self.gameclock_ac}/"
            f"{self.boostclock_ac} MHz  "
            f"PPT: {self.power_ac}W  "
            f"TDC: GFX={self.tdc_gfx}A SOC={self.tdc_soc}A"
        )


# ---------------------------------------------------------------------------
# UPP-based extraction (RDNA3/4)
# ---------------------------------------------------------------------------

def _get_pp_table_offset_rdna(rom_bytes: bytes) -> Optional[Tuple[int, int]]:
    """
    Locate PP table in RDNA3/4 VBIOS via $PS1 magic.
    Returns (absolute_offset, pp_length) or None.
    """
    rom_offset = 0
    search_bytes = rom_bytes

    if len(rom_bytes) >= 2:
        magic = rom_bytes[:2].hex().upper()
        if magic == "AA55":
            rom_offset = _LEGACY_VROM_OFFSET
            if len(rom_bytes) < rom_offset + 64:
                return None
            search_bytes = rom_bytes[rom_offset:]

    if len(search_bytes) < 2 or search_bytes[:2].hex().upper() != "55AA":
        return None

    for magic in (_PS1_RDNA4, _PS1_RDNA3):
        idx = search_bytes.find(magic)
        if idx >= 0:
            pp_base = rom_offset + idx + 0x110
            if pp_base + 4 > len(rom_bytes):
                continue
            pp_len = struct.unpack_from("<H", rom_bytes, pp_base)[0]
            if 64 <= pp_len <= 65535 and pp_base + pp_len <= len(rom_bytes):
                return (pp_base, pp_len)
    return None


def _parse_vbios_upp_bytes(
    rom_bytes: bytes,
    rom_path: str = "",
    diagnostic_out: Optional[List[str]] = None,
) -> Optional[VbiosValues]:
    """Extract VbiosValues using upp's structured PP table parsing (bytes input)."""
    if not _UPP_AVAILABLE:
        return None

    result = _get_pp_table_offset_rdna(rom_bytes)
    if result is None:
        return None

    pp_offset, pp_len = result
    pp_tbl = bytearray(rom_bytes[pp_offset:pp_offset + pp_len])

    try:
        data = _upp_decode.select_pp_struct(pp_tbl, rawdump=False, debug=False)
        if data is None:
            return None

        def _get(path: str) -> Optional[int]:
            parts = path.strip("/").split("/")
            normalized = [int(p) if p.isdigit() else p for p in parts]
            res = _upp_decode.get_value(None, normalized, data_dict=data)
            return int(res["value"]) if res and "value" in res else None

        base = _get("smc_pptable/SkuTable/DriverReportedClocks/BaseClockAc")
        game = _get("smc_pptable/SkuTable/DriverReportedClocks/GameClockAc")
        boost = _get("smc_pptable/SkuTable/DriverReportedClocks/BoostClockAc")
        if base is None or game is None or boost is None:
            return None

        power_ac = _get("smc_pptable/SkuTable/MsgLimits/Power/0/0")
        power_dc = _get("smc_pptable/SkuTable/MsgLimits/Power/0/1")
        ppt1_ac = _get("smc_pptable/SkuTable/MsgLimits/Power/1/0")
        ppt1_dc = _get("smc_pptable/SkuTable/MsgLimits/Power/1/1")
        tdc_gfx = _get("smc_pptable/SkuTable/MsgLimits/Tdc/0")
        tdc_soc = _get("smc_pptable/SkuTable/MsgLimits/Tdc/1")

        temp_edge = _get("smc_pptable/SkuTable/MsgLimits/Temperature/0")
        temp_hotspot = _get("smc_pptable/SkuTable/MsgLimits/Temperature/1")
        temp_hsgfx = _get("smc_pptable/SkuTable/MsgLimits/Temperature/2")
        temp_hssoc = _get("smc_pptable/SkuTable/MsgLimits/Temperature/3")
        temp_mem = _get("smc_pptable/SkuTable/MsgLimits/Temperature/4")
        temp_vr_gfx = _get("smc_pptable/SkuTable/MsgLimits/Temperature/6")
        temp_vr_soc = _get("smc_pptable/SkuTable/MsgLimits/Temperature/7")

        if power_ac is None or tdc_gfx is None:
            return None

        if diagnostic_out is not None:
            diagnostic_out.append(f"  Parsed via upp (PP table at 0x{pp_offset:04X}, {pp_len} bytes)")
            diagnostic_out.append(f"  => {base}/{game}/{boost} MHz  PPT={power_ac}W  TDC={tdc_gfx}A")

        return VbiosValues(
            baseclock_ac=base or 0,
            gameclock_ac=game or 0,
            boostclock_ac=boost or 0,
            power_ac=power_ac or 0,
            power_dc=power_dc or power_ac or 0,
            tdc_gfx=tdc_gfx or 0,
            tdc_soc=tdc_soc or 0,
            ppt1_ac=ppt1_ac or 0,
            ppt1_dc=ppt1_dc or 0,
            temp_edge=temp_edge or 0,
            temp_hotspot=temp_hotspot or 0,
            temp_hsgfx=temp_hsgfx or 0,
            temp_hssoc=temp_hssoc or 0,
            temp_mem=temp_mem or 0,
            temp_vr_gfx=temp_vr_gfx or 0,
            temp_vr_soc=temp_vr_soc or 0,
            rom_offset=pp_offset,
            rom_path=rom_path,
        )
    except Exception:
        return None


def _parse_vbios_upp(
    rom_path: str,
    diagnostic_out: Optional[List[str]] = None,
) -> Optional[VbiosValues]:
    """Extract VbiosValues using upp's structured PP table parsing (path input)."""
    if not _UPP_AVAILABLE:
        return None
    try:
        with open(rom_path, "rb") as f:
            rom_bytes = f.read()
    except OSError:
        return None
    return _parse_vbios_upp_bytes(rom_bytes, rom_path, diagnostic_out)


# ---------------------------------------------------------------------------
# Legacy heuristic fallback
# ---------------------------------------------------------------------------

def _clock_candidates_u16_triples(
    blob: bytes,
    *,
    max_items: int = 15,
    min_mhz: int = 500,
    max_mhz: int = 6000,
) -> List[Tuple[int, int, int, int]]:
    counts: Dict[Tuple[int, int, int], int] = {}
    ln = len(blob)
    for off in range(0, ln - 6, 2):
        base, game, boost = struct.unpack_from("<3H", blob, off)
        if base < min_mhz or boost > max_mhz:
            continue
        if not (base <= game <= boost):
            continue
        if base == 0 or boost == 0 or (base == game == boost):
            continue
        if (boost - base) < 50:
            continue
        counts[(base, game, boost)] = counts.get((base, game, boost), 0) + 1
    items = [(cnt, *k) for k, cnt in counts.items()]
    items.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    return items[: max(0, int(max_items))]


def _find_triple_offsets(blob: bytes, base: int, game: int, boost: int) -> List[int]:
    pattern = struct.pack("<3H", base, game, boost)
    offsets: List[int] = []
    pos = 0
    while True:
        idx = blob.find(pattern, pos)
        if idx < 0:
            break
        offsets.append(idx)
        pos = idx + 2
    return offsets


def _read_msglimits_at(blob: bytes, offset: int) -> Optional[Dict[str, int]]:
    end = offset + _MSGLIMITS_READ_SIZE
    if end > len(blob) or offset < 0:
        return None
    data = blob[offset:end]
    return {
        "ppt0_ac": struct.unpack_from("<H", data, _ML_PPT0_AC)[0],
        "ppt0_dc": struct.unpack_from("<H", data, _ML_PPT0_DC)[0],
        "ppt1_ac": struct.unpack_from("<H", data, _ML_PPT1_AC)[0],
        "ppt1_dc": struct.unpack_from("<H", data, _ML_PPT1_DC)[0],
        "tdc_gfx": struct.unpack_from("<H", data, _ML_TDC_GFX)[0],
        "tdc_soc": struct.unpack_from("<H", data, _ML_TDC_SOC)[0],
        "temp_edge": struct.unpack_from("<H", data, _ML_TEMP_EDGE)[0],
        "temp_hotspot": struct.unpack_from("<H", data, _ML_TEMP_HOTSPOT)[0],
        "temp_hsgfx": struct.unpack_from("<H", data, _ML_TEMP_HSGFX)[0],
        "temp_hssoc": struct.unpack_from("<H", data, _ML_TEMP_HSSOC)[0],
        "temp_mem": struct.unpack_from("<H", data, _ML_TEMP_MEM)[0],
        "temp_vr_gfx": struct.unpack_from("<H", data, _ML_TEMP_VR_GFX)[0],
        "temp_vr_soc": struct.unpack_from("<H", data, _ML_TEMP_VR_SOC)[0],
    }


def _validate_msglimits(ml: Dict[str, int]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    ppt, tdc_gfx, tdc_soc = ml["ppt0_ac"], ml["tdc_gfx"], ml["tdc_soc"]
    t_edge, t_hot, t_vr = ml["temp_edge"], ml["temp_hotspot"], ml["temp_vr_gfx"]
    if not (50 <= ppt <= 600):
        reasons.append(f"PPT={ppt}W outside [50-600]")
    if not (20 <= tdc_gfx <= 500):
        reasons.append(f"TDC_GFX={tdc_gfx}A outside [20-500]")
    if not (5 <= tdc_soc <= 200):
        reasons.append(f"TDC_SOC={tdc_soc}A outside [5-200]")
    if not (50 <= t_edge <= 150):
        reasons.append(f"Temp_Edge={t_edge}C outside [50-150]")
    if not (50 <= t_hot <= 150):
        reasons.append(f"Temp_Hotspot={t_hot}C outside [50-150]")
    if not (50 <= t_vr <= 200):
        reasons.append(f"Temp_VR={t_vr}C outside [50-200]")
    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_vbios(
    rom_path: str,
    *,
    max_candidates: int = 10,
    verbose: bool = False,
    diagnostic_out: Optional[List[str]] = None,
) -> Optional[VbiosValues]:
    """Parse a VBIOS ROM and extract original PPTable values.

    Uses upp for RDNA3/4 when available; falls back to heuristic for older GPUs.
    """
    def _log(msg: str) -> None:
        if verbose:
            print(msg)
        if diagnostic_out is not None:
            diagnostic_out.append(msg)

    if not os.path.isabs(rom_path):
        rom_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rom_path)

    if not os.path.isfile(rom_path):
        raise FileNotFoundError(rom_path)

    # 1. Try upp (RDNA3/4)
    result = _parse_vbios_upp(rom_path, diagnostic_out=diagnostic_out)
    if result is not None:
        return result

    # 2. Fallback: legacy heuristic
    with open(rom_path, "rb") as fh:
        blob = fh.read()

    if len(blob) < 64:
        raise ValueError(f"ROM file too small ({len(blob)} bytes): {rom_path}")

    candidates = _clock_candidates_u16_triples(blob, max_items=max_candidates)
    if not candidates:
        raise ValueError(f"No clock-like u16 triples found in ROM: {rom_path}")

    _log(f"  VBIOS: {rom_path}  ({len(blob)} bytes) [upp not used]")
    _log(f"  Top {len(candidates)} clock triple candidates:")
    for cnt, b, g, bo in candidates:
        _log(f"    {cnt:4d}x  base={b}  game={g}  boost={bo}")

    for count, base, game, boost in candidates:
        offsets = _find_triple_offsets(blob, base, game, boost)
        for off in offsets:
            ml_off = off + _MSGLIMITS_OFFSET
            ml = _read_msglimits_at(blob, ml_off)
            if ml is None:
                continue
            valid, reasons = _validate_msglimits(ml)
            tag = "VALID" if valid else "REJECTED"
            _log(f"    offset 0x{off:06X}: PPT={ml['ppt0_ac']}W TDC={ml['tdc_gfx']}A Edge={ml['temp_edge']}C [{tag}]")
            for r in reasons:
                _log(f"      -> {r}")
            if not valid:
                continue
            return VbiosValues(
                baseclock_ac=base, gameclock_ac=game, boostclock_ac=boost,
                power_ac=ml["ppt0_ac"], power_dc=ml["ppt0_dc"],
                tdc_gfx=ml["tdc_gfx"], tdc_soc=ml["tdc_soc"],
                ppt1_ac=ml["ppt1_ac"], ppt1_dc=ml["ppt1_dc"],
                temp_edge=ml["temp_edge"], temp_hotspot=ml["temp_hotspot"],
                temp_hsgfx=ml["temp_hsgfx"], temp_hssoc=ml["temp_hssoc"],
                temp_mem=ml["temp_mem"], temp_vr_gfx=ml["temp_vr_gfx"],
                temp_vr_soc=ml["temp_vr_soc"],
                rom_offset=off, rom_path=rom_path,
            )

    _log("  No valid PPTable structure found in ROM.")
    return None


def parse_vbios_from_bytes(
    rom_bytes: bytes,
    rom_path: str = "",
    *,
    max_candidates: int = 10,
    verbose: bool = False,
    diagnostic_out: Optional[List[str]] = None,
) -> Optional[VbiosValues]:
    """Parse VBIOS from in-memory bytes. Avoids file I/O timing issues."""
    def _log(msg: str) -> None:
        if verbose:
            print(msg)
        if diagnostic_out is not None:
            diagnostic_out.append(msg)

    # 1. Try upp (RDNA3/4)
    result = _parse_vbios_upp_bytes(rom_bytes, rom_path, diagnostic_out=diagnostic_out)
    if result is not None:
        return result

    # 2. Fallback: legacy heuristic
    blob = rom_bytes
    if len(blob) < 64:
        return None

    candidates = _clock_candidates_u16_triples(blob, max_items=max_candidates)
    if not candidates:
        return None

    _log(f"  VBIOS: (from memory, {len(blob)} bytes) [upp not used]")
    _log(f"  Top {len(candidates)} clock triple candidates:")
    for cnt, b, g, bo in candidates:
        _log(f"    {cnt:4d}x  base={b}  game={g}  boost={bo}")

    for count, base, game, boost in candidates:
        offsets = _find_triple_offsets(blob, base, game, boost)
        for off in offsets:
            ml_off = off + _MSGLIMITS_OFFSET
            ml = _read_msglimits_at(blob, ml_off)
            if ml is None:
                continue
            valid, reasons = _validate_msglimits(ml)
            _log(f"    offset 0x{off:06X}: PPT={ml['ppt0_ac']}W TDC={ml['tdc_gfx']}A [{'VALID' if valid else 'REJECTED'}]")
            if not valid:
                continue
            return VbiosValues(
                baseclock_ac=base, gameclock_ac=game, boostclock_ac=boost,
                power_ac=ml["ppt0_ac"], power_dc=ml["ppt0_dc"],
                tdc_gfx=ml["tdc_gfx"], tdc_soc=ml["tdc_soc"],
                ppt1_ac=ml["ppt1_ac"], ppt1_dc=ml["ppt1_dc"],
                temp_edge=ml["temp_edge"], temp_hotspot=ml["temp_hotspot"],
                temp_hsgfx=ml["temp_hsgfx"], temp_hssoc=ml["temp_hssoc"],
                temp_mem=ml["temp_mem"], temp_vr_gfx=ml["temp_vr_gfx"],
                temp_vr_soc=ml["temp_vr_soc"],
                rom_offset=off, rom_path=rom_path,
            )

    return None


def parse_vbios_or_defaults(
    rom_path: str = "bios/vbios.rom",
    *,
    verbose: bool = False,
) -> VbiosValues:
    """Try to parse the VBIOS; fall back to hardcoded defaults on failure."""
    try:
        result = parse_vbios(rom_path, verbose=verbose)
        if result is not None:
            return result
    except (FileNotFoundError, ValueError, OSError) as exc:
        if verbose:
            print(f"  VBIOS parse failed ({exc}); using hardcoded defaults.")

    return VbiosValues(
        baseclock_ac=1900, gameclock_ac=2780, boostclock_ac=3320,
        power_ac=182, power_dc=182, tdc_gfx=152, tdc_soc=55,
        ppt1_ac=1200, ppt1_dc=1200, temp_edge=100, temp_hotspot=110,
        temp_mem=100, temp_vr_gfx=115, rom_path="",
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Parse a VBIOS ROM and extract PPTable values.")
    ap.add_argument("rom", nargs="?", default="bios/vbios.rom", help="Path to VBIOS ROM file")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show diagnostic output")
    args = ap.parse_args()

    print("=" * 60)
    print("  VBIOS Parser")
    print("=" * 60)
    if _UPP_AVAILABLE:
        print("  upp: available (RDNA3/4 supported)")
    else:
        print("  upp: not found (add ../upp/src or pip install upp for RDNA3/4)")

    result = parse_vbios(args.rom, verbose=True)

    if result is None:
        print("\n  FAILED: no valid PPTable structure found.")
        return 1

    print(f"\n{'=' * 60}")
    print(f"  Extracted Values")
    print(f"{'=' * 60}")
    print(f"  ORIG_BASECLOCK_AC  = {result.baseclock_ac}")
    print(f"  ORIG_GAMECLOCK_AC  = {result.gameclock_ac}")
    print(f"  ORIG_BOOSTCLOCK_AC = {result.boostclock_ac}")
    print(f"  ORIG_POWER_AC      = {result.power_ac}")
    print(f"  ORIG_POWER_DC      = {result.power_dc}")
    print(f"  ORIG_TDC_GFX       = {result.tdc_gfx}")
    print(f"  ORIG_TDC_SOC       = {result.tdc_soc}")
    print()
    print(f"  Clock pattern: {result.clock_pattern().hex(' ').upper()}")
    print(f"  Power pattern: {result.power_pattern().hex(' ').upper()}")
    print(f"  ROM offset:    0x{result.rom_offset:06X}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
