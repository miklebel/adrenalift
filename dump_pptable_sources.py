"""
Dump everything we can read about PPTable sources (ROM / registry / SMU).

This is a diagnostics tool meant to be run directly, e.g.:
  py dump_pptable_sources.py
  py dump_pptable_sources.py --smu
  py dump_pptable_sources.py --rom bios/vbios.rom --no-registry
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import time
from typing import Dict, Iterable, List, Optional, Tuple

import pptable_sources as src

try:
    import winreg  # type: ignore
except Exception:
    winreg = None  # type: ignore


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hex_preview(b: bytes, n: int) -> str:
    take = b[: max(0, int(n))]
    return " ".join(f"{x:02X}" for x in take)


def _print_kv(k: str, v: str) -> None:
    print(f"{k:22s}: {v}")


def _clock_candidates_u16_triples(
    blob: bytes,
    *,
    max_items: int = 15,
    min_mhz: int = 500,
    max_mhz: int = 6000,
) -> List[Tuple[int, int, int, int]]:
    """
    Scan blob for little-endian u16 triples that look like (base, game, boost).

    Returns:
        List of (count, base, game, boost) sorted by count desc.
    """
    # 2MB ROM => ~1M iterations; that's fine but keep output small.
    counts: Dict[Tuple[int, int, int], int] = {}
    ln = len(blob)
    for off in range(0, ln - 6, 2):
        base, game, boost = struct.unpack_from("<3H", blob, off)
        if base < min_mhz or boost > max_mhz:
            continue
        if not (base <= game <= boost):
            continue
        # Filter out flat / junk triples. Real (base,game,boost) are not all equal.
        if base == 0 or boost == 0 or (base == game == boost):
            continue
        # Avoid near-identical triples (often table padding / unrelated fields).
        if (boost - base) < 50:
            continue
        counts[(base, game, boost)] = counts.get((base, game, boost), 0) + 1

    items = [(cnt, *k) for k, cnt in counts.items()]
    items.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    return items[: max(0, int(max_items))]


def _describe_blob(
    name: str,
    blob: Optional[bytes],
    *,
    preview_bytes: int,
    max_clock_candidates: int,
    scan_clocks: bool,
) -> None:
    print()
    print("=" * 80)
    print(name)
    print("=" * 80)
    if blob is None:
        _print_kv("present", "no")
        return

    _print_kv("present", "yes")
    _print_kv("size", f"{len(blob)} bytes")
    _print_kv("sha256", _sha256(blob))
    _print_kv("preview", _hex_preview(blob, preview_bytes))

    if scan_clocks:
        t0 = time.time()
        cands = _clock_candidates_u16_triples(blob, max_items=max_clock_candidates)
        dt = (time.time() - t0) * 1000.0
        _print_kv("clock scan", f"{dt:.1f} ms")
        if not cands:
            _print_kv("clock candidates", "(none found)")
        else:
            print("clock candidates      : count  base  game  boost (MHz)")
            for cnt, base, game, boost in cands:
                print(f"  -                 {cnt:6d}  {base:4d}  {game:4d}  {boost:4d}")


def _dump_registry(
    *,
    preview_bytes: int,
    max_clock_candidates: int,
    scan_clocks: bool,
) -> None:
    adapters = src.enumerate_display_adapters()
    print()
    print("=" * 80)
    print("Registry: Display Class Adapters")
    print("=" * 80)

    if not adapters:
        print("(No adapters enumerated or winreg unavailable.)")
        return

    def parse_pci_ids(mdid: str) -> Dict[str, str]:
        md = mdid.upper()
        out: Dict[str, str] = {}
        for tag in ("VEN", "DEV", "SUBSYS", "REV"):
            m = re.search(rf"{tag}_[0-9A-F]{{4,8}}", md)
            if m:
                out[tag] = m.group(0)
        return out

    def dump_all_values(key_path: str) -> None:
        if winreg is None:
            return
        print()
        print("All registry values     :")
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as k:
                i = 0
                while True:
                    try:
                        name, value, vtype = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1

                    # Keep one-line output per value; binaries get summarized.
                    if vtype in (winreg.REG_DWORD, winreg.REG_DWORD_LITTLE_ENDIAN) and isinstance(value, int):
                        print(f"  - {name}: DWORD {value} (0x{value:08X})")
                    elif vtype == winreg.REG_QWORD and isinstance(value, int):
                        print(f"  - {name}: QWORD {value} (0x{value:016X})")
                    elif vtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        print(f"  - {name}: \"{value}\"")
                    elif vtype == winreg.REG_MULTI_SZ and isinstance(value, list):
                        joined = "; ".join(str(x) for x in value)
                        print(f"  - {name}: MULTI_SZ \"{joined}\"")
                    elif isinstance(value, (bytes, bytearray)):
                        b = bytes(value)
                        print(f"  - {name}: BINARY {len(b)} bytes sha256={_sha256(b)} preview={_hex_preview(b, preview_bytes)}")
                    else:
                        # Fallback: show type id and repr.
                        print(f"  - {name}: type={vtype} value={value!r}")
        except OSError as e:
            print(f"  (failed to enumerate values: {e})")

    for a in adapters:
        key_path = a.get("key_path", "?")
        mdid = a.get("MatchingDeviceId", "")
        is_amd = "VEN_1002" in mdid.upper()
        pci_ids = parse_pci_ids(mdid) if mdid else {}

        print()
        print("-" * 80)
        _print_kv("key_path", key_path)
        if mdid:
            _print_kv("MatchingDeviceId", mdid)
        if pci_ids:
            _print_kv("pci ids", " ".join(pci_ids.get(k, "") for k in ("VEN", "DEV", "SUBSYS", "REV") if pci_ids.get(k)))
        if a.get("DriverDesc"):
            _print_kv("DriverDesc", a["DriverDesc"])
        if a.get("ProviderName"):
            _print_kv("ProviderName", a["ProviderName"])
        if a.get("DriverVersion"):
            _print_kv("DriverVersion", a["DriverVersion"])
        _print_kv("is_amd (VEN_1002)", "yes" if is_amd else "no")

        # Read the PP blob values if they exist (even for non-AMD, harmless).
        vals = src.read_registry_values(
            key_path,
            value_names=(
                "PP_PhmSoftPowerPlayTable",
                "PP_PhmPowerPlayTable",
            ),
        )
        if not vals:
            _print_kv("PP values", "(none present)")
        else:
            for vn, v in vals.items():
                if isinstance(v, (bytes, bytearray)):
                    _describe_blob(
                        f"Registry value: {vn} ({key_path})",
                        bytes(v),
                        preview_bytes=preview_bytes,
                        max_clock_candidates=max_clock_candidates,
                        scan_clocks=scan_clocks,
                    )
                else:
                    print()
                    print("=" * 80)
                    print(f"Registry value: {vn} ({key_path})")
                    print("=" * 80)
                    _print_kv("type", "str")
                    _print_kv("value", str(v))

        # Always enumerate all values for the AMD adapter key; this is where we
        # can discover additional driver caches/overrides beyond PP_Phm*.
        if is_amd:
            dump_all_values(key_path)


def _dump_smu(
    *,
    read_size: int,
    preview_bytes: int,
    max_clock_candidates: int,
    scan_clocks: bool,
) -> None:
    # Import lazily to keep this script usable without driver access.
    try:
        from overclock import create_smu, DRIVER_BUF_OFFSET  # type: ignore
    except Exception as e:
        print()
        print("=" * 80)
        print("SMU")
        print("=" * 80)
        print(f"Could not import overclock.py helpers: {e}")
        return

    print()
    print("=" * 80)
    print("SMU")
    print("=" * 80)
    print("Attempting SMU PPTABLE transfer; run as admin if this fails.")

    try:
        wr0, inpout, mmio, smu, vram_bar = create_smu(verbose=False)
        phys = int(vram_bar) + int(DRIVER_BUF_OFFSET)

        # Map enough window for read_size.
        map_size = (int(read_size) + 0xFFF) & ~0xFFF
        virt, handle = inpout.map_phys(phys, map_size)
        try:
            blob = src.read_smu_pptable_blob(smu, virt, read_size=map_size)
        finally:
            inpout.unmap_phys(virt, handle)

        _describe_blob(
            "SMU table: TABLE_PPTABLE",
            blob,
            preview_bytes=preview_bytes,
            max_clock_candidates=max_clock_candidates,
            scan_clocks=scan_clocks,
        )
    except Exception as e:
        print(f"SMU read failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump PPTable blobs from all sources.")
    ap.add_argument("--rom", default="bios/vbios.rom", help="ROM path (relative or absolute).")
    ap.add_argument("--no-rom", action="store_true", help="Skip ROM read.")
    ap.add_argument("--no-registry", action="store_true", help="Skip registry read.")
    ap.add_argument("--smu", action="store_true", help="Try SMU TABLE_PPTABLE read (admin).")
    ap.add_argument("--smu-size", type=int, default=256 * 1024, help="Bytes to map/read for SMU.")
    ap.add_argument("--preview-bytes", type=int, default=64, help="Hex preview length.")
    ap.add_argument("--no-scan-clocks", action="store_true", help="Skip clock-like triple scan.")
    ap.add_argument("--max-clock-candidates", type=int, default=15, help="Max clock candidates to print.")
    args = ap.parse_args()

    scan_clocks = not args.no_scan_clocks
    max_clock_candidates = int(args.max_clock_candidates)
    preview_bytes = int(args.preview_bytes)

    print("PPTable source dump")
    _print_kv("cwd", os.getcwd())
    _print_kv("rom path", args.rom)

    if not args.no_rom:
        _describe_blob(
            f"ROM file: {args.rom}",
            src.read_rom_blob(args.rom),
            preview_bytes=preview_bytes,
            max_clock_candidates=max_clock_candidates,
            scan_clocks=scan_clocks,
        )

    if not args.no_registry:
        _dump_registry(
            preview_bytes=preview_bytes,
            max_clock_candidates=max_clock_candidates,
            scan_clocks=scan_clocks,
        )

    if args.smu:
        _dump_smu(
            read_size=int(args.smu_size),
            preview_bytes=preview_bytes,
            max_clock_candidates=max_clock_candidates,
            scan_clocks=scan_clocks,
        )
    else:
        print()
        print("(SMU skipped; re-run with --smu to attempt SMU PPTABLE read.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

