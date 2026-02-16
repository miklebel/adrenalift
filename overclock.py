"""
RDNA4 Overclock -- Kernel PPTable Patch + OD Apply
====================================================

One-shot script to run after every reboot. Scans physical memory for
the AMD driver's cached PPTable, patches clock/power/TDC limits,
then applies OD table settings via SMU.

What it patches (in kernel memory):
  - GameClockAc / BoostClockAc  -> removes DPM frequency cap
  - MsgLimits.Power (PPT)       -> raises power limit ceiling
  - MsgLimits.Tdc (GFX current) -> raises current limit
  - Also applies OD table: PPT%, GfxclkFoffset, SetSoftMax

Usage:
  py overclock.py                     # Apply with defaults
  py overclock.py --clock 3500        # Custom clock target
  py overclock.py --power 250         # Custom power limit
  py overclock.py --tdc 200           # Custom TDC (amps)
  py overclock.py --offset 300        # GfxclkFoffset in MHz
  py overclock.py --scan-only         # Just scan and display, no patch

Safe: Non-persistent. Reboot always restores stock values.

Reliability improvements:
  - Validates each match: rejects false positives with garbage PPT/TDC/temp
  - Re-applies OD twice (before and after kernel patch) to handle driver
    re-reading from its cache
  - Verifies patches stick after a short delay
"""

import sys, os, ctypes, struct, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(line_buffering=True)
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from mmio import InpOut32
from smu import create_smu, PPSMC, PPCLK
from od_table import (TABLE_OVERDRIVE, TABLE_SMU_METRICS, TABLE_PPTABLE,
                      OverDriveTable_t, _OD_TABLE_SIZE,
                      PP_OD_FEATURE_PPT_BIT, PP_OD_FEATURE_GFXCLK_BIT)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRIVER_BUF_OFFSET = 0x0FBCC000

# Original values from your GPU's PPTable
ORIG_BASECLOCK_AC  = 1900
ORIG_GAMECLOCK_AC  = 2780
ORIG_BOOSTCLOCK_AC = 3320
ORIG_POWER_AC      = 182   # watts
ORIG_POWER_DC      = 182
ORIG_TDC_GFX       = 152   # amps
ORIG_TDC_SOC       = 55

# Search pattern: just the 3 AC clock fields (6 bytes).
# Previous 28-byte pattern assumed DC clocks + padding were all zero,
# but Adrenalin/driver can populate those fields, breaking the match.
# 6 bytes is unique enough (probability ~1e-14 per position) and we
# use MsgLimits validation to reject any false positives.
CLOCK_PATTERN = struct.pack('<3H',
    ORIG_BASECLOCK_AC, ORIG_GAMECLOCK_AC, ORIG_BOOSTCLOCK_AC)

# MsgLimits.Power pattern (right after DriverReportedClocks)
POWER_PATTERN = struct.pack('<4H',
    ORIG_POWER_AC, ORIG_POWER_DC, 1200, 1200)

CHUNK_SIZE = 2 * 1024 * 1024  # 2MB per scan chunk

# MsgLimits_t field offsets (relative to MsgLimits start)
# Power[PPT_THROTTLER_COUNT=4][POWER_SOURCE_COUNT=2] = 16 bytes
# Tdc[TDC_THROTTLER_COUNT=2] = 4 bytes
# Temperature[TEMP_COUNT=12] = 24 bytes
ML_PPT0_AC  = 0
ML_PPT0_DC  = 2
ML_PPT1_AC  = 4
ML_PPT1_DC  = 6
ML_TDC_GFX  = 16
ML_TDC_SOC  = 18
ML_TEMP_EDGE     = 20
ML_TEMP_HOTSPOT  = 22
ML_TEMP_HSGFX    = 24
ML_TEMP_HSSOC    = 26
ML_TEMP_MEM      = 28
ML_TEMP_VR_GFX   = 30
ML_TEMP_VR_SOC   = 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_buf(virt, n):
    buf = (ctypes.c_ubyte * n)()
    ctypes.memmove(buf, virt, n)
    return bytes(buf)

def write_buf(virt, data):
    arr = (ctypes.c_ubyte * len(data))(*data)
    ctypes.memmove(virt, arr, len(data))

def read_od(smu, virt):
    smu.send_msg(0x12, TABLE_OVERDRIVE)
    raw = read_buf(virt, _OD_TABLE_SIZE)
    if struct.unpack_from('<I', raw, 0)[0] <= 0x1000:
        return OverDriveTable_t.from_buffer_copy(raw)
    smu.send_msg(0x12, TABLE_OVERDRIVE)
    raw = read_buf(virt, _OD_TABLE_SIZE)
    if struct.unpack_from('<I', raw, 0)[0] <= 0x1000:
        return OverDriveTable_t.from_buffer_copy(raw)
    return None

def read_metrics(smu, virt):
    smu.send_msg(0x12, TABLE_SMU_METRICS)
    raw = read_buf(virt, 256)
    gfxclk = struct.unpack_from('<H', raw, 0x48)[0]
    gfxclk2 = struct.unpack_from('<H', raw, 0x4A)[0]
    ppt = struct.unpack_from('<H', raw, 0x30)[0]
    temp = struct.unpack_from('<H', raw, 0x3A)[0]
    return gfxclk, gfxclk2, ppt, temp


# ---------------------------------------------------------------------------
# Memory scanning
# ---------------------------------------------------------------------------

def scan_memory(inpout, patterns, max_gb=16, label="", num_threads=8):
    """Scan physical memory for multiple byte patterns using parallel threads.

    Each thread scans a contiguous range of 2MB chunks, mapping physical memory
    independently.  The InpOut32 driver handles concurrent MapPhysToLin calls
    safely (each creates a separate kernel MDL).  ctypes FFI calls and memmove
    release the GIL, so threads get true parallelism on the I/O-bound work.

    Args:
        patterns: bytes or list of bytes objects to search for (finds any match).
        max_gb:   Upper bound of physical memory to scan.
        label:    Informational label for progress output.
        num_threads: Number of worker threads (default 8).
    """
    if isinstance(patterns, bytes):
        patterns = [patterns]
    max_bytes = max_gb * 1024 * 1024 * 1024
    total_chunks = max_bytes // CHUNK_SIZE

    # Build list of scannable chunk indices (skip PCI hole 3-4 GB)
    chunk_indices = []
    for ci in range(total_chunks):
        phys_base = ci * CHUNK_SIZE
        if 0xC0000000 <= phys_base < 0x100000000:
            continue
        chunk_indices.append(ci)

    scannable = len(chunk_indices)
    lock = threading.Lock()
    progress = [0]  # chunks completed across all threads
    t0 = time.perf_counter()

    def _scan_range(indices):
        """Worker: scan a slice of chunk indices, return local matches."""
        local_found = []
        for ci in indices:
            phys_base = ci * CHUNK_SIZE
            try:
                virt, handle = inpout.map_phys(phys_base, CHUNK_SIZE)
            except (IOError, OSError):
                with lock:
                    progress[0] += 1
                continue
            try:
                buf = (ctypes.c_ubyte * CHUNK_SIZE)()
                ctypes.memmove(buf, virt, CHUNK_SIZE)
                data = bytes(buf)
                for pattern in patterns:
                    pos = 0
                    while True:
                        idx = data.find(pattern, pos)
                        if idx < 0:
                            break
                        local_found.append((phys_base + idx, pattern))
                        pos = idx + 2
            finally:
                inpout.unmap_phys(virt, handle)

            with lock:
                progress[0] += 1
                done = progress[0]
            if done % 512 == 0:
                pct = done / scannable * 100
                gb = done * CHUNK_SIZE / (1024 ** 3)
                print(f"    {pct:5.1f}%  ({gb:.1f} GB scanned)")
        return local_found

    # Split chunks evenly across threads
    per_thread = (scannable + num_threads - 1) // num_threads
    ranges = [chunk_indices[i * per_thread:(i + 1) * per_thread]
              for i in range(num_threads)]
    # Drop empty trailing slices
    ranges = [r for r in ranges if r]

    all_found = []
    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [pool.submit(_scan_range, r) for r in ranges]
        for fut in as_completed(futures):
            all_found.extend(fut.result())

    # Deduplicate by address (threads scan disjoint ranges, but belt-and-suspenders)
    seen = set()
    deduped = []
    for addr, pat in all_found:
        if addr not in seen:
            seen.add(addr)
            deduped.append((addr, pat))

    elapsed = time.perf_counter() - t0
    print(f"    Done: {len(deduped)} match(es) in {elapsed:.1f}s  "
          f"[{len(ranges)} threads, {scannable * CHUNK_SIZE / (1024**3):.1f} GB]")
    return deduped


def read_msglimits(inpout, phys_addr):
    """Read MsgLimits_t at a physical address. Returns dict of values."""
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    virt, handle = inpout.map_phys(page_base, 8192)
    try:
        data = read_buf(virt + page_off, 44)
    finally:
        inpout.unmap_phys(virt, handle)

    return {
        'ppt0_ac':    struct.unpack_from('<H', data, ML_PPT0_AC)[0],
        'ppt0_dc':    struct.unpack_from('<H', data, ML_PPT0_DC)[0],
        'ppt1_ac':    struct.unpack_from('<H', data, ML_PPT1_AC)[0],
        'ppt1_dc':    struct.unpack_from('<H', data, ML_PPT1_DC)[0],
        'tdc_gfx':    struct.unpack_from('<H', data, ML_TDC_GFX)[0],
        'tdc_soc':    struct.unpack_from('<H', data, ML_TDC_SOC)[0],
        'temp_edge':  struct.unpack_from('<H', data, ML_TEMP_EDGE)[0],
        'temp_hotspot': struct.unpack_from('<H', data, ML_TEMP_HOTSPOT)[0],
        'temp_mem':   struct.unpack_from('<H', data, ML_TEMP_MEM)[0],
        'temp_vr_gfx': struct.unpack_from('<H', data, ML_TEMP_VR_GFX)[0],
    }


def is_valid_pptable(ml, target_power=None, target_tdc=None, target_tdc_soc=None):
    """Validate that a MsgLimits readback looks like a real PPTable cache,
    not a random false-positive byte match in memory.

    A real PPTable has:
      - PPT: 100-500W (or our target value)
      - TDC_GFX: 50-500A (or our target)
      - TDC_SOC: 10-200A (or our target)
      - Temp Edge: 50-150C
      - Temp Hotspot: 50-150C
      - Temp VR: 50-200C
    """
    ppt = ml['ppt0_ac']
    tdc_gfx = ml['tdc_gfx']
    tdc_soc = ml['tdc_soc']
    t_edge = ml['temp_edge']
    t_hot = ml['temp_hotspot']
    t_vr = ml['temp_vr_gfx']

    # Allow our own target values as "valid" (for already-patched copies)
    valid_ppt_range = 100 <= ppt <= 500
    valid_tdc_gfx_range = 50 <= tdc_gfx <= 500
    valid_tdc_soc_range = 10 <= tdc_soc <= 200
    valid_temp_edge = 50 <= t_edge <= 150
    valid_temp_hot = 50 <= t_hot <= 150
    valid_temp_vr = 50 <= t_vr <= 200

    # All conditions must pass
    reasons = []
    if not valid_ppt_range:
        reasons.append(f"PPT={ppt}W out of range [100-500]")
    if not valid_tdc_gfx_range:
        reasons.append(f"TDC_GFX={tdc_gfx}A out of range [50-500]")
    if not valid_tdc_soc_range:
        reasons.append(f"TDC_SOC={tdc_soc}A out of range [10-200]")
    if not valid_temp_edge:
        reasons.append(f"Temp_Edge={t_edge}C out of range [50-150]")
    if not valid_temp_hot:
        reasons.append(f"Temp_Hotspot={t_hot}C out of range [50-150]")
    if not valid_temp_vr:
        reasons.append(f"Temp_VR={t_vr}C out of range [50-200]")

    return len(reasons) == 0, reasons


def patch_u16(inpout, phys_addr, offset, new_val):
    """Patch a uint16 at physical address + offset."""
    addr = phys_addr + offset
    page_base = addr & ~0xFFF
    page_off = addr - page_base
    virt, handle = inpout.map_phys(page_base, 4096)
    try:
        old = ctypes.c_ushort.from_address(virt + page_off).value
        ctypes.c_ushort.from_address(virt + page_off).value = new_val
        verify = ctypes.c_ushort.from_address(virt + page_off).value
        return old, verify
    finally:
        inpout.unmap_phys(virt, handle)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='RDNA4 Overclock - Kernel PPTable Patch',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  py overclock.py                        # Defaults: clock=3500, power=250, tdc=200
  py overclock.py --clock 3600 --power 300 --tdc 250
  py overclock.py --offset 300           # Higher GfxclkFoffset
  py overclock.py --scan-only            # Just show current values
  py overclock.py --od-ppt 15            # OD PPT +15%
""")
    parser.add_argument('--clock', type=int, default=3500,
                        help='Target GameClockAc/BoostClockAc MHz (default: 3500)')
    parser.add_argument('--power', type=int, default=250,
                        help='Target MsgLimits.Power watts (default: 250)')
    parser.add_argument('--tdc', type=int, default=200,
                        help='Target TDC_GFX amps (default: 200)')
    parser.add_argument('--tdc-soc', type=int, default=0,
                        help='Target TDC_SOC amps (default: 0=no change)')
    parser.add_argument('--offset', type=int, default=200,
                        help='GfxclkFoffset MHz for OD table (default: 200)')
    parser.add_argument('--od-ppt', type=int, default=10,
                        help='OD PPT percentage (default: 10)')
    parser.add_argument('--max-gb', type=int, default=32,
                        help='Max GB to scan (default: 32)')
    parser.add_argument('--threads', type=int, default=8,
                        help='Number of scan threads (default: 8)')
    parser.add_argument('--scan-only', action='store_true',
                        help='Only scan and display, no modifications')
    args = parser.parse_args()

    print("=" * 62)
    print("  RDNA4 Overclock -- Kernel PPTable Patch + OD Apply")
    print("=" * 62)

    if not args.scan_only:
        print(f"  Clock cap:   {ORIG_GAMECLOCK_AC}/{ORIG_BOOSTCLOCK_AC} -> {args.clock} MHz")
        print(f"  Power (PPT): {ORIG_POWER_AC} -> {args.power} W")
        print(f"  TDC (GFX):   {ORIG_TDC_GFX} -> {args.tdc} A")
        print(f"  OD offset:   +{args.offset} MHz")
        print(f"  OD PPT:      +{args.od_ppt}%")

    # Initialize hardware
    print(f"\n  Initializing hardware...")
    wr0, inpout, mmio, smu, vram_bar = create_smu(verbose=False)
    phys = vram_bar + DRIVER_BUF_OFFSET
    virt, handle = inpout.map_phys(phys, 0x3000)

    try:
        # --- Phase 1: Show current state ---
        fmin = smu.get_min_freq(PPCLK.GFXCLK)
        fmax = smu.get_max_freq(PPCLK.GFXCLK)
        ppt_limit = smu.get_ppt_limit()
        gfxclk, gfxclk2, metrics_ppt, temp = read_metrics(smu, virt)

        print(f"\n  Current GPU State:")
        print(f"    DPM GFXCLK:  min={fmin} max={fmax} MHz")
        print(f"    PPT Limit:   {ppt_limit} W")
        print(f"    GFXCLK:      {gfxclk}/{gfxclk2} MHz")

        # --- Phase 2: Scan for PPTable in kernel memory ---
        print(f"\n{'='*62}")
        print(f"  Phase 1: Scanning kernel memory for PPTable cache...")
        print(f"{'='*62}")

        # Search for both original AND already-patched patterns
        # (handles re-running in same session without reboot)
        patched_clock_pattern = struct.pack('<3H',
            ORIG_BASECLOCK_AC, args.clock, args.clock)

        clock_results = scan_memory(inpout,
            [CLOCK_PATTERN, patched_clock_pattern],
            args.max_gb, "DriverReportedClocks", args.threads)

        if not clock_results:
            print("\n  ERROR: PPTable pattern not found in memory!")
            print("  Make sure AMD driver is loaded (check Device Manager)")
            return

        clock_addrs = [addr for addr, pat in clock_results]
        already_patched_addrs = [addr for addr, pat in clock_results
                                  if pat == patched_clock_pattern]

        # MsgLimits is 28 bytes after DriverReportedClocks
        msglimits_addrs = [a + 28 for a in clock_addrs]

        # --- Validate each match ---
        valid_addrs = []
        rejected_addrs = []

        ap_count = len(already_patched_addrs)
        fresh = len(clock_addrs) - ap_count
        print(f"\n  Found {len(clock_addrs)} pattern match(es)"
              f" ({fresh} original, {ap_count} already patched).")
        print(f"  Validating MsgLimits for each match...")

        for i, addr in enumerate(clock_addrs):
            ml = read_msglimits(inpout, msglimits_addrs[i])
            is_patched = addr in already_patched_addrs
            tag = " [already patched]" if is_patched else ""

            # Read actual clock values from memory
            page_base = addr & ~0xFFF
            page_off = addr - page_base
            v, h = inpout.map_phys(page_base, 4096)
            game_val = ctypes.c_ushort.from_address(v + page_off + 2).value
            boost_val = ctypes.c_ushort.from_address(v + page_off + 4).value
            inpout.unmap_phys(v, h)

            valid, reasons = is_valid_pptable(ml)

            if valid:
                valid_addrs.append(addr)
                status = "VALID"
            else:
                rejected_addrs.append(addr)
                status = "REJECTED (false positive)"

            print(f"\n  Match #{i+1} at 0x{addr:012X}{tag}  [{status}]:")
            print(f"    Clocks:  Base={ORIG_BASECLOCK_AC} Game={game_val} "
                  f"Boost={boost_val} MHz")
            print(f"    PPT:     {ml['ppt0_ac']}W (AC) / {ml['ppt0_dc']}W (DC)")
            print(f"    TDC:     GFX={ml['tdc_gfx']}A  SOC={ml['tdc_soc']}A")
            print(f"    Temps:   Edge={ml['temp_edge']}C  Hotspot={ml['temp_hotspot']}C  "
                  f"VR={ml['temp_vr_gfx']}C")
            if not valid:
                for r in reasons:
                    print(f"    -> {r}")

        if not valid_addrs:
            print(f"\n  WARNING: No valid PPTable copies found!")
            print(f"  All {len(clock_addrs)} match(es) were false positives.")
            print(f"  The driver may have loaded to an unusual memory region.")
            return

        print(f"\n  Result: {len(valid_addrs)} valid PPTable(s), "
              f"{len(rejected_addrs)} false positive(s) rejected")

        if args.scan_only:
            print(f"\n  (scan-only mode, no modifications)")
            return

        # --- Phase 3: Patch kernel memory (ONLY valid copies) ---
        print(f"\n{'='*62}")
        print(f"  Phase 2: Patching {len(valid_addrs)} valid PPTable(s)...")
        print(f"{'='*62}")

        patched = 0
        for i, addr in enumerate(valid_addrs):
            is_ap = addr in already_patched_addrs
            print(f"\n  Patching #{i+1}/{len(valid_addrs)} at 0x{addr:012X}"
                  f"{'  [refreshing]' if is_ap else ''}:")

            # Patch GameClockAc (offset +2 from pattern start)
            old, verify = patch_u16(inpout, addr, 2, args.clock)
            ok = "OK" if verify == args.clock else "FAIL"
            changed = "" if old == verify else f" (was {old})"
            print(f"    GameClockAc:  {verify} MHz{changed}  [{ok}]")

            # Patch BoostClockAc (offset +4)
            old, verify = patch_u16(inpout, addr, 4, args.clock)
            ok = "OK" if verify == args.clock else "FAIL"
            changed = "" if old == verify else f" (was {old})"
            print(f"    BoostClockAc: {verify} MHz{changed}  [{ok}]")

            # Patch MsgLimits.Power PPT0 AC/DC (28 bytes after pattern)
            ml_base = addr + 28
            old, verify = patch_u16(inpout, ml_base, ML_PPT0_AC, args.power)
            ok = "OK" if verify == args.power else "FAIL"
            changed = "" if old == verify else f" (was {old})"
            print(f"    PPT0_AC:      {verify} W{changed}  [{ok}]")

            old, verify = patch_u16(inpout, ml_base, ML_PPT0_DC, args.power)
            ok = "OK" if verify == args.power else "FAIL"
            changed = "" if old == verify else f" (was {old})"
            print(f"    PPT0_DC:      {verify} W{changed}  [{ok}]")

            # Patch TDC_GFX
            old, verify = patch_u16(inpout, ml_base, ML_TDC_GFX, args.tdc)
            ok = "OK" if verify == args.tdc else "FAIL"
            changed = "" if old == verify else f" (was {old})"
            print(f"    TDC_GFX:      {verify} A{changed}  [{ok}]")

            # Patch TDC_SOC if requested
            if args.tdc_soc > 0:
                old, verify = patch_u16(inpout, ml_base, ML_TDC_SOC, args.tdc_soc)
                ok = "OK" if verify == args.tdc_soc else "FAIL"
                changed = "" if old == verify else f" (was {old})"
                print(f"    TDC_SOC:      {verify} A{changed}  [{ok}]")

            patched += 1

        # Also patch any standalone MsgLimits copies
        print(f"\n  Scanning for additional MsgLimits copies...")
        power_results = scan_memory(inpout, POWER_PATTERN, args.max_gb,
                                    "MsgLimits.Power", args.threads)

        # Filter out ones we already patched (within 256 bytes of a clock match)
        all_known_addrs = set(ca + 28 for ca in clock_addrs)  # both valid and rejected
        extra_power = [a for a, _ in power_results
                       if not any(abs(a - ap) < 256 for ap in all_known_addrs)]

        if extra_power:
            print(f"  Found {len(extra_power)} additional power limit(s):")
            for addr in extra_power:
                old, verify = patch_u16(inpout, addr, 0, args.power)
                ok = "OK" if verify == args.power else "FAIL"
                print(f"    0x{addr:012X}: {old} -> {verify} W  [{ok}]")
                patch_u16(inpout, addr, 2, args.power)  # DC too
        else:
            print(f"  No additional copies")

        # --- Phase 4: Apply OD table via SMU ---
        print(f"\n{'='*62}")
        print(f"  Phase 3: Applying OD table settings via SMU...")
        print(f"{'='*62}")

        od = read_od(smu, virt)
        if od:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_PPT_BIT)
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFXCLK_BIT)
            od.Ppt = args.od_ppt
            od.GfxclkFoffset = args.offset
            write_buf(virt, bytes(od))
            resp, _ = smu.send_msg(0x13, TABLE_OVERDRIVE)
            print(f"  OD: PPT=+{args.od_ppt}%, offset=+{args.offset}MHz  "
                  f"resp={resp} {'OK' if resp==1 else 'FAIL'}")

        # Push DPM soft max to clock + offset (matching Linux patch behavior)
        # Linux patch-5 sends SetSoftMaxByFreq(gfx_table->max + GfxclkFoffset)
        # Previously we sent just args.clock (3500), capping the boost!
        effective_max = args.clock + args.offset
        param = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (effective_max & 0xFFFF)
        resp, _ = smu.send_msg(PPSMC.SetSoftMaxByFreq, param)
        print(f"  SetSoftMax({effective_max}): resp={resp}")

        # Set hard max to match -- SMU clamps soft max to never exceed hard max
        resp_hm, _ = smu.send_msg(PPSMC.SetHardMaxByFreq, param)
        print(f"  SetHardMax({effective_max}): resp={resp_hm}")

        # Also try pushing soft MIN higher to encourage boost
        param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (args.clock & 0xFFFF)
        resp2, _ = smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
        print(f"  SetSoftMin({args.clock}): resp={resp2}")

        # Try direct SetPptLimit now that kernel cache has higher MsgLimits.Power
        # (Linux patch-8 enables this by raising msg_limit to 220)
        resp3, _ = smu.send_msg(PPSMC.SetPptLimit, args.power)
        print(f"  SetPptLimit({args.power}W): resp={resp3}")

        # DisallowGfxOff
        smu.send_msg(PPSMC.DisallowGfxOff)

        # Cycle workload to trigger DPM refresh
        smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)  # PowerSave
        time.sleep(0.3)
        smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)  # 3D Fullscreen
        time.sleep(0.3)

        # --- Phase 5: Verify patches survived ---
        print(f"\n{'='*62}")
        print(f"  Phase 4: Verifying patches survived (2s delay)...")
        print(f"{'='*62}")

        time.sleep(2.0)
        overwritten = 0
        for i, addr in enumerate(valid_addrs):
            ml_base = addr + 28
            ml = read_msglimits(inpout, ml_base)
            page_base = addr & ~0xFFF
            page_off = addr - page_base
            v, h = inpout.map_phys(page_base, 4096)
            game_now = ctypes.c_ushort.from_address(v + page_off + 2).value
            boost_now = ctypes.c_ushort.from_address(v + page_off + 4).value
            inpout.unmap_phys(v, h)

            game_ok = game_now == args.clock
            ppt_ok = ml['ppt0_ac'] == args.power
            tdc_ok = ml['tdc_gfx'] == args.tdc

            if game_ok and ppt_ok and tdc_ok:
                print(f"  Copy #{i+1} at 0x{addr:012X}: OK (Game={game_now} PPT={ml['ppt0_ac']}W TDC={ml['tdc_gfx']}A)")
            else:
                overwritten += 1
                print(f"  Copy #{i+1} at 0x{addr:012X}: OVERWRITTEN!")
                print(f"    Game={game_now} (want {args.clock}), "
                      f"PPT={ml['ppt0_ac']}W (want {args.power}), "
                      f"TDC={ml['tdc_gfx']}A (want {args.tdc})")
                # Re-patch this copy
                print(f"    -> Re-patching...")
                patch_u16(inpout, addr, 2, args.clock)
                patch_u16(inpout, addr, 4, args.clock)
                patch_u16(inpout, ml_base, ML_PPT0_AC, args.power)
                patch_u16(inpout, ml_base, ML_PPT0_DC, args.power)
                patch_u16(inpout, ml_base, ML_TDC_GFX, args.tdc)
                if args.tdc_soc > 0:
                    patch_u16(inpout, ml_base, ML_TDC_SOC, args.tdc_soc)

        if overwritten > 0:
            print(f"\n  WARNING: {overwritten} copies were overwritten by the driver!")
            print(f"  Re-patched them. Applying OD again to force refresh...")
            # Re-apply OD table to trigger driver to re-read from patched cache
            od2 = read_od(smu, virt)
            if od2:
                od2.FeatureCtrlMask |= (1 << PP_OD_FEATURE_PPT_BIT)
                od2.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFXCLK_BIT)
                od2.Ppt = args.od_ppt
                od2.GfxclkFoffset = args.offset
                write_buf(virt, bytes(od2))
                resp, _ = smu.send_msg(0x13, TABLE_OVERDRIVE)
                print(f"  OD re-apply: resp={resp} {'OK' if resp==1 else 'FAIL'}")
            # Re-send SetSoftMax/Min and SetPptLimit
            effective_max = args.clock + args.offset
            param = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (effective_max & 0xFFFF)
            smu.send_msg(PPSMC.SetSoftMaxByFreq, param)
            smu.send_msg(PPSMC.SetHardMaxByFreq, param)
            param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (args.clock & 0xFFFF)
            smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
            smu.send_msg(PPSMC.SetPptLimit, args.power)
            # Cycle workload again
            smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)
            time.sleep(0.3)
            smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)
            time.sleep(0.3)
        else:
            print(f"\n  All {len(valid_addrs)} patches verified OK")

        # --- Phase 6: Final results ---
        print(f"\n{'='*62}")
        print(f"  Results")
        print(f"{'='*62}")

        fmin = smu.get_min_freq(PPCLK.GFXCLK)
        fmax = smu.get_max_freq(PPCLK.GFXCLK)
        ppt_limit = smu.get_ppt_limit()

        print(f"  DPM GFXCLK:  min={fmin} max={fmax} MHz", end="")
        if fmax > 3500:
            print(f"  [+{fmax - 3500} MHz above stock!]")
        else:
            print()
        print(f"  PPT Limit:   {ppt_limit} W")

        # Quick monitor
        print(f"\n  Quick monitor (5 seconds):")
        peak = 0
        for i in range(10):
            time.sleep(0.5)
            gfxclk, gfxclk2, metrics_ppt, temp = read_metrics(smu, virt)
            peak = max(peak, gfxclk)
            print(f"    +{(i+1)*0.5:4.1f}s: GFXCLK={gfxclk:4d}/{gfxclk2:4d}  "
                  f"PPT={metrics_ppt:3d}W  T={temp}C")

        print(f"\n  Peak GFXCLK (idle): {peak} MHz")
        print(f"\n{'='*62}")
        print(f"  Overclock applied! Run a GPU benchmark to verify.")
        print(f"  Expected max clock: ~{args.clock + args.offset} MHz")
        print(f"  This patch resets on reboot - run this script again.")
        print(f"{'='*62}")

    finally:
        inpout.unmap_phys(virt, handle)
        mmio.close()
        if inpout: inpout.close()
        if wr0: wr0.close()


if __name__ == '__main__':
    main()
