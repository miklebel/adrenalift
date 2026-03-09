r"""
Standalone tool to parse the PP_CNEscapeInput registry blob.

Discovered structure (1556 bytes for RDNA4 / Navi44):

  Header: 28 bytes (7 dwords)
    0x00  u32  Size            = total blob length
    0x04  u32  Version         = 1
    0x08  u32  SubVersion      = 2
    0x0C  u16  CapLo           = basic OD capability bitmask
    0x0E  u16  CapHi           = advanced OD capability bitmask
    0x10  u32  NumSettingTypes  = 24
    0x14  u32  Reserved         = 0
    0x18  u32  NumRecords       = 76

  Records: 76 x 20-byte entries starting at 0x1C
    Each record:
      +0   i32  value    (setting value, signed)
      +4   u32  enabled  (0 or 1)
      +8   u8[12] pad    (almost always zeros)

  Trailing: 8 bytes of zeros (0x060C..0x0613)

Record index -> field mapping (determined empirically):
  rec  0 = GfxclkFoffset          (MHz, GFX clock frequency offset)
  rec  1 = AutoUvEngine            (auto undervolt toggle)
  rec  2 = AutoOcEngine            (auto overclock toggle)
  rec  8 = UclkFmax                (MHz, memory clock max)
  rec  9 = Ppt                     (%, power limit percent)
  rec 15 = Unknown_15              (some OD enable)
  rec 19 = FanTempPoint0           (C, fan curve temperature)
  rec 20 = FanPwmPoint0            (%, fan curve PWM)
  rec 21 = FanTempPoint1
  rec 22 = FanPwmPoint1
  rec 23 = FanTempPoint2
  rec 24 = FanPwmPoint2
  rec 25 = FanTempPoint3
  rec 26 = FanPwmPoint3
  rec 27 = FanTempPoint4
  rec 28 = FanPwmPoint4
  rec 37 = VoltageOffset           (mV, signed voltage offset)

Usage:
  py src/tools/parse_cn_escape.py
"""

from __future__ import annotations

import os
import struct
import sys
from typing import List, Optional, Tuple

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import winreg
except ImportError:
    print("ERROR: winreg not available (Windows only)", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
)


def find_amd_adapter() -> Optional[str]:
    hive = winreg.HKEY_LOCAL_MACHINE
    i = 0
    while True:
        try:
            with winreg.OpenKey(hive, _DISPLAY_CLASS_PATH, 0, winreg.KEY_READ) as parent:
                sub = winreg.EnumKey(parent, i)
        except OSError:
            break
        i += 1
        if not sub.strip().isdigit():
            continue
        sub_path = _DISPLAY_CLASS_PATH + "\\" + sub
        try:
            with winreg.OpenKey(hive, sub_path, 0, winreg.KEY_READ) as k:
                mdid, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
                if isinstance(mdid, str) and "VEN_1002" in mdid.upper():
                    return sub_path
        except OSError:
            continue
    return None


def read_binary_value(key_path: str, value_name: str) -> Optional[bytes]:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                            winreg.KEY_READ) as k:
            v, t = winreg.QueryValueEx(k, value_name)
            if isinstance(v, (bytes, bytearray)):
                return bytes(v)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# OD enums from smu_v14_0_2_pptable.h
# ---------------------------------------------------------------------------

ODCAP_NAMES = {
    0: "AUTO_FAN_ACOUSTIC_LIMIT",
    1: "POWER_MODE",
    2: "AUTO_UV_ENGINE",
    3: "AUTO_OC_ENGINE",
    4: "AUTO_OC_MEMORY",
    5: "MEMORY_TIMING_TUNE",
    6: "MANUAL_AC_TIMING",
    7: "AUTO_VF_CURVE_OPTIMIZER",
    8: "AUTO_SOC_UV",
}

# ---------------------------------------------------------------------------
# Record field names (empirical mapping)
# ---------------------------------------------------------------------------

RECORD_NAMES = {
    0:  ("GfxclkFoffset",    "MHz",  "GFX clock frequency offset"),
    1:  ("AutoUvEngine",     "",     "Auto undervolt engine toggle"),
    2:  ("AutoOcEngine",     "",     "Auto overclock engine toggle"),
    3:  ("Setting_3",        "",     "Unknown setting 3"),
    4:  ("Setting_4",        "",     "Unknown setting 4"),
    5:  ("Setting_5",        "",     "Unknown setting 5"),
    6:  ("Setting_6",        "",     "Unknown setting 6"),
    7:  ("Setting_7",        "",     "Unknown setting 7"),
    8:  ("UclkFmax",         "MHz",  "Memory clock max frequency"),
    9:  ("Ppt",              "%",    "Power limit percent"),
    10: ("Tdc",              "",     "TDC limit"),
    11: ("Setting_11",       "",     "Unknown setting 11"),
    12: ("Setting_12",       "",     "Unknown setting 12"),
    13: ("Setting_13",       "",     "Unknown setting 13"),
    14: ("Setting_14",       "",     "Unknown setting 14"),
    15: ("Setting_15",       "",     "Unknown setting 15"),
    16: ("Setting_16",       "",     "Unknown setting 16"),
    17: ("Setting_17",       "",     "Unknown setting 17"),
    18: ("Setting_18",       "",     "Unknown setting 18"),
    19: ("FanTempPoint0",    "C",    "Fan curve temp point 0"),
    20: ("FanPwmPoint0",     "%",    "Fan curve PWM point 0"),
    21: ("FanTempPoint1",    "C",    "Fan curve temp point 1"),
    22: ("FanPwmPoint1",     "%",    "Fan curve PWM point 1"),
    23: ("FanTempPoint2",    "C",    "Fan curve temp point 2"),
    24: ("FanPwmPoint2",     "%",    "Fan curve PWM point 2"),
    25: ("FanTempPoint3",    "C",    "Fan curve temp point 3"),
    26: ("FanPwmPoint3",     "%",    "Fan curve PWM point 3"),
    27: ("FanTempPoint4",    "C",    "Fan curve temp point 4"),
    28: ("FanPwmPoint4",     "%",    "Fan curve PWM point 4"),
    29: ("FanTempPoint5",    "C",    "Fan curve temp point 5"),
    30: ("FanPwmPoint5",     "%",    "Fan curve PWM point 5"),
    37: ("VoltageOffset",    "mV",   "Voltage offset (signed)"),
}

HEADER_SIZE = 28
RECORD_SIZE = 20
DATA_START = 0x1C

# ---------------------------------------------------------------------------
# Hex dump
# ---------------------------------------------------------------------------

def hexdump(data: bytes, offset: int = 0, width: int = 16) -> None:
    for row_start in range(0, len(data), width):
        row = data[row_start:row_start + width]
        hex_str = " ".join(f"{b:02X}" for b in row)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"  {offset + row_start:04X}  {hex_str:<{width * 3}}  {ascii_str}")


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

def parse_header(blob: bytes) -> None:
    size = struct.unpack_from("<I", blob, 0x00)[0]
    version = struct.unpack_from("<I", blob, 0x04)[0]
    sub_ver = struct.unpack_from("<I", blob, 0x08)[0]
    cap_lo = struct.unpack_from("<H", blob, 0x0C)[0]
    cap_hi = struct.unpack_from("<H", blob, 0x0E)[0]
    num_setting_types = struct.unpack_from("<I", blob, 0x10)[0]
    reserved = struct.unpack_from("<I", blob, 0x14)[0]
    num_records = struct.unpack_from("<I", blob, 0x18)[0]

    print(f"\n  HEADER (28 bytes)")
    print(f"  {'='*80}")
    print(f"  0x0000  Size             = {size} (blob={len(blob)})")
    print(f"  0x0004  Version          = {version}")
    print(f"  0x0008  SubVersion       = {sub_ver}")
    print(f"  0x000C  CapLo            = 0x{cap_lo:04X} (basic caps)")
    cap_lo_names = [ODCAP_NAMES.get(b, f"bit{b}") for b in range(16) if cap_lo & (1 << b)]
    if cap_lo_names:
        print(f"           -> {', '.join(cap_lo_names)}")
    print(f"  0x000E  CapHi            = 0x{cap_hi:04X} (advanced caps)")
    cap_hi_names = [ODCAP_NAMES.get(b, f"bit{b}") for b in range(16) if cap_hi & (1 << b)]
    if cap_hi_names:
        print(f"           -> {', '.join(cap_hi_names)}")
    print(f"  0x0010  NumSettingTypes  = {num_setting_types}")
    print(f"  0x0014  Reserved         = {reserved}")
    print(f"  0x0018  NumRecords       = {num_records}")

    expected_size = HEADER_SIZE + num_records * RECORD_SIZE
    remainder = len(blob) - expected_size
    print(f"\n  Calculated: {HEADER_SIZE} + {num_records} * {RECORD_SIZE} = {expected_size} "
          f"(remainder={remainder})")

    return num_records


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------

def parse_records(blob: bytes, num_records: int) -> None:
    print(f"\n  RECORDS ({num_records} x {RECORD_SIZE} bytes, starting at 0x{DATA_START:04X})")
    print(f"  {'='*80}")
    print(f"  {'Rec':>4s}  {'Offset':>6s}  {'Name':30s}  {'Value':>10s}  {'En':>3s}  {'Unit':>4s}  Description")
    print(f"  {'----':>4s}  {'------':>6s}  {'-'*30}  {'-'*10}  {'---':>3s}  {'----':>4s}  -----------")

    for i in range(num_records):
        off = DATA_START + i * RECORD_SIZE
        if off + RECORD_SIZE > len(blob):
            break

        val = struct.unpack_from("<i", blob, off)[0]
        uval = struct.unpack_from("<I", blob, off)[0]
        flag = struct.unpack_from("<I", blob, off + 4)[0]
        extra = struct.unpack_from("<I", blob, off + 8)[0]

        name_info = RECORD_NAMES.get(i)
        if name_info:
            name, unit, desc = name_info
        else:
            name = f"rec_{i}"
            unit = ""
            desc = ""

        if val == 0 and flag == 0 and extra == 0:
            continue

        en_str = "YES" if flag == 1 else ("NO" if flag == 0 else f"?{flag}")
        extra_note = f"  extra=0x{extra:08X}" if extra else ""
        signed_note = f" (u32=0x{uval:08X})" if val < 0 else ""
        print(f"  {i:4d}  0x{off:04X}  {name:30s}  {val:10d}  {en_str:>3s}  {unit:>4s}  "
              f"{desc}{signed_note}{extra_note}")


# ---------------------------------------------------------------------------
# Comparison with current cn_escape.py field map
# ---------------------------------------------------------------------------

CN_ESCAPE_CURRENT = [
    ("GfxclkFoffset",       0x001C, "i", "rec 0 value -> CORRECT offset"),
    ("GfxclkFoffset_en",    0x0020, "I", "rec 0 flag  -> CORRECT offset"),
    ("UclkFmax",            0x00C0, "I", "rec 8 FLAG (not value!) -> WRONG, should be 0x00BC"),
    ("UclkFmax_en",         0x00C4, "I", "rec 8 pad[0] -> WRONG, should be 0x00C0"),
    ("PowerPct",            0x00D4, "I", "rec 9 FLAG (not value!) -> WRONG, should be 0x00D0"),
    ("PowerPct_en",         0x00D8, "I", "rec 9 pad[0] -> WRONG, should be 0x00D4"),
    ("VoltageOffset",       0x0304, "i", "rec 37 FLAG -> WRONG, should be 0x0300"),
    ("VoltageOffset_en",    0x0308, "I", "rec 37 pad[0] -> WRONG, should be 0x0304"),
    ("CurveZone0",          0x019C, "I", "rec 19 FLAG -> WRONG name+offset, is FanTempPoint0 at 0x0198"),
    ("CurveZone0_en",       0x01A0, "I", "rec 19 pad[0] -> WRONG, should be 0x019C"),
    ("CurveZone1",          0x01B0, "I", "rec 20 FLAG -> WRONG name+offset, is FanPwmPoint0 at 0x01AC"),
    ("CurveZone2",          0x01C4, "I", "rec 21 FLAG -> WRONG, is FanTempPoint1 at 0x01C0"),
    ("CurveZone3",          0x01D8, "I", "rec 22 FLAG -> WRONG, is FanPwmPoint1 at 0x01D4"),
    ("CurveZone4",          0x01EC, "I", "rec 23 FLAG -> WRONG, is FanTempPoint2 at 0x01E8"),
    ("CurveZone5",          0x0200, "I", "rec 24 FLAG -> WRONG, is FanPwmPoint2 at 0x01FC"),
    ("CurveZone6",          0x0214, "I", "rec 25 FLAG -> WRONG, is FanTempPoint3 at 0x0210"),
    ("CurveZone7",          0x0228, "I", "rec 26 FLAG -> WRONG, is FanPwmPoint3 at 0x0224"),
    ("CurveZone8",          0x023C, "I", "rec 27 FLAG -> WRONG, is FanTempPoint4 at 0x0238"),
    ("CurveZone9",          0x0250, "I", "rec 28 FLAG -> WRONG, is FanPwmPoint4 at 0x024C"),
]


def compare_with_current(blob: bytes) -> None:
    print(f"\n  COMPARISON: current cn_escape.py vs corrected")
    print(f"  {'='*80}")
    print(f"  {'Current Name':24s}  {'CurOff':>7s}  {'CurVal':>10s}  {'FixOff':>7s}  {'FixVal':>10s}  Problem")
    print(f"  {'-'*24}  {'-'*7}  {'-'*10}  {'-'*7}  {'-'*10}  -------")

    fixes = {
        "UclkFmax":       0x00BC,
        "UclkFmax_en":    0x00C0,
        "PowerPct":       0x00D0,
        "PowerPct_en":    0x00D4,
        "VoltageOffset":  0x0300,
        "VoltageOffset_en": 0x0304,
        "CurveZone0":     0x0198,
        "CurveZone0_en":  0x019C,
        "CurveZone1":     0x01AC,
        "CurveZone1_en":  0x01B0,
        "CurveZone2":     0x01C0,
        "CurveZone2_en":  0x01C4,
        "CurveZone3":     0x01D4,
        "CurveZone3_en":  0x01D8,
        "CurveZone4":     0x01E8,
        "CurveZone4_en":  0x01EC,
        "CurveZone5":     0x01FC,
        "CurveZone5_en":  0x0200,
        "CurveZone6":     0x0210,
        "CurveZone6_en":  0x0214,
        "CurveZone7":     0x0224,
        "CurveZone7_en":  0x0228,
        "CurveZone8":     0x0238,
        "CurveZone8_en":  0x023C,
        "CurveZone9":     0x024C,
        "CurveZone9_en":  0x0250,
    }

    for name, cur_off, fmt, note in CN_ESCAPE_CURRENT:
        sz = struct.calcsize(fmt)
        cur_val = struct.unpack_from(f"<{fmt}", blob, cur_off)[0] if cur_off + sz <= len(blob) else "OOB"
        fix_off = fixes.get(name, cur_off)
        fix_val = struct.unpack_from(f"<{fmt}", blob, fix_off)[0] if fix_off + sz <= len(blob) else "OOB"
        problem = "OK" if cur_off == fix_off else f"OFF BY {cur_off - fix_off:+d}"
        print(f"  {name:24s}  0x{cur_off:04X}   {str(cur_val):>10s}  0x{fix_off:04X}   {str(fix_val):>10s}  {problem}  {note}")


# ---------------------------------------------------------------------------
# Full record dump (all 76 records including zeros)
# ---------------------------------------------------------------------------

def dump_all_records(blob: bytes, num_records: int) -> None:
    print(f"\n  ALL RECORDS (including zero/disabled)")
    print(f"  {'='*80}")

    for i in range(num_records):
        off = DATA_START + i * RECORD_SIZE
        if off + RECORD_SIZE > len(blob):
            break

        val = struct.unpack_from("<i", blob, off)[0]
        uval = struct.unpack_from("<I", blob, off)[0]
        flag = struct.unpack_from("<I", blob, off + 4)[0]
        rest = blob[off + 8:off + RECORD_SIZE]
        rest_nonzero = any(b != 0 for b in rest)

        name_info = RECORD_NAMES.get(i)
        name = name_info[0] if name_info else f"rec_{i}"

        rest_hex = " ".join(f"{b:02X}" for b in rest) if rest_nonzero else ""
        en = "EN" if flag == 1 else ("  " if flag == 0 else f"?{flag}")
        signed_note = f" (0x{uval:08X})" if val < 0 else ""

        if val == 0 and flag == 0 and not rest_nonzero:
            print(f"  {i:4d}  0x{off:04X}  {name:30s}    (zero)")
        else:
            print(f"  {i:4d}  0x{off:04X}  {name:30s}  val={val:10d}{signed_note}  {en}  {rest_hex}")


# ---------------------------------------------------------------------------
# Fan curve summary
# ---------------------------------------------------------------------------

def print_fan_curve(blob: bytes) -> None:
    print(f"\n  FAN CURVE (interleaved temp/PWM pairs in records 19-30)")
    print(f"  {'='*80}")

    points = []
    for pt in range(6):
        temp_rec = 19 + pt * 2
        pwm_rec = 20 + pt * 2
        temp_off = DATA_START + temp_rec * RECORD_SIZE
        pwm_off = DATA_START + pwm_rec * RECORD_SIZE

        if temp_off + 4 > len(blob) or pwm_off + 4 > len(blob):
            break

        temp_val = struct.unpack_from("<i", blob, temp_off)[0]
        temp_en = struct.unpack_from("<I", blob, temp_off + 4)[0]
        pwm_val = struct.unpack_from("<i", blob, pwm_off)[0]
        pwm_en = struct.unpack_from("<I", blob, pwm_off + 4)[0]

        if temp_en or pwm_en or temp_val or pwm_val:
            en_str = "active" if (temp_en and pwm_en) else "partial" if (temp_en or pwm_en) else "inactive"
            points.append((temp_val, pwm_val, en_str))
            print(f"  Point {pt}: {temp_val:3d} C -> {pwm_val:3d}% PWM  ({en_str})")
        else:
            print(f"  Point {pt}: (disabled)")

    if points:
        print(f"\n  Curve: ", end="")
        for i, (t, p, _) in enumerate(points):
            sep = " -> " if i > 0 else ""
            print(f"{sep}{t}C:{p}%", end="")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    adapter_path = find_amd_adapter()
    if not adapter_path:
        print("ERROR: No AMD adapter found", file=sys.stderr)
        return 1

    print(f"  Adapter: {adapter_path}")

    blob = read_binary_value(adapter_path, "PP_CNEscapeInput")
    if not blob:
        print("  PP_CNEscapeInput: not present")
        return 1

    print(f"  PP_CNEscapeInput: {len(blob)} bytes ({len(blob) // 4} dwords)")

    print(f"\n  Hex dump:")
    hexdump(blob)

    num_records = parse_header(blob)
    parse_records(blob, num_records)
    print_fan_curve(blob)
    compare_with_current(blob)
    dump_all_records(blob, num_records)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
