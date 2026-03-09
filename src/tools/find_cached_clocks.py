r"""
Search the Windows registry for cached PP table clock values.

Searches AMD GPU driver registry keys for specific clock frequencies
(BaseClockAc, GameClockAc, BoostClockAc) stored as DWORDs, strings,
or embedded in binary blobs (u16/u32 little-endian).

Usage (run as admin for full access):
  py find_cached_clocks.py
  py find_cached_clocks.py --deep
  py find_cached_clocks.py --dump-adapter
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple, Union

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import winreg
except ImportError:
    print("ERROR: winreg not available (Windows only)", file=sys.stderr)
    sys.exit(1)

# ---- Clock targets ----

CLOCK_TARGETS: Dict[str, int] = {
    "BaseClockAc":  1920,
    "GameClockAc":  2840,
    "BoostClockAc": 3320,
}


def _build_byte_patterns(targets: Dict[str, int]) -> Dict[str, List[Tuple[str, bytes]]]:
    patterns: Dict[str, List[Tuple[str, bytes]]] = {}
    for label, mhz in targets.items():
        pats = []
        pats.append(("u16_LE", struct.pack("<H", mhz)))
        pats.append(("u32_LE", struct.pack("<I", mhz)))
        pats.append(("u16_BE", struct.pack(">H", mhz)))
        # 10kHz units (some AMD tables): mhz * 100
        khz10 = mhz * 100
        if khz10 <= 0xFFFFFFFF:
            pats.append(("u32_LE_10kHz", struct.pack("<I", khz10)))
        # kHz: mhz * 1000
        khz = mhz * 1000
        if khz <= 0xFFFFFFFF:
            pats.append(("u32_LE_kHz", struct.pack("<I", khz)))
        # 10MHz units (some OD tables): mhz / 10
        if mhz % 10 == 0:
            unit10m = mhz // 10
            pats.append(("u16_LE_10MHz", struct.pack("<H", unit10m)))
            pats.append(("u32_LE_10MHz", struct.pack("<I", unit10m)))
        patterns[label] = pats
    return patterns


BYTE_PATTERNS = _build_byte_patterns(CLOCK_TARGETS)

# ---- Registry paths to scan ----

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
)

DEEP_SCAN_ROOTS_HKLM = [
    r"SOFTWARE\AMD",
    r"SOFTWARE\ATI Technologies",
    r"SYSTEM\CurrentControlSet\Services\amdkmdag",
    r"SYSTEM\CurrentControlSet\Services\amdkmdap",
    r"SYSTEM\CurrentControlSet\Services\amdwddmg",
    r"SYSTEM\CurrentControlSet\Enum\PCI",
    r"SYSTEM\CurrentControlSet\Hardware Profiles",
]

DEEP_SCAN_ROOTS_HKCU = [
    r"SOFTWARE\AMD",
    r"SOFTWARE\ATI Technologies",
    r"SOFTWARE\AMD\DVS",
    r"SOFTWARE\AMD\CN",
]

# ---- Helpers ----

def _type_name(vtype: int) -> str:
    names = {
        winreg.REG_NONE: "REG_NONE",
        winreg.REG_SZ: "REG_SZ",
        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
        winreg.REG_BINARY: "REG_BINARY",
        winreg.REG_DWORD: "REG_DWORD",
        winreg.REG_DWORD_BIG_ENDIAN: "REG_DWORD_BE",
        winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
        winreg.REG_QWORD: "REG_QWORD",
    }
    return names.get(vtype, f"REG_TYPE_{vtype}")


def _hex_preview(b: bytes, n: int = 32) -> str:
    return " ".join(f"{x:02X}" for x in b[:n])


def _hive_name(hive) -> str:
    if hive == winreg.HKEY_LOCAL_MACHINE:
        return "HKLM"
    if hive == winreg.HKEY_CURRENT_USER:
        return "HKCU"
    return f"HIVE_{hive}"


def _enum_subkeys(hive, path: str):
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    yield winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
    except (OSError, PermissionError):
        return


def _enum_values(hive, path: str):
    """Yield (name, value, type) for all values under hive\\path."""
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    yield winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
    except (OSError, PermissionError):
        return


# ---- Match finding ----

class Match:
    __slots__ = ("hive_name", "key_path", "value_name", "value_type",
                 "clock_label", "clock_mhz", "match_kind", "offset", "context")

    def __init__(self, hive_name, key_path, value_name, value_type, clock_label,
                 clock_mhz, match_kind, offset=None, context=""):
        self.hive_name = hive_name
        self.key_path = key_path
        self.value_name = value_name
        self.value_type = value_type
        self.clock_label = clock_label
        self.clock_mhz = clock_mhz
        self.match_kind = match_kind
        self.offset = offset
        self.context = context


def _check_value(hive_name: str, key_path: str, name: str,
                 value, vtype: int) -> List[Match]:
    matches = []

    if vtype in (winreg.REG_DWORD, winreg.REG_DWORD_BIG_ENDIAN, winreg.REG_QWORD):
        if isinstance(value, int):
            for label, mhz in CLOCK_TARGETS.items():
                if value == mhz:
                    matches.append(Match(
                        hive_name, key_path, name, _type_name(vtype), label, mhz,
                        "DWORD_exact", context=f"value={value} (0x{value:X})"
                    ))
                if value == mhz * 100:
                    matches.append(Match(
                        hive_name, key_path, name, _type_name(vtype), label, mhz,
                        "DWORD_10kHz", context=f"value={value} (0x{value:X}) = {mhz}*100"
                    ))
                if value == mhz * 1000:
                    matches.append(Match(
                        hive_name, key_path, name, _type_name(vtype), label, mhz,
                        "DWORD_kHz", context=f"value={value} (0x{value:X}) = {mhz}*1000"
                    ))
                if mhz % 10 == 0 and value == mhz // 10:
                    matches.append(Match(
                        hive_name, key_path, name, _type_name(vtype), label, mhz,
                        "DWORD_10MHz", context=f"value={value} (0x{value:X}) = {mhz}/10"
                    ))

    elif vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
        for label, mhz in CLOCK_TARGETS.items():
            if str(mhz) in value:
                matches.append(Match(
                    hive_name, key_path, name, _type_name(vtype), label, mhz,
                    "STRING_contains", context=f'"{value[:200]}"'
                ))

    elif vtype == winreg.REG_MULTI_SZ and isinstance(value, list):
        full = "\n".join(str(x) for x in value)
        for label, mhz in CLOCK_TARGETS.items():
            if str(mhz) in full:
                matches.append(Match(
                    hive_name, key_path, name, "REG_MULTI_SZ", label, mhz,
                    "MULTI_SZ_contains", context=f'"{full[:200]}"'
                ))

    elif isinstance(value, (bytes, bytearray)):
        blob = bytes(value)
        for label, patterns in BYTE_PATTERNS.items():
            mhz = CLOCK_TARGETS[label]
            for pat_name, pat_bytes in patterns:
                idx = 0
                while True:
                    pos = blob.find(pat_bytes, idx)
                    if pos < 0:
                        break
                    start = max(0, pos - 8)
                    end = min(len(blob), pos + len(pat_bytes) + 8)
                    ctx_hex = _hex_preview(blob[start:end], 48)
                    matches.append(Match(
                        hive_name, key_path, name,
                        _type_name(winreg.REG_BINARY), label, mhz,
                        f"BINARY_{pat_name}",
                        offset=pos,
                        context=f"blob_size={len(blob)} offset=0x{pos:X} nearby=[{ctx_hex}]"
                    ))
                    idx = pos + 1

    return matches


def _scan_key_recursive(hive, hive_name: str, path: str,
                        depth: int = 0, max_depth: int = 10,
                        counter: Optional[list] = None) -> List[Match]:
    results = []

    for name, value, vtype in _enum_values(hive, path):
        results.extend(_check_value(hive_name, path, name, value, vtype))

    if counter is not None:
        counter[0] += 1

    if depth < max_depth:
        for sub in _enum_subkeys(hive, path):
            sub_path = path + "\\" + sub
            results.extend(_scan_key_recursive(
                hive, hive_name, sub_path, depth + 1, max_depth, counter
            ))

    return results


# ---- Adapter dump ----

def dump_adapter_values(hive, path: str) -> None:
    """Print ALL values in an adapter key (non-recursive, just this key)."""
    print(f"\n  All values in {path}:")
    print(f"  {'─' * 90}")
    count = 0
    binary_count = 0
    for name, value, vtype in _enum_values(hive, path):
        count += 1
        tn = _type_name(vtype)
        if vtype in (winreg.REG_DWORD, winreg.REG_DWORD_BIG_ENDIAN) and isinstance(value, int):
            print(f"    {name}: {tn} = {value} (0x{value:08X})")
        elif vtype == winreg.REG_QWORD and isinstance(value, int):
            print(f"    {name}: {tn} = {value} (0x{value:016X})")
        elif vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
            print(f'    {name}: {tn} = "{value[:120]}"')
        elif vtype == winreg.REG_MULTI_SZ and isinstance(value, list):
            joined = "; ".join(str(x) for x in value[:5])
            print(f"    {name}: {tn} = [{joined}]{'...' if len(value) > 5 else ''}")
        elif isinstance(value, (bytes, bytearray)):
            binary_count += 1
            b = bytes(value)
            print(f"    {name}: {tn} ({len(b)} bytes) = [{_hex_preview(b, 24)}]{'...' if len(b) > 24 else ''}")
        else:
            print(f"    {name}: {tn} = {value!r}")

    print(f"  ─ {count} values total, {binary_count} binary ─")

    # Also list subkeys
    subkeys = list(_enum_subkeys(hive, path))
    if subkeys:
        print(f"\n  Subkeys ({len(subkeys)}):")
        for sk in subkeys:
            print(f"    {sk}")


# ---- Main scan logic ----

def find_amd_adapter_path(hive) -> Optional[str]:
    """Find first AMD adapter key path."""
    for sub in _enum_subkeys(hive, _DISPLAY_CLASS_PATH):
        if not sub.strip().isdigit():
            continue
        sub_path = _DISPLAY_CLASS_PATH + "\\" + sub
        try:
            with winreg.OpenKey(hive, sub_path, 0, winreg.KEY_READ) as k:
                try:
                    mdid, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
                    if isinstance(mdid, str) and "VEN_1002" in mdid.upper():
                        return sub_path
                except OSError:
                    pass
        except OSError:
            continue
    return None


def scan_display_adapters(*, amd_only: bool = True) -> List[Match]:
    all_matches = []
    hive = winreg.HKEY_LOCAL_MACHINE

    for sub in _enum_subkeys(hive, _DISPLAY_CLASS_PATH):
        sub_path = _DISPLAY_CLASS_PATH + "\\" + sub

        if amd_only:
            try:
                with winreg.OpenKey(hive, sub_path, 0, winreg.KEY_READ) as k:
                    try:
                        mdid, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
                        if not (isinstance(mdid, str) and "VEN_1002" in mdid.upper()):
                            continue
                    except OSError:
                        continue
            except OSError:
                continue

        counter = [0]
        matches = _scan_key_recursive(hive, "HKLM", sub_path, counter=counter)
        print(f"    {sub_path}: scanned {counter[0]} keys, {len(matches)} hit(s)")
        all_matches.extend(matches)

    return all_matches


def scan_roots(hive, hive_name: str, roots: List[str],
               max_depth: int = 8) -> List[Match]:
    all_matches = []
    for root_path in roots:
        counter = [0]
        print(f"  Scanning {hive_name}\\{root_path} ...", end="", flush=True)
        matches = _scan_key_recursive(
            hive, hive_name, root_path, max_depth=max_depth, counter=counter
        )
        all_matches.extend(matches)
        print(f" {counter[0]} keys, {len(matches)} hit(s)")
    return all_matches


def _print_matches(matches: List[Match], title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)

    if not matches:
        print("  (no matches found)")
        return

    grouped: Dict[str, List[Match]] = {}
    for m in matches:
        gkey = f"{m.hive_name}\\{m.key_path} :: {m.value_name}"
        grouped.setdefault(gkey, []).append(m)

    for gkey, group in grouped.items():
        print()
        print(f"  {gkey}")
        print(f"  {'~' * min(len(gkey), 96)}")
        for m in group:
            offset_str = f" @0x{m.offset:X}" if m.offset is not None else ""
            print(f"    [{m.clock_label:14s}] {m.clock_mhz:5d} MHz  "
                  f"type={m.value_type}  match={m.match_kind}{offset_str}")
            if m.context:
                print(f"      {m.context}")

    print()
    print("-" * 60)
    for label, mhz in CLOCK_TARGETS.items():
        count = sum(1 for m in matches if m.clock_label == label)
        unique_values = {(m.hive_name, m.key_path, m.value_name)
                         for m in matches if m.clock_label == label}
        print(f"  {label:14s} ({mhz} MHz): {count} hit(s) across "
              f"{len(unique_values)} value(s)")
    print(f"  TOTAL: {len(matches)} hit(s)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Search registry for cached PP table clocks."
    )
    ap.add_argument("--deep", action="store_true",
                    help="Also scan SOFTWARE\\AMD, Services, PCI enum, HKCU, etc.")
    ap.add_argument("--all-adapters", action="store_true",
                    help="Scan all display adapters, not just AMD (VEN_1002)")
    ap.add_argument("--dump-adapter", action="store_true",
                    help="Dump all values in the AMD adapter registry key")
    ap.add_argument("--base", type=int, default=1920, help="BaseClockAc in MHz")
    ap.add_argument("--game", type=int, default=2840, help="GameClockAc in MHz")
    ap.add_argument("--boost", type=int, default=3320, help="BoostClockAc in MHz")
    args = ap.parse_args()

    CLOCK_TARGETS["BaseClockAc"] = args.base
    CLOCK_TARGETS["GameClockAc"] = args.game
    CLOCK_TARGETS["BoostClockAc"] = args.boost
    global BYTE_PATTERNS
    BYTE_PATTERNS = _build_byte_patterns(CLOCK_TARGETS)

    print("=" * 100)
    print("  Registry Clock Cache Search")
    print("=" * 100)
    print()
    for label, mhz in CLOCK_TARGETS.items():
        u16_le = struct.pack("<H", mhz)
        u32_le = struct.pack("<I", mhz)
        print(f"  {label:14s} = {mhz:5d} MHz  (0x{mhz:04X})  "
              f"u16_LE=[{u16_le[0]:02X} {u16_le[1]:02X}]  "
              f"u32_LE=[{u32_le[0]:02X} {u32_le[1]:02X} "
              f"{u32_le[2]:02X} {u32_le[3]:02X}]")
    print()

    # Dump adapter key contents if requested
    if args.dump_adapter:
        print("─" * 100)
        print("  AMD Adapter Key Dump")
        print("─" * 100)
        amd_path = find_amd_adapter_path(winreg.HKEY_LOCAL_MACHINE)
        if amd_path:
            print(f"  Found AMD adapter: {amd_path}")
            dump_adapter_values(winreg.HKEY_LOCAL_MACHINE, amd_path)
            # Also dump subkeys contents
            for sk in _enum_subkeys(winreg.HKEY_LOCAL_MACHINE, amd_path):
                sk_path = amd_path + "\\" + sk
                dump_adapter_values(winreg.HKEY_LOCAL_MACHINE, sk_path)
        else:
            print("  (no AMD adapter found)")
        print()

    t0 = time.time()

    # Phase 1: Display adapter class keys
    print("Phase 1: Display adapter keys (HKLM)")
    print(f"  {_DISPLAY_CLASS_PATH}")
    print(f"  AMD only: {not args.all_adapters}")
    adapter_matches = scan_display_adapters(amd_only=not args.all_adapters)
    _print_matches(adapter_matches, "Phase 1 Results: Display Adapter Keys")

    # Phase 2: Deep scan
    deep_matches: List[Match] = []
    if args.deep:
        print()
        print("Phase 2: Deep scan — HKLM additional roots")
        hklm_matches = scan_roots(
            winreg.HKEY_LOCAL_MACHINE, "HKLM", DEEP_SCAN_ROOTS_HKLM, max_depth=8
        )
        deep_matches.extend(hklm_matches)

        print()
        print("Phase 3: Deep scan — HKCU (per-user AMD/Radeon settings)")
        hkcu_matches = scan_roots(
            winreg.HKEY_CURRENT_USER, "HKCU", DEEP_SCAN_ROOTS_HKCU, max_depth=8
        )
        deep_matches.extend(hkcu_matches)

        _print_matches(deep_matches, "Deep Scan Results (HKLM + HKCU)")

    dt = time.time() - t0

    all_matches = adapter_matches + deep_matches
    print()
    print("=" * 100)
    print(f"  GRAND TOTAL: {len(all_matches)} match(es) in {dt:.2f}s")
    if all_matches:
        unique_vals = {(m.hive_name, m.key_path, m.value_name) for m in all_matches}
        print(f"  Unique registry values with hits: {len(unique_vals)}")
        for hn, kp, vn in sorted(unique_vals):
            hits_here = [m for m in all_matches
                         if m.hive_name == hn and m.key_path == kp
                         and m.value_name == vn]
            clocks_found = sorted({m.clock_label for m in hits_here})
            print(f"    {hn}\\{kp}")
            print(f"      value: {vn}")
            print(f"      clocks: {', '.join(clocks_found)}")
            for m in hits_here:
                if m.offset is not None:
                    print(f"        {m.clock_label} @ offset 0x{m.offset:X} "
                          f"({m.match_kind})")
    else:
        print("  No cached clock values found in any scanned location.")
        print()
        print("  Possible explanations:")
        print("    1. Driver stores clocks in a different unit or encoding")
        print("    2. Values are in a registry hive we didn't scan")
        print("    3. Driver caches the table in a file, not the registry")
        print("       (check C:\\Windows\\System32\\AMD, driver store, or appdata)")
        print("    4. The PP table cache is only held in kernel memory (paged pool)")
        print()
        print("  Next steps:")
        print("    - Run with --dump-adapter to see what's in the adapter key")
        print("    - Check for files: C:\\Windows\\System32\\AMD\\**")
        print("    - Check: C:\\Windows\\System32\\DriverStore\\FileRepository\\*amd*\\**")
    print("=" * 100)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
