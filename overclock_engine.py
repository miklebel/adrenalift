"""
overclock_engine.py -- Core Overclock Engine for RDNA4 GPUs
============================================================

Provides callable functions for GUI and CLI integration:

  init_hardware()            -> hardware handle dict
  scan_for_pptable()         -> ScanResult with validated addresses
  patch_pptable()            -> list of per-copy patch reports
  apply_od_settings()        -> dict of SMU command results
  verify_patches()           -> (all_ok, overwritten_count, details)
  cleanup_hardware()         -> release hardware handles

  get_gpu_state()            -> current GPU metrics/frequencies
  get_dpm_ranges()           -> DPM frequency ranges for each clock
  watchdog_step()            -> single watchdog iteration

The scan function accepts a progress_callback(pct, msg) for GUI
progress bars. All functions return structured data instead of
printing directly, so callers control presentation.

Safe: Non-persistent.  Reboot always restores stock values.
"""

import sys, os, ctypes, struct, time, threading, json
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from mmio import InpOut32
from smu import create_smu, PPSMC, PPCLK, SMU_FEATURE
from od_table import (TABLE_OVERDRIVE, TABLE_SMU_METRICS, TABLE_PPTABLE,
                      OverDriveTable_t, _OD_TABLE_SIZE,
                      PP_OD_FEATURE_PPT_BIT, PP_OD_FEATURE_GFXCLK_BIT,
                      PP_OD_FEATURE_TDC_BIT, PP_OD_FEATURE_UCLK_BIT,
                      PP_OD_FEATURE_FCLK_BIT)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRIVER_BUF_OFFSET = 0x0FBCC000

ORIG_BASECLOCK_AC  = 1900
ORIG_GAMECLOCK_AC  = 2780
ORIG_BOOSTCLOCK_AC = 3320
ORIG_POWER_AC      = 182   # watts
ORIG_POWER_DC      = 182
ORIG_TDC_GFX       = 152   # amps
ORIG_TDC_SOC       = 55

CLOCK_PATTERN = struct.pack('<3H',
    ORIG_BASECLOCK_AC, ORIG_GAMECLOCK_AC, ORIG_BOOSTCLOCK_AC)

POWER_PATTERN = struct.pack('<4H',
    ORIG_POWER_AC, ORIG_POWER_DC, 1200, 1200)

CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB per scan chunk
CACHE_FILE = os.path.join(_script_dir, ".pptable_phys_cache.json")
CACHE_MAX_ENTRIES = 32

# MsgLimits_t field offsets (relative to MsgLimits start)
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
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class OverclockSettings:
    """All tunable overclock parameters."""
    clock: int = 3500       # Target GameClockAc/BoostClockAc MHz (simple mode)
    power: int = 250        # Target MsgLimits.Power watts
    tdc: int = 200          # Target TDC_GFX amps
    tdc_soc: int = 0        # Target TDC_SOC amps (0 = no change)
    offset: int = 200       # GfxclkFoffset MHz for OD table
    od_ppt: int = 10        # OD PPT percentage
    od_tdc: int = 0         # OD TDC percentage (0 = no change)
    min_clock: int = 0      # Minimum GFX clock floor (0 = use clock)
    lock_features: bool = False  # Disable DS_GFXCLK / GFX_ULV / GFXOFF

    # Detailed mode: explicit per-field values (0 = use clock/power/tdc)
    game_clock: int = 0
    boost_clock: int = 0
    power_ac: int = 0
    power_dc: int = 0
    tdc_gfx: int = 0
    temp_edge: int = 0
    temp_hotspot: int = 0
    temp_mem: int = 0
    temp_vr_gfx: int = 0
    temp_vr_soc: int = 0
    uclk_min: int = 0
    uclk_max: int = 0
    fclk_min: int = 0
    fclk_max: int = 0

    def _game_clock(self):
        return self.game_clock if self.game_clock else self.clock

    def _boost_clock(self):
        return self.boost_clock if self.boost_clock else self.clock

    def _power_ac(self):
        return self.power_ac if self.power_ac else self.power

    def _power_dc(self):
        return self.power_dc if self.power_dc else self.power

    def _tdc_gfx(self):
        return self.tdc_gfx if self.tdc_gfx else self.tdc


    @property
    def effective_min_clock(self):
        base = self._game_clock()
        return self.min_clock if self.min_clock > 0 else base

    @property
    def effective_lock_features(self):
        return self.lock_features or self.min_clock > 0

    @property
    def effective_max(self):
        return self._boost_clock() + self.offset


@dataclass
class ScanOptions:
    """Controls for the memory scanning strategy."""
    max_gb: int = 32
    num_threads: int = 0
    fast_window_mb: int = 512
    cache_max_addrs: int = 16
    no_cache_fastpath: bool = False


@dataclass
class ScanResult:
    """Result of scan_for_pptable()."""
    valid_addrs: list            # validated PPTable physical addresses
    already_patched_addrs: list  # subset that matched the patched pattern
    rejected_addrs: list         # false positives
    all_clock_addrs: list        # all found addresses before validation
    did_full_scan: bool
    match_details: list          # per-match info dicts for display
    error: str = ""              # non-empty if scan failed
    od_table: object = None  # OverDriveTable_t from read_od(), if available


@dataclass
class ODScanResult:
    """Result of scan_for_od_table()."""
    valid_addrs: list            # validated OD table physical addresses
    valid_tables: list           # OverDriveTable_t per valid addr (same order)
    all_matches: list            # all pattern matches before validation
    rejected_addrs: list
    did_full_scan: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _noop_cb(pct, msg):
    pass


def _map_progress(cb, lo, hi):
    """Return a callback that maps inner 0-100 % to the [lo, hi] range."""
    if cb is None:
        return None
    def mapped(pct, msg):
        cb(lo + pct * (hi - lo) / 100.0, msg)
    return mapped


def _resolve_scan_threads(num_threads):
    if num_threads and num_threads > 0:
        return num_threads
    cpu = os.cpu_count() or 8
    return max(8, min(32, cpu * 2))


# ---------------------------------------------------------------------------
# Low-level memory helpers
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


def extract_od_pattern(smu, virt, pattern_len=24):
    """Read OD table from SMU and return byte pattern for RAM search.

    Uses first N bytes of OverDriveTable_t (FeatureCtrlMask + voltage fields).
    These tend to be more distinctive than all-zero default tables.

    Args:
        smu: SmuCmd instance
        virt: Virtual address of DMA buffer (from init_hardware)
        pattern_len: Bytes to use (default 24: mask + 6*int16 + 2*uint16)

    Returns:
        bytes: Pattern for scan, or empty bytes if read fails.
    """
    od = read_od(smu, virt)
    if od is None:
        return b""
    raw = bytes(od)
    return raw[:min(pattern_len, len(raw))]


def read_metrics(smu, virt):
    smu.send_msg(0x12, TABLE_SMU_METRICS)
    raw = read_buf(virt, 256)
    gfxclk = struct.unpack_from('<H', raw, 0x48)[0]
    gfxclk2 = struct.unpack_from('<H', raw, 0x4A)[0]
    ppt = struct.unpack_from('<H', raw, 0x30)[0]
    temp = struct.unpack_from('<H', raw, 0x3A)[0]
    return gfxclk, gfxclk2, ppt, temp


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
        'ppt0_ac':      struct.unpack_from('<H', data, ML_PPT0_AC)[0],
        'ppt0_dc':      struct.unpack_from('<H', data, ML_PPT0_DC)[0],
        'ppt1_ac':      struct.unpack_from('<H', data, ML_PPT1_AC)[0],
        'ppt1_dc':      struct.unpack_from('<H', data, ML_PPT1_DC)[0],
        'tdc_gfx':      struct.unpack_from('<H', data, ML_TDC_GFX)[0],
        'tdc_soc':      struct.unpack_from('<H', data, ML_TDC_SOC)[0],
        'temp_edge':    struct.unpack_from('<H', data, ML_TEMP_EDGE)[0],
        'temp_hotspot': struct.unpack_from('<H', data, ML_TEMP_HOTSPOT)[0],
        'temp_mem':     struct.unpack_from('<H', data, ML_TEMP_MEM)[0],
        'temp_vr_gfx':  struct.unpack_from('<H', data, ML_TEMP_VR_GFX)[0],
    }


def is_valid_pptable(ml, target_power=None, target_tdc=None, target_tdc_soc=None):
    """Validate that a MsgLimits readback looks like a real PPTable cache."""
    ppt = ml['ppt0_ac']
    tdc_gfx = ml['tdc_gfx']
    tdc_soc = ml['tdc_soc']
    t_edge = ml['temp_edge']
    t_hot = ml['temp_hotspot']
    t_vr = ml['temp_vr_gfx']

    reasons = []
    if not (100 <= ppt <= 500):
        reasons.append(f"PPT={ppt}W out of range [100-500]")
    if not (50 <= tdc_gfx <= 500):
        reasons.append(f"TDC_GFX={tdc_gfx}A out of range [50-500]")
    if not (10 <= tdc_soc <= 200):
        reasons.append(f"TDC_SOC={tdc_soc}A out of range [10-200]")
    if not (50 <= t_edge <= 150):
        reasons.append(f"Temp_Edge={t_edge}C out of range [50-150]")
    if not (50 <= t_hot <= 150):
        reasons.append(f"Temp_Hotspot={t_hot}C out of range [50-150]")
    if not (50 <= t_vr <= 200):
        reasons.append(f"Temp_VR={t_vr}C out of range [50-200]")
    return len(reasons) == 0, reasons


def validate_od_candidate(inpout, phys_addr):
    """Parse phys_addr as OverDriveTable_t and validate sanity.

    Returns OverDriveTable_t if valid, None if invalid or unreadable.
    """
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    if page_off + _OD_TABLE_SIZE > 4096:
        map_size = 8192
    else:
        map_size = 4096
    try:
        virt, handle = inpout.map_phys(page_base, map_size)
    except (IOError, OSError):
        return None
    try:
        raw = read_buf(virt + page_off, _OD_TABLE_SIZE)
    finally:
        inpout.unmap_phys(virt, handle)

    if len(raw) < _OD_TABLE_SIZE:
        return None

    try:
        od = OverDriveTable_t.from_buffer_copy(raw)
    except Exception:
        return None

    # Sanity checks per plan
    if od.FeatureCtrlMask > 0x3FFF:
        return None
    if not (0 <= od.VddGfxVmax <= 2000):
        return None
    if not (0 <= od.VddSocVmax <= 2000):
        return None
    if not (-500 <= od.GfxclkFoffset <= 2000):
        return None
    if od.UclkFmin > od.UclkFmax:
        return None
    if not (0 <= od.UclkFmin <= 3000) or not (0 <= od.UclkFmax <= 3000):
        return None
    if not (-50 <= od.Ppt <= 100) or not (-50 <= od.Tdc <= 100):
        return None

    return od


def patch_u16(inpout, phys_addr, offset, new_val):
    """Patch a uint16 at physical address + offset. Returns (old, verified)."""
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
# Address cache
# ---------------------------------------------------------------------------

def _parse_cache_addr(v):
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    raise ValueError("Unsupported address type")


def load_cached_addrs(max_entries=CACHE_MAX_ENTRIES):
    """Load cached PPTable addresses, ranked by hit-count + recency."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        entries = data.get("entries", None)
        ranked = []

        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                try:
                    addr = _parse_cache_addr(e.get("addr"))
                except Exception:
                    continue
                hits = int(e.get("hits", 1))
                last_seen = int(e.get("last_seen_unix", 0))
                ranked.append((addr, max(1, hits), max(0, last_seen)))
        else:
            addrs = data.get("valid_addrs", [])
            if isinstance(addrs, list):
                for a in addrs:
                    try:
                        ranked.append((_parse_cache_addr(a), 1, 0))
                    except Exception:
                        continue

        merged = {}
        for addr, hits, last_seen in ranked:
            prev = merged.get(addr)
            if prev is None or (hits, last_seen) > prev:
                merged[addr] = (hits, last_seen)

        ordered = sorted(
            merged.items(),
            key=lambda kv: (-kv[1][0], -kv[1][1], kv[0])
        )
        return [addr for addr, _meta in ordered[:max_entries]]
    except Exception:
        return []


def save_cached_addrs(addrs, max_entries=CACHE_MAX_ENTRIES):
    """Persist cached PPTable addresses with frequency + recency ranking."""
    now = int(time.time())
    try:
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}

        state = {}
        old_entries = old.get("entries", None)
        if isinstance(old_entries, list):
            for e in old_entries:
                if not isinstance(e, dict):
                    continue
                try:
                    addr = _parse_cache_addr(e.get("addr"))
                except Exception:
                    continue
                hits = int(e.get("hits", 1))
                last_seen = int(e.get("last_seen_unix", 0))
                state[addr] = [max(1, hits), max(0, last_seen)]
        else:
            for a in (old.get("valid_addrs", [])
                      if isinstance(old.get("valid_addrs", []), list) else []):
                try:
                    addr = _parse_cache_addr(a)
                except Exception:
                    continue
                state.setdefault(addr, [1, 0])

        for addr in sorted(set(addrs)):
            if addr in state:
                state[addr][0] += 1
                state[addr][1] = now
            else:
                state[addr] = [1, now]

        ranked = sorted(
            state.items(),
            key=lambda kv: (-kv[1][0], -kv[1][1], kv[0])
        )[:max_entries]

        payload = {
            "schema": 2,
            "saved_at_unix": now,
            "entries": [
                {
                    "addr": f"0x{addr:012X}",
                    "hits": meta[0],
                    "last_seen_unix": meta[1],
                }
                for addr, meta in ranked
            ],
            "valid_addrs": [f"0x{addr:012X}" for addr, _meta in ranked],
        }

        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Memory scanning primitives
# ---------------------------------------------------------------------------

def scan_memory(inpout, patterns, max_gb=16, num_threads=0,
                progress_callback=None):
    """Scan physical memory for byte patterns using parallel threads.

    Returns list of (phys_addr, matched_pattern) tuples.
    progress_callback(pct, msg) is called periodically during the scan.
    """
    if isinstance(patterns, bytes):
        patterns = [patterns]
    cb = progress_callback or _noop_cb
    max_bytes = max_gb * 1024 * 1024 * 1024
    total_chunks = max_bytes // CHUNK_SIZE

    chunk_indices = []
    for ci in range(total_chunks):
        phys_base = ci * CHUNK_SIZE
        if 0xC0000000 <= phys_base < 0x100000000:
            continue
        chunk_indices.append(ci)

    scannable = len(chunk_indices)
    lock = threading.Lock()
    progress = [0]
    t0 = time.perf_counter()

    def _scan_range(indices):
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
            if done % 64 == 0 or done == scannable:
                pct = done / scannable * 100
                gb = done * CHUNK_SIZE / (1024 ** 3)
                cb(pct, f"{pct:.1f}% ({gb:.1f} GB scanned)")
        return local_found

    num_threads = _resolve_scan_threads(num_threads)
    per_thread = (scannable + num_threads - 1) // num_threads
    ranges = [chunk_indices[i * per_thread:(i + 1) * per_thread]
              for i in range(num_threads)]
    ranges = [r for r in ranges if r]

    all_found = []
    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [pool.submit(_scan_range, r) for r in ranges]
        for fut in as_completed(futures):
            all_found.extend(fut.result())

    seen = set()
    deduped = []
    for addr, pat in all_found:
        if addr not in seen:
            seen.add(addr)
            deduped.append((addr, pat))

    elapsed = time.perf_counter() - t0
    total_gb = scannable * CHUNK_SIZE / (1024 ** 3)
    cb(100, f"Done: {len(deduped)} match(es) in {elapsed:.1f}s "
       f"[{len(ranges)} threads, {total_gb:.1f} GB]")
    return deduped


def scan_memory_windows(inpout, patterns, centers, window_mb=512,
                        max_centers=None, num_threads=0,
                        progress_callback=None):
    """Scan small windows around candidate physical addresses."""
    if isinstance(patterns, bytes):
        patterns = [patterns]
    cb = progress_callback or _noop_cb
    if not centers:
        return []
    if max_centers is not None and max_centers > 0:
        centers = centers[:max_centers]

    half = (window_mb * 1024 * 1024) // 2
    chunks = set()
    for c in centers:
        start = max(0, c - half)
        end = c + half
        phys = (start // CHUNK_SIZE) * CHUNK_SIZE
        while phys < end:
            if not (0xC0000000 <= phys < 0x100000000):
                chunks.add(phys)
            phys += CHUNK_SIZE

    if not chunks:
        return []

    chunk_list = sorted(chunks)
    num_threads = _resolve_scan_threads(num_threads)
    lock = threading.Lock()
    progress = [0]
    total = len(chunk_list)
    t0 = time.perf_counter()

    def _scan_range(phys_ranges):
        local = []
        for phys_base in phys_ranges:
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
                        local.append((phys_base + idx, pattern))
                        pos = idx + 2
            finally:
                inpout.unmap_phys(virt, handle)

            with lock:
                progress[0] += 1
                done = progress[0]
            if done % 64 == 0 or done == total:
                pct = done / total * 100
                gb = done * CHUNK_SIZE / (1024 ** 3)
                cb(pct, f"Window scan: {gb:.1f} GB")
        return local

    per_thread = (total + num_threads - 1) // num_threads
    ranges = [chunk_list[i * per_thread:(i + 1) * per_thread]
              for i in range(num_threads)]
    ranges = [r for r in ranges if r]

    found = []
    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [pool.submit(_scan_range, r) for r in ranges]
        for fut in as_completed(futures):
            found.extend(fut.result())

    seen = set()
    deduped = []
    for addr, pat in found:
        if addr not in seen:
            seen.add(addr)
            deduped.append((addr, pat))

    elapsed = time.perf_counter() - t0
    total_gb = total * CHUNK_SIZE / (1024 ** 3)
    cb(100, f"Window scan done: {len(deduped)} match(es) in {elapsed:.2f}s "
       f"[{total_gb:.2f} GB, {len(ranges)} threads]")
    return deduped


def probe_cached_addrs(inpout, patterns, cached_addrs):
    """Check exact cached addresses (near-instant). Returns list of (addr, pat)."""
    if isinstance(patterns, bytes):
        patterns = [patterns]
    if not cached_addrs:
        return []

    max_pat = max(len(p) for p in patterns)
    hits = []
    for addr in cached_addrs:
        page_base = addr & ~0xFFF
        page_off = addr - page_base
        map_size = 8192 if page_off + max_pat <= 8192 else 12288
        try:
            virt, handle = inpout.map_phys(page_base, map_size)
        except (IOError, OSError):
            continue
        try:
            raw = read_buf(virt + page_off, max_pat)
            for p in patterns:
                if raw[:len(p)] == p:
                    hits.append((addr, p))
                    break
        finally:
            inpout.unmap_phys(virt, handle)
    return hits


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def init_hardware():
    """Initialize hardware drivers and map the driver DMA buffer.

    Returns a dict with keys:
        wr0, inpout, mmio, smu, vram_bar, virt, handle, phys
    Caller must call cleanup_hardware() when done.
    """
    wr0, inpout, mmio, smu, vram_bar = create_smu(verbose=False)
    phys = vram_bar + DRIVER_BUF_OFFSET
    virt, handle = inpout.map_phys(phys, 0x3000)
    return {
        'wr0': wr0, 'inpout': inpout, 'mmio': mmio, 'smu': smu,
        'vram_bar': vram_bar, 'virt': virt, 'handle': handle, 'phys': phys,
    }


def cleanup_hardware(hw):
    """Release all hardware handles opened by init_hardware()."""
    try:
        if hw.get('virt') and hw.get('handle') and hw.get('inpout'):
            hw['inpout'].unmap_phys(hw['virt'], hw['handle'])
    except Exception:
        pass
    try:
        if hw.get('mmio'):
            hw['mmio'].close()
    except Exception:
        pass
    try:
        if hw.get('inpout'):
            hw['inpout'].close()
    except Exception:
        pass
    try:
        if hw.get('wr0'):
            hw['wr0'].close()
    except Exception:
        pass


def get_gpu_state(smu, virt):
    """Query current GPU state. Returns dict with metrics and DPM info."""
    fmin = smu.get_min_freq(PPCLK.GFXCLK)
    fmax = smu.get_max_freq(PPCLK.GFXCLK)
    ppt_limit = smu.get_ppt_limit()
    gfxclk, gfxclk2, metrics_ppt, temp = read_metrics(smu, virt)
    return {
        'fmin': fmin, 'fmax': fmax, 'ppt_limit': ppt_limit,
        'gfxclk': gfxclk, 'gfxclk2': gfxclk2,
        'metrics_ppt': metrics_ppt, 'temp': temp,
    }


def get_dpm_ranges(smu):
    """Query DPM frequency ranges for all clock domains.

    Returns list of dicts: [{'name': 'GFXCLK', 'min': N, 'max': N}, ...]
    Failed queries have 'error' key instead of min/max.
    """
    results = []
    for clk_id, clk_name in [(PPCLK.GFXCLK, "GFXCLK"),
                              (PPCLK.SOCCLK, "SOCCLK"),
                              (PPCLK.UCLK,   "UCLK"),
                              (PPCLK.FCLK,   "FCLK")]:
        try:
            fmin = smu.get_min_freq(clk_id)
            fmax = smu.get_max_freq(clk_id)
            results.append({'name': clk_name, 'min': fmin, 'max': fmax})
        except Exception as e:
            results.append({'name': clk_name, 'error': str(e)})
    return results


def scan_for_pptable(inpout, settings, scan_opts=None, progress_callback=None,
                     vbios_values=None):
    """Scan physical memory for the driver's cached PPTable.

    Uses a tiered strategy: exact cache probe -> window scan -> full scan.
    Validates each match against MsgLimits sanity checks.

    Args:
        inpout:            InpOut32 driver instance
        settings:          OverclockSettings (needs .clock for pattern)
        scan_opts:         ScanOptions (defaults used if None)
        progress_callback: fn(pct: float, msg: str) for progress updates
        vbios_values:      VbiosValues from parsed VBIOS (clock/power patterns).
                           When None, uses hardcoded 9060 XT values.

    Returns:
        ScanResult with validated addresses and per-match details.
    """
    if scan_opts is None:
        scan_opts = ScanOptions()
    cb = progress_callback or _noop_cb

    if vbios_values is not None:
        clock_pattern = vbios_values.clock_pattern()
        base_mhz = vbios_values.baseclock_ac
    else:
        clock_pattern = CLOCK_PATTERN
        base_mhz = ORIG_BASECLOCK_AC

    patched_clock_pattern = struct.pack('<3H',
        base_mhz, settings._game_clock(), settings._boost_clock())
    search_patterns = [clock_pattern, patched_clock_pattern]

    clock_results = []
    did_full_scan = False

    # --- Fast path: cache probe + window scan ---
    if not scan_opts.no_cache_fastpath:
        cached_addrs = load_cached_addrs(
            max_entries=max(1, scan_opts.cache_max_addrs))
        if cached_addrs:
            cb(1, f"Probing {len(cached_addrs)} cached address(es)...")
            exact_hits = probe_cached_addrs(inpout, search_patterns,
                                            cached_addrs)
            if exact_hits:
                cb(3, f"Exact cached hits: {len(exact_hits)}")
                clock_results.extend(exact_hits)

            stages = sorted(set([
                max(64, scan_opts.fast_window_mb // 4),
                max(128, scan_opts.fast_window_mb // 2),
                max(256, scan_opts.fast_window_mb),
            ]))
            for si, wmb in enumerate(stages):
                max_centers = (min(max(1, scan_opts.cache_max_addrs), 4)
                               if clock_results
                               else scan_opts.cache_max_addrs)
                pct_lo = 5 + (si / len(stages)) * 20
                pct_hi = 5 + ((si + 1) / len(stages)) * 20
                cb(pct_lo,
                   f"Window scan: {wmb} MB around top "
                   f"{max_centers} cached address(es)...")
                window_hits = scan_memory_windows(
                    inpout, search_patterns, cached_addrs, wmb,
                    max_centers=max_centers,
                    num_threads=scan_opts.num_threads,
                    progress_callback=_map_progress(cb, pct_lo, pct_hi),
                )
                clock_results.extend(window_hits)
                if window_hits:
                    break

            # Deduplicate
            dedup = {}
            for addr, pat in clock_results:
                dedup[addr] = pat
            clock_results = [(a, p) for a, p in sorted(dedup.items())]
            if clock_results:
                cb(28, f"Fast path found {len(clock_results)} "
                   f"candidate match(es)")

    # --- Full scan fallback ---
    if not clock_results:
        cb(30, "Starting full physical memory scan...")
        clock_results = scan_memory(
            inpout, search_patterns, scan_opts.max_gb,
            num_threads=scan_opts.num_threads,
            progress_callback=_map_progress(cb, 30, 90),
        )
        did_full_scan = True

    if not clock_results:
        cb(100, "No PPTable pattern found in memory")
        return ScanResult([], [], [], [], did_full_scan, [],
                          error="PPTable pattern not found in memory")

    # --- Validation loop (may fall back to full scan once) ---
    while True:
        clock_addrs = [addr for addr, pat in clock_results]
        already_patched = [addr for addr, pat in clock_results
                           if pat == patched_clock_pattern]
        msglimits_addrs = [a + 28 for a in clock_addrs]

        valid_addrs = []
        rejected_addrs = []
        match_details = []

        cb(92, f"Validating {len(clock_addrs)} match(es)...")

        for i, addr in enumerate(clock_addrs):
            ml = read_msglimits(inpout, msglimits_addrs[i])
            is_patched = addr in already_patched

            page_base = addr & ~0xFFF
            page_off = addr - page_base
            v, h = inpout.map_phys(page_base, 4096)
            game_val = ctypes.c_ushort.from_address(v + page_off + 2).value
            boost_val = ctypes.c_ushort.from_address(v + page_off + 4).value
            inpout.unmap_phys(v, h)

            valid, reasons = is_valid_pptable(ml)
            if valid:
                valid_addrs.append(addr)
            else:
                rejected_addrs.append(addr)

            match_details.append({
                'addr': addr,
                'valid': valid,
                'already_patched': is_patched,
                'game_clock': game_val,
                'boost_clock': boost_val,
                'msglimits': ml,
                'reject_reasons': reasons,
            })

        if valid_addrs:
            break

        if did_full_scan:
            cb(100, "No valid PPTable copies found (all false positives)")
            return ScanResult([], already_patched, rejected_addrs,
                              clock_addrs, True, match_details,
                              error="All matches were false positives")

        cb(30, "Fast path candidates were false positives. "
           "Falling back to full scan...")
        clock_results = scan_memory(
            inpout, search_patterns, scan_opts.max_gb,
            num_threads=scan_opts.num_threads,
            progress_callback=_map_progress(cb, 30, 90),
        )
        did_full_scan = True
        if not clock_results:
            cb(100, "No PPTable pattern found in memory")
            return ScanResult([], [], [], [], True, [],
                              error="PPTable pattern not found in memory")

    save_cached_addrs(valid_addrs)
    cb(100, f"Found {len(valid_addrs)} valid PPTable(s), "
       f"{len(rejected_addrs)} rejected")

    return ScanResult(
        valid_addrs=valid_addrs,
        already_patched_addrs=[a for a in already_patched if a in valid_addrs],
        rejected_addrs=rejected_addrs,
        all_clock_addrs=clock_addrs,
        did_full_scan=did_full_scan,
        match_details=match_details,
    )


def scan_for_od_table(inpout, pattern, pptable_addrs=None, scan_opts=None,
                     progress_callback=None):
    """Scan physical memory for OD table using SMU-extracted pattern.

    Uses tiered strategy: probe cached PPTable addrs -> window scan ->
    full scan. Validates each match via validate_od_candidate().

    Args:
        inpout: InpOut32 driver instance
        pattern: bytes to search for (from extract_od_pattern)
        pptable_addrs: optional list of PPTable phys addrs for proximity scan
        scan_opts: ScanOptions (defaults if None)
        progress_callback: fn(pct, msg) for progress

    Returns:
        ODScanResult with valid_addrs and valid_tables.
    """
    if not pattern:
        return ODScanResult([], [], [], [], False,
                           error="Empty pattern (OD read failed)")

    if scan_opts is None:
        scan_opts = ScanOptions()
    cb = progress_callback or _noop_cb

    all_matches = []
    did_full_scan = False

    # Phase 1: Probe cached PPTable addrs + window scan
    cached_addrs = load_cached_addrs(max_entries=scan_opts.cache_max_addrs)
    centers = list(set((pptable_addrs or []) + cached_addrs))

    if centers:
        cb(5, f"Probing {len(centers)} address(es) for OD pattern...")
        hits = probe_cached_addrs(inpout, [pattern], centers)
        all_matches.extend(hits)

        if not all_matches:
            cb(15, f"Window scan around PPTable addrs ({scan_opts.fast_window_mb} MB)...")
            window_hits = scan_memory_windows(
                inpout, [pattern], centers,
                window_mb=scan_opts.fast_window_mb,
                max_centers=min(4, len(centers)),
                num_threads=scan_opts.num_threads,
                progress_callback=_map_progress(cb, 15, 45),
            )
            all_matches.extend(window_hits)

    # Phase 2: Full scan fallback
    if not all_matches:
        cb(50, "Full physical memory scan for OD pattern...")
        all_matches = scan_memory(
            inpout, [pattern],
            max_gb=min(2, scan_opts.max_gb),  # Limit OD scan to 2 GB initially
            num_threads=scan_opts.num_threads,
            progress_callback=_map_progress(cb, 50, 95),
        )
        did_full_scan = True

    if not all_matches:
        cb(100, "No OD table pattern found in memory")
        return ODScanResult([], [], [], [], did_full_scan,
                           error="OD table pattern not found in memory")

    # Deduplicate and validate
    seen = set()
    unique_addrs = []
    for addr, _ in all_matches:
        if addr not in seen:
            seen.add(addr)
            unique_addrs.append(addr)
    cb(96, f"Validating {len(unique_addrs)} OD candidate(s)...")

    valid_addrs = []
    valid_tables = []
    rejected = []

    for addr in unique_addrs:
        od = validate_od_candidate(inpout, addr)
        if od is not None:
            valid_addrs.append(addr)
            valid_tables.append(od)
        else:
            rejected.append(addr)

    cb(100, f"Found {len(valid_addrs)} valid OD table(s), "
       f"{len(rejected)} rejected")

    return ODScanResult(
        valid_addrs=valid_addrs,
        valid_tables=valid_tables,
        all_matches=[(a, pattern) for a in unique_addrs],
        rejected_addrs=rejected,
        did_full_scan=did_full_scan,
    )


def patch_pptable(inpout, scan_result, settings, scan_opts=None,
                  progress_callback=None, vbios_values=None):
    """Patch clock, power, and TDC limits in validated PPTable copies.

    Also scans for and patches standalone MsgLimits copies that aren't
    co-located with a clock match.

    Args:
        inpout:      InpOut32 driver instance
        scan_result: ScanResult from scan_for_pptable()
        settings:    OverclockSettings with target values
        scan_opts:   ScanOptions (for extra MsgLimits scan)
        progress_callback: fn(pct, msg) for progress updates
        vbios_values: VbiosValues for power-pattern scan; when None uses defaults.

    Returns:
        list of per-copy report dicts, each with 'addr', 'refreshing',
        'patches' (list of field dicts), and optionally 'extra_power'
        for standalone MsgLimits matches.
    """
    if scan_opts is None:
        scan_opts = ScanOptions()
    cb = progress_callback or _noop_cb
    power_pattern = vbios_values.power_pattern() if vbios_values else POWER_PATTERN
    valid_addrs = scan_result.valid_addrs
    already_patched = set(scan_result.already_patched_addrs)

    reports = []

    for i, addr in enumerate(valid_addrs):
        is_ap = addr in already_patched
        pct = (i / max(len(valid_addrs), 1)) * 70
        cb(pct, f"Patching copy {i+1}/{len(valid_addrs)} "
           f"at 0x{addr:012X}")

        patches = []
        ml_base = addr + 28
        game_clock = settings._game_clock()
        boost_clock = settings._boost_clock()
        power_ac = settings._power_ac()
        power_dc = settings._power_dc()
        tdc_gfx = settings._tdc_gfx()

        old, verify = patch_u16(inpout, addr, 2, game_clock)
        patches.append({'field': 'GameClockAc', 'old': old, 'new': verify,
                        'ok': verify == game_clock, 'unit': 'MHz'})

        old, verify = patch_u16(inpout, addr, 4, boost_clock)
        patches.append({'field': 'BoostClockAc', 'old': old, 'new': verify,
                        'ok': verify == boost_clock, 'unit': 'MHz'})

        old, verify = patch_u16(inpout, ml_base, ML_PPT0_AC, power_ac)
        patches.append({'field': 'PPT0_AC', 'old': old, 'new': verify,
                        'ok': verify == power_ac, 'unit': 'W'})

        old, verify = patch_u16(inpout, ml_base, ML_PPT0_DC, power_dc)
        patches.append({'field': 'PPT0_DC', 'old': old, 'new': verify,
                        'ok': verify == power_dc, 'unit': 'W'})

        old, verify = patch_u16(inpout, ml_base, ML_TDC_GFX, tdc_gfx)
        patches.append({'field': 'TDC_GFX', 'old': old, 'new': verify,
                        'ok': verify == tdc_gfx, 'unit': 'A'})

        if settings.tdc_soc > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TDC_SOC,
                                    settings.tdc_soc)
            patches.append({'field': 'TDC_SOC', 'old': old, 'new': verify,
                            'ok': verify == settings.tdc_soc, 'unit': 'A'})

        # Temp limits (0 = no change)
        if settings.temp_edge > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TEMP_EDGE,
                                    settings.temp_edge)
            patches.append({'field': 'Temp_Edge', 'old': old, 'new': verify,
                            'ok': verify == settings.temp_edge, 'unit': 'C'})
        if settings.temp_hotspot > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TEMP_HOTSPOT,
                                    settings.temp_hotspot)
            patches.append({'field': 'Temp_Hotspot', 'old': old, 'new': verify,
                            'ok': verify == settings.temp_hotspot, 'unit': 'C'})
        if settings.temp_mem > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TEMP_MEM,
                                    settings.temp_mem)
            patches.append({'field': 'Temp_Mem', 'old': old, 'new': verify,
                            'ok': verify == settings.temp_mem, 'unit': 'C'})
        if settings.temp_vr_gfx > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TEMP_VR_GFX,
                                    settings.temp_vr_gfx)
            patches.append({'field': 'Temp_VR_GFX', 'old': old, 'new': verify,
                            'ok': verify == settings.temp_vr_gfx, 'unit': 'C'})
        if settings.temp_vr_soc > 0:
            old, verify = patch_u16(inpout, ml_base, ML_TEMP_VR_SOC,
                                    settings.temp_vr_soc)
            patches.append({'field': 'Temp_VR_SOC', 'old': old, 'new': verify,
                            'ok': verify == settings.temp_vr_soc, 'unit': 'C'})

        reports.append({
            'addr': addr,
            'refreshing': is_ap,
            'patches': patches,
        })

    # Scan for standalone MsgLimits copies
    cb(75, "Scanning for additional MsgLimits copies...")
    power_results = scan_memory(inpout, power_pattern, scan_opts.max_gb,
                                num_threads=scan_opts.num_threads,
                                progress_callback=_map_progress(cb, 75, 95))

    all_known = set(addr + 28 for addr in scan_result.all_clock_addrs)
    extra_power = [a for a, _ in power_results
                   if not any(abs(a - ap) < 256 for ap in all_known)]

    extra_reports = []
    power_ac = settings._power_ac()
    power_dc = settings._power_dc()
    for addr in extra_power:
        old, verify = patch_u16(inpout, addr, 0, power_ac)
        ok = verify == power_ac
        patch_u16(inpout, addr, 2, power_dc)
        extra_reports.append({
            'addr': addr,
            'old': old, 'new': verify, 'ok': ok,
        })

    reports.append({'extra_power': extra_reports})

    cb(100, f"Patched {len(valid_addrs)} PPTable(s), "
       f"{len(extra_power)} extra MsgLimits")
    return reports


def apply_clocks_only(inpout, smu, scan_result, settings):
    """Apply only clock patches (GameClockAc, BoostClockAc) and freq limits."""
    results = {}
    if scan_result and scan_result.valid_addrs:
        for addr in scan_result.valid_addrs:
            patch_u16(inpout, addr, 2, settings._game_clock())
            patch_u16(inpout, addr, 4, settings._boost_clock())
    min_clock = settings.effective_min_clock
    effective_max = settings.effective_max
    param_max = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (effective_max & 0xFFFF)
    param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (min_clock & 0xFFFF)
    resp, _ = smu.send_msg(PPSMC.SetSoftMaxByFreq, param_max)
    results['soft_max'] = resp
    resp, _ = smu.send_msg(PPSMC.SetHardMaxByFreq, param_max)
    results['hard_max'] = resp
    resp, _ = smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
    results['soft_min'] = resp
    resp, _ = smu.send_msg(PPSMC.SetHardMinByFreq, param_min)
    results['hard_min'] = resp
    smu.send_msg(PPSMC.DisallowGfxOff)
    if settings.effective_lock_features:
        feat_mask = ((1 << SMU_FEATURE.DS_GFXCLK) |
                     (1 << SMU_FEATURE.GFX_ULV) |
                     (1 << SMU_FEATURE.GFXOFF))
        smu.send_msg(PPSMC.DisableSmuFeaturesLow, feat_mask)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)
    time.sleep(0.3)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)
    time.sleep(0.3)
    return results


def apply_msglimits_only(inpout, smu, scan_result, settings, scan_opts=None,
                         vbios_values=None):
    """Apply only MsgLimits patches (PPT, TDC, temps) and SetPptLimit."""
    if scan_opts is None:
        scan_opts = ScanOptions()
    power_pattern = vbios_values.power_pattern() if vbios_values else POWER_PATTERN
    results = {}
    if scan_result and scan_result.valid_addrs:
        for addr in scan_result.valid_addrs:
            ml_base = addr + 28
            patch_u16(inpout, ml_base, ML_PPT0_AC, settings._power_ac())
            patch_u16(inpout, ml_base, ML_PPT0_DC, settings._power_dc())
            patch_u16(inpout, ml_base, ML_TDC_GFX, settings._tdc_gfx())
            if settings.tdc_soc > 0:
                patch_u16(inpout, ml_base, ML_TDC_SOC, settings.tdc_soc)
            if settings.temp_edge > 0:
                patch_u16(inpout, ml_base, ML_TEMP_EDGE, settings.temp_edge)
            if settings.temp_hotspot > 0:
                patch_u16(inpout, ml_base, ML_TEMP_HOTSPOT, settings.temp_hotspot)
            if settings.temp_mem > 0:
                patch_u16(inpout, ml_base, ML_TEMP_MEM, settings.temp_mem)
            if settings.temp_vr_gfx > 0:
                patch_u16(inpout, ml_base, ML_TEMP_VR_GFX, settings.temp_vr_gfx)
            if settings.temp_vr_soc > 0:
                patch_u16(inpout, ml_base, ML_TEMP_VR_SOC, settings.temp_vr_soc)
        power_results = scan_memory(inpout, power_pattern, scan_opts.max_gb,
                                    num_threads=scan_opts.num_threads)
        all_known = set(a + 28 for a in scan_result.all_clock_addrs)
        extra = [a for a, _ in power_results
                 if not any(abs(a - ap) < 256 for ap in all_known)]
        for addr in extra:
            patch_u16(inpout, addr, 0, settings._power_ac())
            patch_u16(inpout, addr, 2, settings._power_dc())
    resp, _ = smu.send_msg(PPSMC.SetPptLimit, settings._power_ac())
    results['ppt_limit'] = resp
    return results


def apply_od_table_only(smu, virt, settings, only_offset=False):
    """Apply only OD table and freq limits (no PPTable patch, no SetPptLimit).

    When only_offset=True (Simple tab), only GfxclkFoffset is modified.
    """
    results = {}
    od = read_od(smu, virt)
    if od:
        od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFXCLK_BIT)
        od.GfxclkFoffset = settings.offset
        if not only_offset:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_PPT_BIT)
            od.Ppt = settings.od_ppt
        if not only_offset and settings.od_tdc != 0:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_TDC_BIT)
            od.Tdc = settings.od_tdc
        if not only_offset and (settings.uclk_min > 0 or settings.uclk_max > 0):
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_UCLK_BIT)
            if settings.uclk_min > 0:
                od.UclkFmin = settings.uclk_min
            if settings.uclk_max > 0:
                od.UclkFmax = settings.uclk_max
        if not only_offset and (settings.fclk_min > 0 or settings.fclk_max > 0):
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_FCLK_BIT)
            if settings.fclk_min > 0:
                od.FclkFmin = settings.fclk_min
            if settings.fclk_max > 0:
                od.FclkFmax = settings.fclk_max
        write_buf(virt, bytes(od))
        resp, _ = smu.send_msg(0x13, TABLE_OVERDRIVE)
        results['od_commit'] = resp
    min_clock = settings.effective_min_clock
    effective_max = settings.effective_max
    param_max = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (effective_max & 0xFFFF)
    param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (min_clock & 0xFFFF)
    smu.send_msg(PPSMC.SetSoftMaxByFreq, param_max)
    smu.send_msg(PPSMC.SetHardMaxByFreq, param_max)
    smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
    smu.send_msg(PPSMC.SetHardMinByFreq, param_min)
    smu.send_msg(PPSMC.DisallowGfxOff)
    if settings.effective_lock_features:
        feat_mask = ((1 << SMU_FEATURE.DS_GFXCLK) |
                     (1 << SMU_FEATURE.GFX_ULV) |
                     (1 << SMU_FEATURE.GFXOFF))
        smu.send_msg(PPSMC.DisableSmuFeaturesLow, feat_mask)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)
    time.sleep(0.3)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)
    time.sleep(0.3)
    return results


def apply_smu_features_only(smu, settings):
    """Apply only SMU features: min clock, lock features, DisallowGfxOff."""
    results = {}
    min_clock = settings.effective_min_clock
    param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (min_clock & 0xFFFF)
    resp, _ = smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
    results['soft_min'] = resp
    resp, _ = smu.send_msg(PPSMC.SetHardMinByFreq, param_min)
    results['hard_min'] = resp
    smu.send_msg(PPSMC.DisallowGfxOff)
    if settings.effective_lock_features:
        feat_mask = ((1 << SMU_FEATURE.DS_GFXCLK) |
                     (1 << SMU_FEATURE.GFX_ULV) |
                     (1 << SMU_FEATURE.GFXOFF))
        resp, _ = smu.send_msg(PPSMC.DisableSmuFeaturesLow, feat_mask)
        results['disable_features'] = resp
    return results


def apply_od_settings(smu, virt, settings):
    """Apply OD table and all frequency/power SMU commands.

    Sends: OD table commit, SetSoftMax/HardMax, SetSoftMin/HardMin,
    SetPptLimit, DisallowGfxOff, optional feature disable, workload cycle.

    Returns dict of command results.
    """
    results = {}
    min_clock = settings.effective_min_clock
    lock_features = settings.effective_lock_features

    # OD table
    od = read_od(smu, virt)
    if od:
        od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_PPT_BIT)
        od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFXCLK_BIT)
        od.Ppt = settings.od_ppt
        od.GfxclkFoffset = settings.offset
        if settings.od_tdc != 0:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_TDC_BIT)
            od.Tdc = settings.od_tdc
        if settings.uclk_min > 0 or settings.uclk_max > 0:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_UCLK_BIT)
            if settings.uclk_min > 0:
                od.UclkFmin = settings.uclk_min
            if settings.uclk_max > 0:
                od.UclkFmax = settings.uclk_max
        if settings.fclk_min > 0 or settings.fclk_max > 0:
            od.FeatureCtrlMask |= (1 << PP_OD_FEATURE_FCLK_BIT)
            if settings.fclk_min > 0:
                od.FclkFmin = settings.fclk_min
            if settings.fclk_max > 0:
                od.FclkFmax = settings.fclk_max
        write_buf(virt, bytes(od))
        resp, _ = smu.send_msg(0x13, TABLE_OVERDRIVE)
        results['od_commit'] = resp

    # Frequency limits
    effective_max = settings.effective_max
    param_max = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (effective_max & 0xFFFF)
    resp, _ = smu.send_msg(PPSMC.SetSoftMaxByFreq, param_max)
    results['soft_max'] = resp
    resp, _ = smu.send_msg(PPSMC.SetHardMaxByFreq, param_max)
    results['hard_max'] = resp

    param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (min_clock & 0xFFFF)
    resp, _ = smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
    results['soft_min'] = resp
    resp, _ = smu.send_msg(PPSMC.SetHardMinByFreq, param_min)
    results['hard_min'] = resp

    # Power limit
    resp, _ = smu.send_msg(PPSMC.SetPptLimit, settings._power_ac())
    results['ppt_limit'] = resp

    # GfxOff
    smu.send_msg(PPSMC.DisallowGfxOff)

    # Feature disable
    if lock_features:
        feat_mask = ((1 << SMU_FEATURE.DS_GFXCLK) |
                     (1 << SMU_FEATURE.GFX_ULV) |
                     (1 << SMU_FEATURE.GFXOFF))
        resp, _ = smu.send_msg(PPSMC.DisableSmuFeaturesLow, feat_mask)
        results['disable_features'] = resp

    # Workload cycle to trigger DPM refresh
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)  # PowerSave
    time.sleep(0.3)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)  # 3D Fullscreen
    time.sleep(0.3)

    return results


def verify_patches(inpout, scan_result, settings):
    """Verify all patched PPTable copies still hold the target values.

    Re-patches any copies the driver overwrote.

    Returns:
        (all_ok: bool, overwritten_count: int, details: list)
        Each detail dict has 'addr', 'ok', 'game', 'ppt', 'tdc'.
    """
    valid_addrs = scan_result.valid_addrs
    details = []
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

        game_ok = game_now == settings._game_clock()
        ppt_ok = ml['ppt0_ac'] == settings._power_ac()
        tdc_ok = ml['tdc_gfx'] == settings._tdc_gfx()
        ok = game_ok and ppt_ok and tdc_ok

        detail = {
            'addr': addr,
            'ok': ok,
            'game': game_now,
            'boost': boost_now,
            'ppt': ml['ppt0_ac'],
            'tdc': ml['tdc_gfx'],
        }

        if not ok:
            overwritten += 1
            patch_u16(inpout, addr, 2, settings._game_clock())
            patch_u16(inpout, addr, 4, settings._boost_clock())
            patch_u16(inpout, ml_base, ML_PPT0_AC, settings._power_ac())
            patch_u16(inpout, ml_base, ML_PPT0_DC, settings._power_dc())
            patch_u16(inpout, ml_base, ML_TDC_GFX, settings._tdc_gfx())
            if settings.tdc_soc > 0:
                patch_u16(inpout, ml_base, ML_TDC_SOC, settings.tdc_soc)
            detail['repatched'] = True

        details.append(detail)

    return overwritten == 0, overwritten, details


def watchdog_step(smu, virt, settings, iteration):
    """Single watchdog iteration: read metrics, re-enforce floor if needed.

    Returns dict with metrics and action taken.
    """
    min_clock = settings.effective_min_clock
    lock_features = settings.effective_lock_features

    gfxclk, gfxclk2, metrics_ppt, temp = read_metrics(smu, virt)
    below = gfxclk < min_clock and gfxclk > 0
    safety = (iteration % 12 == 0)

    action = None
    if below or safety:
        param_min = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (min_clock & 0xFFFF)
        smu.send_msg(PPSMC.SetSoftMinByFreq, param_min)
        smu.send_msg(PPSMC.SetHardMinByFreq, param_min)
        smu.send_msg(PPSMC.DisallowGfxOff)
        if lock_features:
            feat_mask = ((1 << SMU_FEATURE.DS_GFXCLK) |
                         (1 << SMU_FEATURE.GFX_ULV) |
                         (1 << SMU_FEATURE.GFXOFF))
            smu.send_msg(PPSMC.DisableSmuFeaturesLow, feat_mask)
        action = "BELOW -> re-sent min" if below else "OK (safety re-sent)"

    return {
        'gfxclk': gfxclk, 'gfxclk2': gfxclk2,
        'ppt': metrics_ppt, 'temp': temp,
        'min_clock': min_clock,
        'action': action or "OK",
    }
