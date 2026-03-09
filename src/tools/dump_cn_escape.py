r"""
Dump and analyze the PP_CNEscapeInput registry blob.

This is the Adrenalin escape-interface structure that the AMD driver uses
to communicate OD (OverDrive) settings. It may contain cached clock limits.

Usage:
  py dump_cn_escape.py
"""

from __future__ import annotations

import os
import struct
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import winreg
except ImportError:
    print("ERROR: winreg not available", file=sys.stderr)
    sys.exit(1)

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
)

CLOCK_TARGETS = {
    "BaseClockAc":  1920,
    "GameClockAc":  2840,
    "BoostClockAc": 3320,
}


def find_amd_adapter():
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


def read_binary_value(key_path: str, value_name: str) -> bytes | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                            winreg.KEY_READ) as k:
            v, t = winreg.QueryValueEx(k, value_name)
            if isinstance(v, (bytes, bytearray)):
                return bytes(v)
    except OSError:
        pass
    return None


def hexdump(data: bytes, offset: int = 0, width: int = 16,
            highlights: dict | None = None) -> None:
    """Print hex dump with optional highlights for specific offsets."""
    hl_offsets = {}
    if highlights:
        for label, (off, length) in highlights.items():
            for x in range(off, off + length):
                hl_offsets[x] = label

    for row_start in range(0, len(data), width):
        row = data[row_start:row_start + width]
        hex_parts = []
        for j, b in enumerate(row):
            abs_off = row_start + j
            if abs_off in hl_offsets:
                hex_parts.append(f"\033[1;33m{b:02X}\033[0m")
            else:
                hex_parts.append(f"{b:02X}")

        hex_str = " ".join(hex_parts)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"  {offset + row_start:04X}  {hex_str:<{width*3}}  {ascii_str}")


def scan_all_clock_patterns(blob: bytes) -> None:
    """Scan for all possible encodings of target clocks."""
    print("\n  Scanning for target clock values in all encodings:")
    print("  " + "─" * 80)

    for label, mhz in CLOCK_TARGETS.items():
        print(f"\n  {label} = {mhz} MHz:")
        found_any = False

        patterns = [
            ("u16_LE (MHz)",     struct.pack("<H", mhz)),
            ("u32_LE (MHz)",     struct.pack("<I", mhz)),
            ("u16_BE (MHz)",     struct.pack(">H", mhz)),
            ("u32_BE (MHz)",     struct.pack(">I", mhz)),
            ("u16_LE (10MHz)",   struct.pack("<H", mhz // 10) if mhz % 10 == 0 else b""),
            ("u32_LE (10MHz)",   struct.pack("<I", mhz // 10) if mhz % 10 == 0 else b""),
            ("u16_LE (10kHz)",   struct.pack("<H", mhz * 10) if mhz * 10 <= 0xFFFF else b""),
            ("u32_LE (10kHz)",   struct.pack("<I", mhz * 10)),
            ("u32_LE (100kHz)",  struct.pack("<I", mhz * 100)),
            ("u32_LE (kHz)",     struct.pack("<I", mhz * 1000)),
        ]

        for pat_name, pat_bytes in patterns:
            if not pat_bytes:
                continue
            idx = 0
            while True:
                pos = blob.find(pat_bytes, idx)
                if pos < 0:
                    break
                found_any = True
                start = max(0, pos - 4)
                end = min(len(blob), pos + len(pat_bytes) + 4)
                ctx = " ".join(f"{x:02X}" for x in blob[start:end])
                print(f"    FOUND {pat_name:20s} at offset 0x{pos:04X}  [{ctx}]")
                idx = pos + 1

        if not found_any:
            print(f"    (not found in any encoding)")


def parse_structure(blob: bytes) -> None:
    """Try to parse the PP_CNEscapeInput as a structured blob."""
    if len(blob) < 12:
        print("  Blob too small to parse")
        return

    print("\n  Structure analysis:")
    print("  " + "─" * 80)

    # Header
    size = struct.unpack_from("<I", blob, 0)[0]
    print(f"  [0x0000] u32 header/size = {size} (0x{size:08X})")
    if size == len(blob):
        print(f"           ^ matches blob size exactly")

    # Try to identify field patterns: scan as u32 LE sequence
    print(f"\n  As u32 LE sequence (first 64 dwords):")
    n_dwords = min(len(blob) // 4, 64)
    for i in range(n_dwords):
        off = i * 4
        val = struct.unpack_from("<I", blob, off)[0]
        annotations = []
        for label, mhz in CLOCK_TARGETS.items():
            if val == mhz:
                annotations.append(f"== {label} (MHz)")
            if mhz % 10 == 0 and val == mhz // 10:
                annotations.append(f"== {label}/10")
            if val == mhz * 10:
                annotations.append(f"== {label}*10 (10kHz)")
            if val == mhz * 100:
                annotations.append(f"== {label}*100 (100kHz)")
            if val == mhz * 1000:
                annotations.append(f"== {label}*1000 (kHz)")

        # Also flag plausible clock values (500-4000 range)
        if 500 <= val <= 5000 and not annotations:
            annotations.append(f"(plausible clock MHz?)")
        if 50 <= val <= 500 and not annotations:
            annotations.append(f"(plausible clock/10?)")
        if 50000 <= val <= 5000000 and not annotations:
            annotations.append(f"(plausible kHz/10kHz?)")

        ann = "  " + ", ".join(annotations) if annotations else ""
        print(f"  [0x{off:04X}] dw[{i:3d}] = {val:12d}  (0x{val:08X}){ann}")

    # Also dump as u16 LE for the first 128 words
    print(f"\n  As u16 LE sequence (first 128 words, showing non-zero only):")
    n_words = min(len(blob) // 2, 128)
    for i in range(n_words):
        off = i * 2
        val = struct.unpack_from("<H", blob, off)[0]
        if val == 0:
            continue
        annotations = []
        for label, mhz in CLOCK_TARGETS.items():
            if val == mhz:
                annotations.append(f"== {label}")
            if mhz % 10 == 0 and val == mhz // 10:
                annotations.append(f"== {label}/10")
            if val == mhz * 10:
                annotations.append(f"== {label}*10")
        if 500 <= val <= 5000 and not annotations:
            annotations.append("(plausible clock MHz?)")
        ann = "  " + ", ".join(annotations) if annotations else ""
        print(f"  [0x{off:04X}] w[{i:3d}] = {val:8d}  (0x{val:04X}){ann}")

    # Tail: dump remaining bytes if any structure ends before blob
    if len(blob) > n_dwords * 4:
        remaining = len(blob) - n_dwords * 4
        print(f"\n  Remaining {remaining} bytes after first {n_dwords} dwords:")
        hexdump(blob[n_dwords * 4:], offset=n_dwords * 4)


def main() -> int:
    adapter_path = find_amd_adapter()
    if not adapter_path:
        print("ERROR: No AMD adapter found", file=sys.stderr)
        return 1

    print(f"  Adapter: {adapter_path}")

    # Read PP_CNEscapeInput
    blob = read_binary_value(adapter_path, "PP_CNEscapeInput")
    if blob:
        print(f"\n{'='*90}")
        print(f"  PP_CNEscapeInput: {len(blob)} bytes")
        print(f"{'='*90}")
        print("\n  Full hex dump:")
        hexdump(blob)
        scan_all_clock_patterns(blob)
        parse_structure(blob)
    else:
        print("  PP_CNEscapeInput: not present")

    # Also read BDValue
    bdval = read_binary_value(adapter_path, "BDValue")
    if bdval:
        print(f"\n{'='*90}")
        print(f"  BDValue: {len(bdval)} bytes")
        print(f"{'='*90}")
        hexdump(bdval)
        scan_all_clock_patterns(bdval)

    # Read OD8Settings and related
    print(f"\n{'='*90}")
    print(f"  OD-related registry values")
    print(f"{'='*90}")
    od_names = [
        "OD8Settings", "Point2Freq", "Point3Freq",
        "PP_AutoODActive", "PP_AutoOCEngineClock",
        "PP_AutoUVV2VoltageOffset",
        "PP_AutoCurveOptimizerVoltageOffset1",
        "PP_AutoCurveOptimizerVoltageOffset2",
        "PP_AutoCurveOptimizerVoltageOffset3",
        "PP_AutoCurveOptimizerVoltageOffset4",
        "PP_AutoCurveOptimizerVoltageOffset5",
        "PP_AutoCurveOptimizerVoltageOffset6",
        "IsAdvancedControl", "IsAdvancedControlCached",
        "EscapeInputState",
    ]
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, adapter_path, 0,
                            winreg.KEY_READ) as k:
            for name in od_names:
                try:
                    v, t = winreg.QueryValueEx(k, name)
                    if t in (winreg.REG_DWORD,) and isinstance(v, int):
                        print(f"  {name:48s} = {v} (0x{v:08X})")
                    elif t in (winreg.REG_SZ,) and isinstance(v, str):
                        print(f"  {name:48s} = \"{v}\"")
                    elif isinstance(v, (bytes, bytearray)):
                        print(f"  {name:48s} = BINARY({len(v)}b) "
                              f"[{' '.join(f'{x:02X}' for x in v[:16])}]")
                    else:
                        print(f"  {name:48s} = {v!r}")
                except OSError:
                    print(f"  {name:48s} = (not present)")
    except OSError as e:
        print(f"  Failed to open key: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
