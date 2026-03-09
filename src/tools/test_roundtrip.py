r"""
Round-trip integrity test for SpptCache and CnEscapeCache.

Reads PP_PhmSoftPowerPlayTable and PP_CNEscapeInput from the AMD adapter
registry key, parses them through SpptCache / CnEscapeCache, and verifies
that to_bytes() reproduces the original blob byte-for-byte.

This MUST pass before any write-back operations are trusted.

Usage (run as admin):
  py test_roundtrip.py
  py test_roundtrip.py --dump-adapter
  py test_roundtrip.py --rom bios/vbios.rom
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
import traceback
from typing import Dict, List, Optional, Tuple

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    import winreg
except ImportError:
    winreg = None

from src.io.pptable_sources import (
    enumerate_display_adapters,
    read_registry_values,
)
from src.tools.sppt_cache import SpptCache, SpptField
from src.tools.cn_escape import CnEscapeCache, CnEscapeField


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS = "\033[1;32mPASS\033[0m"
_FAIL = "\033[1;31mFAIL\033[0m"
_SKIP = "\033[1;33mSKIP\033[0m"
_INFO = "\033[1;36mINFO\033[0m"


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hex_preview(b: bytes, n: int = 32) -> str:
    return " ".join(f"{x:02X}" for x in b[:n])


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.messages: List[str] = []

    def ok(self, msg: str) -> None:
        self.passed += 1
        self.messages.append(f"  {_PASS}  {msg}")

    def fail(self, msg: str) -> None:
        self.failed += 1
        self.messages.append(f"  {_FAIL}  {msg}")

    def skip(self, msg: str) -> None:
        self.skipped += 1
        self.messages.append(f"  {_SKIP}  {msg}")

    def info(self, msg: str) -> None:
        self.messages.append(f"  {_INFO}  {msg}")

    def print_summary(self) -> None:
        status = _PASS if self.failed == 0 else _FAIL
        total = self.passed + self.failed + self.skipped
        print(f"\n{'='*80}")
        print(f"  {self.name}: {status}  "
              f"({self.passed} passed, {self.failed} failed, "
              f"{self.skipped} skipped / {total} total)")
        print(f"{'='*80}")
        for m in self.messages:
            print(m)
        print()


# ---------------------------------------------------------------------------
# Adapter discovery
# ---------------------------------------------------------------------------

def find_amd_adapter_key() -> Optional[str]:
    """Find the first AMD (VEN_1002) display adapter registry key."""
    for a in enumerate_display_adapters():
        mdid = a.get("MatchingDeviceId", "")
        if "VEN_1002" in mdid.upper():
            return a["key_path"]
    return None


def dump_adapter_info(key_path: str) -> Dict[str, str]:
    """Read basic adapter info for display."""
    info: Dict[str, str] = {"key_path": key_path}
    if winreg is None:
        return info
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                            winreg.KEY_READ) as k:
            for name in ("MatchingDeviceId", "DriverDesc", "ProviderName",
                         "DriverVersion"):
                try:
                    v, t = winreg.QueryValueEx(k, name)
                    if isinstance(v, str):
                        info[name] = v
                except OSError:
                    pass
    except OSError:
        pass
    return info


def dump_adapter_values(key_path: str) -> None:
    """Print all values in the adapter key (mirrors find_cached_clocks --dump-adapter)."""
    if winreg is None:
        print("  (winreg not available)")
        return

    print(f"\n  All values in {key_path}:")
    print(f"  {'-'*90}")
    count = 0
    bin_count = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                            winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    name, value, vtype = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                count += 1

                if vtype in (winreg.REG_DWORD,) and isinstance(value, int):
                    print(f"    {name}: DWORD = {value} (0x{value:08X})")
                elif vtype == winreg.REG_QWORD and isinstance(value, int):
                    print(f"    {name}: QWORD = {value} (0x{value:016X})")
                elif vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                    print(f'    {name}: SZ = "{value[:120]}"')
                elif isinstance(value, (bytes, bytearray)):
                    bin_count += 1
                    b = bytes(value)
                    print(f"    {name}: BINARY ({len(b)} bytes) "
                          f"[{_hex_preview(b, 24)}]{'...' if len(b) > 24 else ''}")
                else:
                    print(f"    {name}: type={vtype} = {value!r}")
    except OSError as e:
        print(f"    (failed: {e})")

    print(f"  -- {count} values total, {bin_count} binary --")


# ---------------------------------------------------------------------------
# SPPT round-trip tests
# ---------------------------------------------------------------------------

def test_sppt_roundtrip(blob: bytes, source: str, result: TestResult) -> None:
    """Verify SpptCache parse/serialize round-trip on a raw PP table blob."""
    result.info(f"SPPT source: {source}, blob size: {len(blob)} bytes, "
                f"sha256: {_sha256(blob)[:16]}...")

    # 1) Construction
    try:
        cache = SpptCache.from_bytes(blob, source=source)
    except Exception as e:
        result.fail(f"SpptCache.from_bytes() raised: {e}")
        return
    result.ok(f"SpptCache constructed: {len(cache.fields)} fields parsed")

    if not cache.fields:
        result.skip("No fields parsed (UPP not available and fallback failed)")
        return

    # 2) to_bytes() identity
    roundtrip = cache.to_bytes()
    if roundtrip == blob:
        result.ok(f"to_bytes() == original ({len(blob)} bytes)")
    else:
        result.fail(f"to_bytes() DIFFERS from original! "
                    f"(len {len(roundtrip)} vs {len(blob)})")
        _report_blob_diff(blob, roundtrip, result)

    # 3) original_bytes property
    if cache.original_bytes == blob:
        result.ok("original_bytes == input blob")
    else:
        result.fail("original_bytes != input blob")

    # 4) is_modified should be False on fresh parse
    if not cache.is_modified:
        result.ok("is_modified == False (no changes yet)")
    else:
        result.fail("is_modified == True on fresh parse (unexpected)")

    # 5) diff() should be empty
    changes = cache.diff()
    if not changes:
        result.ok("diff() returns empty list (no modifications)")
    else:
        result.fail(f"diff() returned {len(changes)} unexpected change(s)")
        for name, orig, cur in changes[:5]:
            result.info(f"  diff: {name} orig={orig} cur={cur}")

    # 6) Per-field value consistency: re-read each field from blob and compare
    field_mismatches = 0
    for f in cache.fields.values():
        try:
            raw_val = struct.unpack_from(f"<{f.fmt}", blob, f.offset)[0]
        except struct.error:
            continue
        if raw_val != f.value:
            field_mismatches += 1
            if field_mismatches <= 3:
                result.fail(f"Field '{f.name}' at 0x{f.offset:04X}: "
                            f"parsed={f.value} vs raw={raw_val}")
    if field_mismatches == 0:
        result.ok(f"All {len(cache.fields)} field values match raw blob reads")
    elif field_mismatches > 3:
        result.fail(f"... and {field_mismatches - 3} more field mismatches")

    # 7) set_field + reset round-trip
    test_field = next(iter(cache.fields.values()), None)
    if test_field:
        old_val = test_field.value
        test_val = (old_val + 1) & ((1 << (test_field.size * 8)) - 1)
        cache.set_field(test_field.name, test_val)
        if cache.is_modified:
            result.ok(f"set_field('{test_field.name}', {test_val}) marks as modified")
        else:
            result.fail("set_field did not mark blob as modified")

        modified_bytes = cache.to_bytes()
        if modified_bytes != blob:
            result.ok("Modified blob differs from original (expected)")
        else:
            result.fail("Modified blob is identical to original (set_field had no effect)")

        cache.reset()
        if cache.to_bytes() == blob:
            result.ok("reset() restores original blob")
        else:
            result.fail("reset() did NOT restore original blob")

        if not cache.is_modified:
            result.ok("is_modified == False after reset()")
        else:
            result.fail("is_modified == True after reset()")

    # 8) clone() independence
    clone = cache.clone()
    if clone.to_bytes() == cache.to_bytes():
        result.ok("clone().to_bytes() == original.to_bytes()")
    else:
        result.fail("clone() produced different bytes")

    if test_field:
        clone.set_field(test_field.name, test_val)
        if cache.to_bytes() == blob:
            result.ok("Mutating clone does not affect original")
        else:
            result.fail("Mutating clone corrupted original (shallow copy bug)")
        clone.reset()

    # 9) Field group queries return consistent subsets
    all_fields = set(f.name for f in cache.fields.values())
    grouped = set()
    for group in ("clock", "power", "tdc", "temp", "od", "fan", "meta"):
        grouped.update(f.name for f in cache.fields_by_group(group))
    ungrouped = all_fields - grouped
    if not ungrouped:
        result.ok("All fields belong to a known group")
    else:
        result.info(f"{len(ungrouped)} field(s) in non-standard groups: "
                    f"{', '.join(sorted(list(ungrouped)[:5]))}")

    # 10) Dump summary for visual inspection
    result.info(cache.summary())


def test_cn_escape_roundtrip(blob: bytes, source: str, result: TestResult) -> None:
    """Verify CnEscapeCache parse/serialize round-trip on a raw CN escape blob."""
    result.info(f"CN Escape source: {source}, blob size: {len(blob)} bytes, "
                f"sha256: {_sha256(blob)[:16]}...")

    # 1) Construction
    try:
        cache = CnEscapeCache.from_bytes(blob, source=source)
    except Exception as e:
        result.fail(f"CnEscapeCache.from_bytes() raised: {e}")
        return
    result.ok(f"CnEscapeCache constructed: {len(cache.fields)} fields "
              f"({len(cache.known_fields())} known + {len(cache.unknown_fields())} auto)")

    # 2) to_bytes() identity
    roundtrip = cache.to_bytes()
    if roundtrip == blob:
        result.ok(f"to_bytes() == original ({len(blob)} bytes)")
    else:
        result.fail(f"to_bytes() DIFFERS from original! "
                    f"(len {len(roundtrip)} vs {len(blob)})")
        _report_blob_diff(blob, roundtrip, result)

    # 3) original_bytes property
    if cache.original_bytes == blob:
        result.ok("original_bytes == input blob")
    else:
        result.fail("original_bytes != input blob")

    # 4) is_modified should be False
    if not cache.is_modified:
        result.ok("is_modified == False (no changes yet)")
    else:
        result.fail("is_modified == True on fresh parse")

    # 5) diff() empty
    changes = cache.diff()
    if not changes:
        result.ok("diff() returns empty list")
    else:
        result.fail(f"diff() returned {len(changes)} unexpected change(s)")

    # 6) Per-field value consistency
    field_mismatches = 0
    for f in cache.fields.values():
        try:
            raw_val = struct.unpack_from(f"<{f.fmt}", blob, f.offset)[0]
        except struct.error:
            continue
        if raw_val != f.value:
            field_mismatches += 1
            if field_mismatches <= 3:
                result.fail(f"Field '{f.name}' at 0x{f.offset:04X}: "
                            f"parsed={f.value} vs raw={raw_val}")
    if field_mismatches == 0:
        result.ok(f"All {len(cache.fields)} field values match raw blob reads")
    elif field_mismatches > 3:
        result.fail(f"... and {field_mismatches - 3} more field mismatches")

    # 7) Size header self-consistency (CNEscape first dword should equal blob length)
    size_field = cache.get_value("Size")
    if size_field is not None:
        if size_field == len(blob):
            result.ok(f"Size header (0x{size_field:X}) == blob length ({len(blob)})")
        else:
            result.info(f"Size header (0x{size_field:X}) != blob length ({len(blob)}) "
                        f"(may be normal)")

    # 8) Signed field round-trip (GfxclkFoffset, VoltageOffset)
    for fname in ("GfxclkFoffset", "VoltageOffset"):
        f = cache.get_field(fname)
        if f is None:
            continue
        display_val = f.display_value_int
        ok = cache.set_field(fname, display_val)
        if ok:
            new_raw = struct.unpack_from(f"<{f.fmt}", cache._blob, f.offset)[0]
            orig_raw = struct.unpack_from(f"<{f.fmt}", blob, f.offset)[0]
            if new_raw == orig_raw:
                result.ok(f"Signed field '{fname}': display_value={display_val} "
                          f"-> pack -> raw=0x{new_raw:08X} matches original")
            else:
                result.fail(f"Signed field '{fname}': re-pack gave 0x{new_raw:08X}, "
                            f"expected 0x{orig_raw:08X}")
        cache.reset()

    # 9) set_field + reset
    test_field = None
    for f in cache.fields.values():
        if f.group not in ("header",):
            test_field = f
            break

    if test_field:
        old_val = test_field.value
        if test_field.signed:
            test_val = -1
        else:
            test_val = (old_val + 1) & ((1 << (test_field.size * 8)) - 1)

        cache.set_field(test_field.name, test_val)
        if cache.is_modified:
            result.ok(f"set_field('{test_field.name}', {test_val}) marks as modified")
        else:
            result.fail("set_field did not mark as modified")

        cache.reset()
        if cache.to_bytes() == blob:
            result.ok("reset() restores original blob")
        else:
            result.fail("reset() did NOT restore original blob")

    # 10) clone() independence
    clone = cache.clone()
    if clone.to_bytes() == cache.to_bytes():
        result.ok("clone().to_bytes() == original")
    else:
        result.fail("clone() bytes differ")

    # 11) hex export/import round-trip
    hex_str = cache.export_hex()
    reimport = CnEscapeCache.from_hex(hex_str, source="hex_roundtrip")
    if reimport is not None and reimport.to_bytes() == blob:
        result.ok("export_hex() -> from_hex() round-trips correctly")
    elif reimport is None:
        result.fail("from_hex() returned None on its own export")
    else:
        result.fail("export_hex() -> from_hex() produced different bytes")

    # 12) Summary for visual inspection
    result.info(cache.summary())


# ---------------------------------------------------------------------------
# Diff reporting
# ---------------------------------------------------------------------------

def _report_blob_diff(orig: bytes, modified: bytes, result: TestResult) -> None:
    """Report first few byte-level differences between two blobs."""
    min_len = min(len(orig), len(modified))
    diffs = []
    for i in range(min_len):
        if orig[i] != modified[i]:
            diffs.append(i)
            if len(diffs) >= 10:
                break
    if len(orig) != len(modified):
        result.info(f"  Length differs: {len(orig)} vs {len(modified)}")
    if diffs:
        for off in diffs:
            result.info(f"  Byte 0x{off:04X}: orig=0x{orig[off]:02X} "
                        f"got=0x{modified[off]:02X}")
        if len(diffs) >= 10:
            result.info(f"  ... (showing first 10 differences)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Round-trip integrity test for SpptCache / CnEscapeCache."
    )
    ap.add_argument("--dump-adapter", action="store_true",
                    help="Also dump all values in the AMD adapter registry key")
    ap.add_argument("--rom", default=None,
                    help="Path to VBIOS ROM file to test SPPT extraction from ROM")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print field dumps for visual inspection")
    args = ap.parse_args()

    print("=" * 80)
    print("  Driver Cache Round-Trip Integrity Test")
    print("=" * 80)
    print()

    # -- Find adapter --
    adapter_key = find_amd_adapter_key()
    if adapter_key is None:
        print(f"  {_FAIL}  No AMD adapter found in registry.")
        print("  This test requires an AMD GPU with the display driver installed.")
        return 1

    info = dump_adapter_info(adapter_key)
    print(f"  Adapter key : {adapter_key}")
    print(f"  Device ID   : {info.get('MatchingDeviceId', '(unknown)')}")
    print(f"  Description : {info.get('DriverDesc', '(unknown)')}")
    print(f"  Driver ver  : {info.get('DriverVersion', '(unknown)')}")

    if args.dump_adapter:
        dump_adapter_values(adapter_key)

    # -- Read blobs from registry --
    vals = read_registry_values(
        adapter_key,
        value_names=("PP_PhmSoftPowerPlayTable", "PP_CNEscapeInput"),
    )

    sppt_blob = vals.get("PP_PhmSoftPowerPlayTable")
    cn_blob = vals.get("PP_CNEscapeInput")

    print()
    print(f"  PP_PhmSoftPowerPlayTable : "
          f"{'present, ' + str(len(sppt_blob)) + ' bytes' if isinstance(sppt_blob, bytes) else 'not present'}")
    print(f"  PP_CNEscapeInput         : "
          f"{'present, ' + str(len(cn_blob)) + ' bytes' if isinstance(cn_blob, bytes) else 'not present'}")

    results: List[TestResult] = []

    # -- SPPT from registry --
    sppt_result = TestResult("SPPT Registry Round-Trip")
    if isinstance(sppt_blob, bytes) and len(sppt_blob) >= 64:
        test_sppt_roundtrip(sppt_blob, f"registry:{adapter_key}", sppt_result)
        if args.verbose:
            cache = SpptCache.from_bytes(sppt_blob)
            print(cache.dump_fields())
    else:
        sppt_result.skip("PP_PhmSoftPowerPlayTable not present in registry "
                         "(this is normal if no SPPT override has been written)")
    results.append(sppt_result)

    # -- SPPT from VBIOS ROM --
    sppt_rom_result = TestResult("SPPT VBIOS ROM Round-Trip")
    rom_path = args.rom
    if rom_path is None:
        default_rom = os.path.join(_project_root, "bios", "vbios.rom")
        if os.path.isfile(default_rom):
            rom_path = default_rom

    if rom_path and os.path.isfile(rom_path):
        try:
            cache = SpptCache.from_vbios(rom_path)
            if cache is not None:
                test_sppt_roundtrip(cache.original_bytes, f"vbios:{rom_path}", sppt_rom_result)
                if args.verbose:
                    print(cache.dump_fields())
            else:
                sppt_rom_result.skip(f"No PP table found in ROM: {rom_path}")
        except Exception as e:
            sppt_rom_result.fail(f"Exception loading ROM: {e}")
            traceback.print_exc()
    else:
        sppt_rom_result.skip("No VBIOS ROM file available (use --rom path/to/vbios.rom)")
    results.append(sppt_rom_result)

    # -- CN Escape from registry --
    cn_result = TestResult("CN Escape Registry Round-Trip")
    if isinstance(cn_blob, bytes) and len(cn_blob) >= 16:
        test_cn_escape_roundtrip(cn_blob, f"registry:{adapter_key}", cn_result)
        if args.verbose:
            cache = CnEscapeCache.from_bytes(cn_blob)
            print(cache.dump_fields())
    else:
        cn_result.skip("PP_CNEscapeInput not present in registry "
                       "(Adrenalin may not have written OD settings yet)")
    results.append(cn_result)

    # -- Print all results --
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    for r in results:
        r.print_summary()
        total_passed += r.passed
        total_failed += r.failed
        total_skipped += r.skipped

    overall = _PASS if total_failed == 0 else _FAIL
    print("=" * 80)
    print(f"  OVERALL: {overall}  "
          f"({total_passed} passed, {total_failed} failed, "
          f"{total_skipped} skipped)")
    print("=" * 80)

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
