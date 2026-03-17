"""
find_driver_buffer.py -- Scan VRAM to locate the Windows driver's DMA buffer.
=============================================================================

The SMU firmware locks the Driver DRAM address at boot (SetDriverDramAddr is
one-shot).  This tool finds where the Windows driver's buffer actually lives
by triggering a TransferTableSmu2Dram and then scanning VRAM for the metrics
pattern.

Works even when the buffer is far beyond the PCIe BAR window (e.g. 252 MB on
a 64 MB BAR) because InpOut32's MmMapIoSpace can map any physical address.

Usage:
    python -m src.tools.find_driver_buffer
    python -m src.tools.find_driver_buffer --scan-range 1024   # scan first 1 GB
"""

import argparse
import ctypes
import struct
import sys
import time

from src.engine.smu import create_smu, PPSMC
from src.engine.smu_metrics import (
    SmuMetrics_t, SMU_METRICS_SIZE, TABLE_SMU_METRICS, parse_metrics,
)


def _read_mapped(virt, size):
    buf = (ctypes.c_ubyte * size)()
    ctypes.memmove(buf, virt, size)
    return bytes(buf)


def scan_for_metrics(inpout, smu, vram_bar, scan_range_mb, verbose=True):
    """Scan VRAM for pages that look like a live SmuMetrics_t buffer.

    Sends two TransferTableSmu2Dram commands separated by a delay,
    then scans physical memory [vram_bar, vram_bar + scan_range] for
    4KB-aligned pages where MetricsCounter changed between the two passes.

    Returns a list of (offset, gfxclk, uclk, metrics_counter, avg_power).
    """
    scan_bytes = scan_range_mb * 1024 * 1024
    CHUNK = 4 * 1024 * 1024  # 4 MB per mapping
    PAGE = 0x1000

    OFF_MC = SmuMetrics_t.MetricsCounter.offset
    OFF_PWR = SmuMetrics_t.AverageSocketPower.offset
    OFF_GFXCLK = 0
    OFF_UCLK = 8

    print(f"[1/4] Triggering first metrics transfer...")
    smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_SMU_METRICS)
    smu.hdp_flush()
    time.sleep(0.2)

    print(f"[2/4] Scanning {scan_range_mb} MB of VRAM (pass 1)...")
    snap1 = {}
    for chunk_base in range(0, scan_bytes, CHUNK):
        chunk_sz = min(CHUNK, scan_bytes - chunk_base)
        phys = vram_bar + chunk_base
        try:
            v, h = inpout.map_phys(phys, chunk_sz)
            data = _read_mapped(v, chunk_sz)
            inpout.unmap_phys(v, h)
        except Exception:
            continue

        for pg in range(0, chunk_sz, PAGE):
            page = data[pg:pg + PAGE]
            if len(page) < OFF_PWR + 2:
                continue
            mc = struct.unpack_from('<I', page, OFF_MC)[0]
            pwr = struct.unpack_from('<H', page, OFF_PWR)[0]
            if mc == 0 or mc >= 0x80000000 or pwr == 0 or pwr > 600:
                continue
            gfx = struct.unpack_from('<I', page, OFF_GFXCLK)[0]
            uclk = struct.unpack_from('<I', page, OFF_UCLK)[0]
            if gfx == 0 or gfx > 5000 or uclk > 5000:
                continue
            off = chunk_base + pg
            snap1[off] = (gfx, uclk, mc, pwr)

        done_mb = (chunk_base + chunk_sz) / (1024 * 1024)
        if verbose and int(done_mb) % 64 == 0 and done_mb > 0:
            print(f"       ...{int(done_mb)} MB scanned, "
                  f"{len(snap1)} candidate(s) so far")

    print(f"[2/4] Pass 1 done: {len(snap1)} candidate page(s)")
    if not snap1:
        return []

    print(f"[3/4] Triggering second metrics transfer (for MetricsCounter delta)...")
    time.sleep(0.5)
    smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_SMU_METRICS)
    smu.hdp_flush()
    time.sleep(0.2)

    print(f"[4/4] Re-reading {len(snap1)} candidate(s) for MetricsCounter change...")
    results = []
    for off, (gfx1, uclk1, mc1, pwr1) in sorted(snap1.items()):
        phys = vram_bar + off
        try:
            v, h = inpout.map_phys(phys, PAGE)
            page2 = _read_mapped(v, PAGE)
            inpout.unmap_phys(v, h)
        except Exception:
            continue

        mc2 = struct.unpack_from('<I', page2, OFF_MC)[0]
        gfx2 = struct.unpack_from('<I', page2, OFF_GFXCLK)[0]
        uclk2 = struct.unpack_from('<I', page2, OFF_UCLK)[0]
        pwr2 = struct.unpack_from('<H', page2, OFF_PWR)[0]

        delta = mc2 - mc1 if mc2 >= mc1 else 0
        live = delta > 0 and mc2 < 0x80000000
        tag = " *** LIVE ***" if live else ""

        if verbose or live:
            print(f"  offset=0x{off:08X} ({off / (1024*1024):.1f} MB) "
                  f"MC={mc1}->{mc2} (delta={delta}) "
                  f"GFX={gfx2} UCLK={uclk2} PWR={pwr2}{tag}")

        results.append({
            'offset': off,
            'mc1': mc1, 'mc2': mc2, 'delta': delta,
            'gfx': gfx2, 'uclk': uclk2, 'pwr': pwr2,
            'live': live,
        })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Find the Windows driver's DMA buffer in VRAM")
    parser.add_argument('--scan-range', type=int, default=512,
                        help='VRAM range to scan in MB (default: 512)')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    print("=== Driver DMA Buffer Finder ===")
    print()

    wr0, inpout, mmio, smu, vram_bar = create_smu(verbose=False)
    print(f"GPU VRAM BAR: 0x{vram_bar:X}")
    print()

    try:
        results = scan_for_metrics(
            inpout, smu, vram_bar, args.scan_range,
            verbose=not args.quiet,
        )

        print()
        live = [r for r in results if r['live']]
        if live:
            print(f"Found {len(live)} LIVE buffer(s):")
            for r in live:
                print(f"  OFFSET = 0x{r['offset']:08X}  "
                      f"({r['offset'] / (1024*1024):.1f} MB)  "
                      f"GFX={r['gfx']} UCLK={r['uclk']} PWR={r['pwr']}W "
                      f"MC={r['mc2']}")
            print()
            best = live[0]
            print(f"Recommended DRIVER_BUF_OFFSET = 0x{best['offset']:08X}")
            print(f"Set this in overclock_engine.py as DRIVER_BUF_OFFSET_DEFAULT")
        else:
            print("No LIVE buffer found in the scanned range.")
            print(f"Try increasing --scan-range (current: {args.scan_range} MB)")
            if results:
                print(f"\nStatic candidates (MetricsCounter didn't change):")
                for r in results[:10]:
                    print(f"  offset=0x{r['offset']:08X} MC={r['mc1']} "
                          f"GFX={r['gfx']} PWR={r['pwr']}")
    finally:
        mmio.close()
        if inpout:
            inpout.close()
        if wr0:
            wr0.close()


if __name__ == '__main__':
    main()
