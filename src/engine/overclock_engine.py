"""
overclock_engine.py -- Core Overclock Engine for Adrenalift
============================================================

Provides callable functions for GUI and CLI integration:

  init_hardware()            -> hardware handle dict
  scan_for_pptable()         -> ScanResult with validated addresses
  patch_pptable()            -> list of per-copy patch reports
  apply_od_settings()        -> dict of SMU command results (admin DMA path)
  apply_od_via_escape()      -> dict of results (no-admin D3DKMTEscape path)
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

import gc
import json
import logging
import multiprocessing as mp
import sys, os, ctypes, struct, time, threading, traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

if getattr(sys, "frozen", False):
    _project_root = os.path.dirname(sys.executable)
else:
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.io.mmio import InpOut32
from src.engine.smu import (create_smu, PPSMC, PPCLK, SMU_FEATURE,
                             _CLK_NAMES, _FEATURE_NAMES_LOW,
                             SMU_RESP_OK, SMU_RESP_FAIL, _RESP_NAMES)

_engine_log = logging.getLogger("overclock.engine")


def _elog(msg: str):
    """Write a log line to the persistent log file from the engine."""
    try:
        _engine_log.info(msg)
    except Exception:
        pass
from src.engine.od_table import (TABLE_OVERDRIVE, TABLE_SMU_METRICS, TABLE_PPTABLE,
                      TABLE_CUSTOM_SKUTABLE,
                      decode_od_fail,
                      OverDriveTable_t, _OD_TABLE_SIZE,
                      PP_OD_FEATURE_PPT_BIT, PP_OD_FEATURE_GFXCLK_BIT,
                      PP_OD_FEATURE_TDC_BIT, PP_OD_FEATURE_UCLK_BIT,
                      PP_OD_FEATURE_FCLK_BIT, PP_OD_FEATURE_GFX_VF_CURVE_BIT,
                      PP_OD_FEATURE_GFX_VMAX_BIT, PP_OD_FEATURE_SOC_VMAX_BIT,
                      PP_OD_FEATURE_FAN_CURVE_BIT, PP_OD_FEATURE_EDC_BIT,
                      PP_OD_FEATURE_FULL_CTRL_BIT,
                      PP_NUM_OD_VF_CURVE_POINTS)
from src.engine.smu_metrics import (SmuMetrics_t, SMU_METRICS_SIZE,
                                     parse_metrics, metrics_to_dict)
from src.io.escape_structures import Od8Setting, OD8_RDNA4_FIELD_MAP, OdFail
from src.io.d3dkmt_escape import D3DKMTClient, D3DKMTError
from src.app.settings import settings as _settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRIVER_BUF_OFFSET_DEFAULT = 0x0FBCC000

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

_INC_TABLE = bytes((i + 1) & 0xFF for i in range(256))
_DEC_TABLE = bytes((i - 1) & 0xFF for i in range(256))

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
# DMA offset cache  (delegates to unified settings.json)
# ---------------------------------------------------------------------------


def _load_dma_cache():
    """Load cached DMA buffer offset from disk. Returns int or None."""
    offset = _settings.get("dma_cache.offset")
    if isinstance(offset, int) and offset > 0:
        return offset
    return None


def _save_dma_cache(offset: int, method: str):
    """Persist the discovered DMA buffer offset to disk."""
    try:
        _settings.set("dma_cache", {
            "offset": offset,
            "method": method,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        _elog(f"_save_dma_cache: wrote offset=0x{offset:X} method={method}")
    except Exception as e:
        _elog(f"_save_dma_cache: failed: {e}")


# ---------------------------------------------------------------------------
# In-memory DMA offset cache (shared across threads within one process)
# ---------------------------------------------------------------------------

_inmemory_dma_lock = threading.Lock()
_inmemory_dma_offset: int | None = None
_inmemory_dma_path: str | None = None


def _get_inmemory_dma():
    """Return (offset, dma_path) from the in-memory cache, or (None, None)."""
    with _inmemory_dma_lock:
        return _inmemory_dma_offset, _inmemory_dma_path


def _set_inmemory_dma(offset: int, dma_path: str):
    """Store a discovered DMA offset in the in-memory cache."""
    global _inmemory_dma_offset, _inmemory_dma_path
    with _inmemory_dma_lock:
        _inmemory_dma_offset = offset
        _inmemory_dma_path = dma_path
    _elog(f"_set_inmemory_dma: offset=0x{offset:X} path={dma_path}")


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
    max_gb: int = 0
    num_threads: int = 0
    fast_window_mb: int = 512


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
    od_table: object = None      # OverDriveTable_t from read_od(), if available
    fingerprint_validated: bool = False  # True when scan used immutable header fingerprint


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


# ---------------------------------------------------------------------------
# Low-level memory helpers
# ---------------------------------------------------------------------------

def read_buf(virt, n):
    buf = (ctypes.c_ubyte * n)()
    ctypes.memmove(buf, virt, n)
    return bytes(buf)


def read_raw_at_addr(inpout, phys_addr, size):
    """Read raw bytes from a physical address. Returns bytes or None on failure."""
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    map_size = ((page_off + size + 0xFFF) // 0x1000) * 0x1000
    try:
        virt, handle = inpout.map_phys(page_base, map_size)
    except (IOError, OSError):
        return None
    try:
        return read_buf(virt + page_off, size)
    except (IOError, OSError):
        return None
    finally:
        inpout.unmap_phys(virt, handle)


def write_buf(virt, data):
    arr = (ctypes.c_ubyte * len(data))(*data)
    ctypes.memmove(virt, arr, len(data))


def read_od(smu, virt):
    smu.send_msg(smu.transfer_read, TABLE_OVERDRIVE)
    smu.hdp_flush()
    raw = read_buf(virt, _OD_TABLE_SIZE)
    if struct.unpack_from('<I', raw, 0)[0] <= 0x1000:
        return OverDriveTable_t.from_buffer_copy(raw)
    smu.send_msg(smu.transfer_read, TABLE_OVERDRIVE)
    smu.hdp_flush()
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
    smu.send_msg(smu.transfer_read, TABLE_SMU_METRICS)
    smu.hdp_flush()
    raw = read_buf(virt, 256)
    gfxclk = struct.unpack_from('<H', raw, 0x48)[0]
    gfxclk2 = struct.unpack_from('<H', raw, 0x4A)[0]
    ppt = struct.unpack_from('<H', raw, 0x30)[0]
    temp = struct.unpack_from('<H', raw, 0x3A)[0]
    return gfxclk, gfxclk2, ppt, temp


def read_smu_metrics_full(smu, virt):
    """Read the full SmuMetrics_t from the SMU DMA buffer.

    Sends TransferTableSmu2Dram (0x12) with TABLE_SMU_METRICS, reads
    SMU_METRICS_SIZE bytes from the mapped DMA buffer, and returns:

        (SmuMetrics_t instance, dict of flattened scalar values)

    On failure returns (None, {}) — callers should handle gracefully.

    Args:
        smu:  SmuCmd instance (from init_hardware / create_smu).
        virt: Virtual address of the mapped DMA buffer (hw['virt']).

    Returns:
        tuple[SmuMetrics_t | None, dict]
    """
    try:
        smu.send_msg(smu.transfer_read, TABLE_SMU_METRICS)
        smu.hdp_flush()
        raw = read_buf(virt, SMU_METRICS_SIZE)
        m = parse_metrics(raw)
        return m, metrics_to_dict(m)
    except Exception as e:
        _elog(f"read_smu_metrics_full: failed: {e}")
        return None, {}


def read_smu_table_raw(smu, virt, table_id, read_size=8192):
    """Read a raw SMU table via TransferTableSmu2Dram.

    Sends msg 0x12 with the given table_id, then reads read_size bytes
    from the mapped DMA buffer.  Useful for dumping PPTable, DriverInfo,
    or any other SMU table as raw bytes.

    Args:
        smu:        SmuCmd instance.
        virt:       Virtual address of the mapped DMA buffer.
        table_id:   SMU table index (TABLE_PPTABLE=0, TABLE_DRIVER_INFO=10, etc.)
        read_size:  Bytes to read from the buffer (default 8192, max ~0x3000).

    Returns:
        (resp, raw_bytes) on success — resp is the SMU response code.
        (None, None) on failure.
    """
    try:
        resp, ret = smu.send_msg(smu.transfer_read, table_id)
        smu.hdp_flush()
        raw = read_buf(virt, min(read_size, 0x3000))
        _elog(f"read_smu_table_raw: table_id={table_id} resp=0x{resp:X} "
              f"ret=0x{ret:X} read {len(raw)} bytes")
        return resp, raw
    except Exception as e:
        _elog(f"read_smu_table_raw: table_id={table_id} failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# PFE_Settings_t -- PPTable header parsing and patching
# ---------------------------------------------------------------------------
#
# Layout from smu14_driver_if_v14_0.h:
#   uint8_t  Version;           // offset 0
#   uint8_t  Spare8[3];         // offset 1
#   uint32_t FeaturesToRun[2];  // offset 4  (bits 0-31 at [0], bits 32-63 at [1])
#   uint32_t FwDStateMask;      // offset 12
#   uint32_t DebugOverrides;    // offset 16
#   uint32_t Spare[2];          // offset 20
# Total: 28 bytes

PFE_FEATURES_TO_RUN_OFFSET = 4
PFE_FW_DSTATE_MASK_OFFSET  = 12
PFE_DEBUG_OVERRIDES_OFFSET = 16
PFE_SETTINGS_SIZE          = 28

DEBUG_OVERRIDE_DISABLE_VOLT_LINK_DCN_FCLK     = 0x00000002
DEBUG_OVERRIDE_DISABLE_VOLT_LINK_MP0_FCLK     = 0x00000004
DEBUG_OVERRIDE_DISABLE_VOLT_LINK_VCN_DCFCLK   = 0x00000008
DEBUG_OVERRIDE_DISABLE_FAST_FCLK_TIMER        = 0x00000010
DEBUG_OVERRIDE_DISABLE_VCN_PG                 = 0x00000020
DEBUG_OVERRIDE_DISABLE_FMAX_VMAX              = 0x00000040
DEBUG_OVERRIDE_DISABLE_IMU_FW_CHECKS          = 0x00000080
DEBUG_OVERRIDE_DISABLE_DFLL                   = 0x00000200
DEBUG_OVERRIDE_DFLL_MASTER_MODE               = 0x00000800
DEBUG_OVERRIDE_ENABLE_PROFILING_MODE          = 0x00001000
DEBUG_OVERRIDE_ENABLE_PER_WGP_RESIENCY       = 0x00004000
DEBUG_OVERRIDE_DISABLE_MEMORY_VOLTAGE_SCALING = 0x00008000

_DEBUG_OVERRIDE_NAMES = {
    0x00000001: "NOT_USE",
    0x00000002: "DISABLE_VOLT_LINK_DCN_FCLK",
    0x00000004: "DISABLE_VOLT_LINK_MP0_FCLK",
    0x00000008: "DISABLE_VOLT_LINK_VCN_DCFCLK",
    0x00000010: "DISABLE_FAST_FCLK_TIMER",
    0x00000020: "DISABLE_VCN_PG",
    0x00000040: "DISABLE_FMAX_VMAX",
    0x00000080: "DISABLE_IMU_FW_CHECKS",
    0x00000100: "DISABLE_D0i2_REENTRY_HSR_TIMER_CHECK",
    0x00000200: "DISABLE_DFLL",
    0x00000400: "ENABLE_RLC_VF_BRINGUP_MODE",
    0x00000800: "DFLL_MASTER_MODE",
    0x00001000: "ENABLE_PROFILING_MODE",
    0x00002000: "ENABLE_SOC_VF_BRINGUP_MODE",
    0x00004000: "ENABLE_PER_WGP_RESIENCY",
    0x00008000: "DISABLE_MEMORY_VOLTAGE_SCALING",
    0x00010000: "DFLL_BTC_FCW_LOG",
}


def decode_debug_overrides(value):
    """Decode DebugOverrides bitmask into list of (flag_value, name) tuples."""
    active = []
    for flag, name in sorted(_DEBUG_OVERRIDE_NAMES.items()):
        if value & flag:
            active.append((flag, name))
    return active


def read_pfe_settings(smu, virt):
    """Read TABLE_PPTABLE via SMU DMA and parse PFE_Settings_t (first 28 bytes).

    Returns dict with version, features, FwDStateMask, DebugOverrides, or None on failure.
    """
    resp, raw = read_smu_table_raw(smu, virt, TABLE_PPTABLE, read_size=256)
    if raw is None or len(raw) < PFE_SETTINGS_SIZE:
        _elog("read_pfe_settings: failed or too short")
        return None

    version = raw[0]
    feat_lo = struct.unpack_from('<I', raw, 4)[0]
    feat_hi = struct.unpack_from('<I', raw, 8)[0]
    fw_dstate = struct.unpack_from('<I', raw, 12)[0]
    debug_ov = struct.unpack_from('<I', raw, 16)[0]
    spare0, spare1 = struct.unpack_from('<II', raw, 20)

    feat_64 = feat_lo | (feat_hi << 32)
    _elog(f"read_pfe_settings: ver={version} feat=0x{feat_64:016X} "
          f"dstate=0x{fw_dstate:08X} dbg=0x{debug_ov:08X}")

    return {
        'version': version,
        'features_to_run_lo': feat_lo,
        'features_to_run_hi': feat_hi,
        'features_to_run_64': feat_64,
        'fw_dstate_mask': fw_dstate,
        'debug_overrides': debug_ov,
        'spare': [spare0, spare1],
        'raw_28': raw[:PFE_SETTINGS_SIZE],
    }


def _read_pptable_full(smu, virt):
    """Read the full PPTable (~0x3000 bytes) from SMU DMA."""
    resp, raw = read_smu_table_raw(smu, virt, TABLE_PPTABLE, read_size=0x3000)
    if raw is None:
        raise RuntimeError("Failed to read TABLE_PPTABLE from SMU DMA")
    return raw


def _write_pptable_back(smu, virt, data):
    """Write patched PPTable data to DMA buffer and transfer to SMU."""
    write_buf(virt, bytes(data))
    smu.hdp_flush()
    resp, ret = smu.send_msg(smu.transfer_write, TABLE_PPTABLE)
    _elog(f"_write_pptable_back: resp=0x{resp:02X} ret=0x{ret:08X}")
    return resp, ret


def patch_features_to_run(smu, virt, extra_bits):
    """Patch FeaturesToRun in the PPTable to add the given feature bit positions.

    Args:
        extra_bits: list of feature bit positions to OR in (e.g. [41, 43, 49]).

    Returns dict with old/new values, SMU response, and verification read-back.
    """
    pfe = read_pfe_settings(smu, virt)
    if pfe is None:
        raise RuntimeError("Failed to read PFE_Settings_t")

    old_lo, old_hi = pfe['features_to_run_lo'], pfe['features_to_run_hi']
    new_lo, new_hi = old_lo, old_hi

    for bit in extra_bits:
        if bit < 32:
            new_lo |= (1 << bit)
        else:
            new_hi |= (1 << (bit - 32))

    features_before = smu.get_running_features()

    raw = _read_pptable_full(smu, virt)
    data = bytearray(raw)
    struct.pack_into('<I', data, PFE_FEATURES_TO_RUN_OFFSET, new_lo)
    struct.pack_into('<I', data, PFE_FEATURES_TO_RUN_OFFSET + 4, new_hi)

    resp, ret = _write_pptable_back(smu, virt, data)

    time.sleep(0.2)
    after_pfe = read_pfe_settings(smu, virt)
    features_after = smu.get_running_features()

    newly_enabled = features_after & ~features_before
    bits_added = []
    for bit in extra_bits:
        name = _FEATURE_NAMES_LOW.get(bit, f"BIT_{bit}")
        was_on = bool(features_before & (1 << bit))
        now_on = bool(features_after & (1 << bit))
        bits_added.append((bit, name, was_on, now_on))

    return {
        'old_lo': old_lo, 'old_hi': old_hi,
        'new_lo': new_lo, 'new_hi': new_hi,
        'smu_resp': resp, 'smu_ret': ret,
        'features_before': features_before,
        'features_after': features_after,
        'newly_enabled': newly_enabled,
        'bits_detail': bits_added,
        'after_pfe': after_pfe,
    }


def patch_debug_overrides(smu, virt, flags_to_set):
    """Patch DebugOverrides in PFE_Settings to set the given flag bitmask.

    Args:
        flags_to_set: uint32 bitmask of flags to OR into DebugOverrides.

    Returns dict with old/new values, SMU response, and verification.
    """
    pfe = read_pfe_settings(smu, virt)
    if pfe is None:
        raise RuntimeError("Failed to read PFE_Settings_t")

    old_dbg = pfe['debug_overrides']
    new_dbg = old_dbg | flags_to_set

    raw = _read_pptable_full(smu, virt)
    data = bytearray(raw)
    struct.pack_into('<I', data, PFE_DEBUG_OVERRIDES_OFFSET, new_dbg)

    resp, ret = _write_pptable_back(smu, virt, data)

    after_pfe = read_pfe_settings(smu, virt)

    flags_set = []
    for flag, name in sorted(_DEBUG_OVERRIDE_NAMES.items()):
        if flags_to_set & flag:
            was_set = bool(old_dbg & flag)
            now_set = bool(after_pfe['debug_overrides'] & flag) if after_pfe else None
            flags_set.append((flag, name, was_set, now_set))

    return {
        'old_debug_overrides': old_dbg,
        'new_debug_overrides': new_dbg,
        'flags_applied': flags_to_set,
        'smu_resp': resp, 'smu_ret': ret,
        'flags_detail': flags_set,
        'after_pfe': after_pfe,
    }


# ---------------------------------------------------------------------------
# Tools DRAM Path -- TABLE_PPTABLE write via msg 0x53 (Phase 6)
# ---------------------------------------------------------------------------
#
# The Driver path (msg 0x13 TransferTableDram2Smu) is rejected by PMFW for
# TABLE_PPTABLE writes (returns 0xFF FAIL).  The Tools path (msg 0x53
# TransferTableDram2SmuWithAddr) uses a separate DRAM address register
# that the Windows driver never touches.  PMFW may have different
# validation for the Tools path.


def _compute_mc_addr(mmio, phys, vram_bar):
    """Compute the GPU MC address for a CPU physical address in VRAM.

    MC address = vram_start + (phys - vram_bar), where vram_start comes
    from the MMHUB FB_LOCATION_BASE register.

    Returns (mc_addr, vram_start) or raises RuntimeError.
    """
    vram_start, raw_reg = _read_vram_start(mmio)
    offset = phys - vram_bar
    mc_addr = vram_start + offset
    _elog(f"_compute_mc_addr: phys=0x{phys:X} vram_bar=0x{vram_bar:X} "
          f"offset=0x{offset:X} vram_start=0x{vram_start:X} "
          f"mc_addr=0x{mc_addr:X} (raw_reg=0x{raw_reg:X})")
    return mc_addr, vram_start


def _write_pptable_tools_path(smu, virt, data, mmio, phys, vram_bar):
    """Write patched PPTable data via the Tools DRAM path (msg 0x53).

    Tries TABLE_PPTABLE (id=0) first, falls back to TABLE_CUSTOM_SKUTABLE
    (id=12) if PMFW rejects the primary table ID.

    Args:
        smu:      SmuCmd instance.
        virt:     Virtual address of the mapped DMA buffer.
        data:     Patched PPTable bytes to write.
        mmio:     GpuMMIO instance (for vram_start readback).
        phys:     CPU physical address of the DMA buffer.
        vram_bar: GPU BAR0 physical base address.

    Returns:
        dict with keys: mc_addr, vram_start, table_id_used, attempts
        (list of {table_id, resp, ret}), success (bool).
    """
    mc_addr, vram_start = _compute_mc_addr(mmio, phys, vram_bar)

    old_read, old_write = smu.setup_tools_dram(mc_addr)
    result = {
        'mc_addr': mc_addr,
        'vram_start': vram_start,
        'table_id_used': None,
        'attempts': [],
        'success': False,
    }
    try:
        write_buf(virt, bytes(data))
        smu.hdp_flush()

        for table_id, label in [(TABLE_PPTABLE, "TABLE_PPTABLE"),
                                 (TABLE_CUSTOM_SKUTABLE, "TABLE_CUSTOM_SKUTABLE")]:
            resp, ret = smu.transfer_table_tools_to_smu(table_id)
            attempt = {
                'table_id': table_id,
                'label': label,
                'resp': resp,
                'ret': ret,
            }
            result['attempts'].append(attempt)
            _elog(f"_write_pptable_tools_path: {label} (id={table_id}) "
                  f"resp=0x{resp:02X} ret=0x{ret:08X}")

            if resp == SMU_RESP_OK:
                result['table_id_used'] = table_id
                result['success'] = True
                _elog(f"_write_pptable_tools_path: SUCCESS via {label}")
                break
            _elog(f"_write_pptable_tools_path: {label} failed, "
                  f"resp={_RESP_NAMES.get(resp, f'0x{resp:02X}')}")
    finally:
        smu.restore_transfer_msgs(old_read, old_write)

    return result


def _read_pptable_tools_path(smu, virt, mmio, phys, vram_bar, read_size=0x3000):
    """Read TABLE_PPTABLE via the Tools DRAM path (msg 0x52).

    Returns (raw_bytes, mc_addr, vram_start) or raises RuntimeError.
    """
    mc_addr, vram_start = _compute_mc_addr(mmio, phys, vram_bar)

    smu.set_dram_addr(mc_addr, use_tools=True)
    resp, ret = smu.transfer_table_tools_from_smu(TABLE_PPTABLE)
    if resp != SMU_RESP_OK:
        raise RuntimeError(
            f"Tools path read TABLE_PPTABLE failed: "
            f"{_RESP_NAMES.get(resp, f'0x{resp:02X}')} (ret=0x{ret:08X})")
    smu.hdp_flush()
    raw = read_buf(virt, min(read_size, 0x3000))
    _elog(f"_read_pptable_tools_path: read {len(raw)} bytes via 0x52, "
          f"mc_addr=0x{mc_addr:X}")
    return raw, mc_addr, vram_start


def patch_features_tools_path(smu, virt, extra_bits, mmio, phys, vram_bar):
    """Patch FeaturesToRun via the Tools DRAM path (msg 0x53).

    Same as patch_features_to_run() but writes back using
    TransferTableDram2SmuWithAddr instead of TransferTableDram2Smu.

    Returns dict with old/new values, per-attempt SMU responses, verification.
    """
    pfe = read_pfe_settings(smu, virt)
    if pfe is None:
        raise RuntimeError("Failed to read PFE_Settings_t")

    old_lo, old_hi = pfe['features_to_run_lo'], pfe['features_to_run_hi']
    new_lo, new_hi = old_lo, old_hi

    for bit in extra_bits:
        if bit < 32:
            new_lo |= (1 << bit)
        else:
            new_hi |= (1 << (bit - 32))

    features_before = smu.get_running_features()

    raw = _read_pptable_full(smu, virt)
    data = bytearray(raw)
    struct.pack_into('<I', data, PFE_FEATURES_TO_RUN_OFFSET, new_lo)
    struct.pack_into('<I', data, PFE_FEATURES_TO_RUN_OFFSET + 4, new_hi)

    write_result = _write_pptable_tools_path(smu, virt, data, mmio, phys, vram_bar)

    time.sleep(0.2)
    after_pfe = read_pfe_settings(smu, virt)
    features_after = smu.get_running_features()

    newly_enabled = features_after & ~features_before
    bits_added = []
    for bit in extra_bits:
        name = _FEATURE_NAMES_LOW.get(bit, f"BIT_{bit}")
        was_on = bool(features_before & (1 << bit))
        now_on = bool(features_after & (1 << bit))
        bits_added.append((bit, name, was_on, now_on))

    return {
        'old_lo': old_lo, 'old_hi': old_hi,
        'new_lo': new_lo, 'new_hi': new_hi,
        'write_result': write_result,
        'features_before': features_before,
        'features_after': features_after,
        'newly_enabled': newly_enabled,
        'bits_detail': bits_added,
        'after_pfe': after_pfe,
    }


def patch_debug_tools_path(smu, virt, flags_to_set, mmio, phys, vram_bar):
    """Patch DebugOverrides via the Tools DRAM path (msg 0x53).

    Same as patch_debug_overrides() but writes back using the Tools path.
    """
    pfe = read_pfe_settings(smu, virt)
    if pfe is None:
        raise RuntimeError("Failed to read PFE_Settings_t")

    old_dbg = pfe['debug_overrides']
    new_dbg = old_dbg | flags_to_set

    raw = _read_pptable_full(smu, virt)
    data = bytearray(raw)
    struct.pack_into('<I', data, PFE_DEBUG_OVERRIDES_OFFSET, new_dbg)

    write_result = _write_pptable_tools_path(smu, virt, data, mmio, phys, vram_bar)

    after_pfe = read_pfe_settings(smu, virt)

    flags_set = []
    for flag, name in sorted(_DEBUG_OVERRIDE_NAMES.items()):
        if flags_to_set & flag:
            was_set = bool(old_dbg & flag)
            now_set = bool(after_pfe['debug_overrides'] & flag) if after_pfe else None
            flags_set.append((flag, name, was_set, now_set))

    return {
        'old_debug_overrides': old_dbg,
        'new_debug_overrides': new_dbg,
        'flags_applied': flags_to_set,
        'write_result': write_result,
        'flags_detail': flags_set,
        'after_pfe': after_pfe,
    }


def check_od_mem_timing_caps(smu, virt):
    """Check OD capabilities related to memory timing tuning.

    Reads:
    1. The current OD table FeatureCtrlMask for UCLK/FCLK OD bits.
    2. The raw PPTable at PP_OD_CAPABILITY_FLAGS_OFFSET (0x105C) for
       the OverDriveLimitsBasicMin FeatureCtrlMask baked in by VBIOS.
    3. UCLK DPM min/max from SMU queries.
    4. The OD table's current UclkFmin/UclkFmax settings.

    Returns dict with all capability data.
    """
    from src.io.escape_structures import PP_OD_CAPABILITY_FLAGS_OFFSET

    result = {'caps': {}, 'od_features': {}, 'uclk': {}, 'pptable_raw_caps': None}

    try:
        uclk_min = smu.get_min_freq(PPCLK.UCLK)
        uclk_max = smu.get_max_freq(PPCLK.UCLK)
        result['uclk']['dpm_min'] = uclk_min
        result['uclk']['dpm_max'] = uclk_max
    except Exception as e:
        result['uclk']['error'] = str(e)

    od = read_od(smu, virt)
    if od is not None:
        mask = od.FeatureCtrlMask
        result['od_features']['FeatureCtrlMask'] = mask
        result['od_features']['UCLK_bit'] = bool(mask & (1 << PP_OD_FEATURE_UCLK_BIT))
        result['od_features']['FCLK_bit'] = bool(mask & (1 << PP_OD_FEATURE_FCLK_BIT))
        result['od_features']['FULL_CTRL_bit'] = bool(
            mask & (1 << PP_OD_FEATURE_FULL_CTRL_BIT))
        result['uclk']['od_UclkFmin'] = od.UclkFmin
        result['uclk']['od_UclkFmax'] = od.UclkFmax
        result['uclk']['od_FclkFmin'] = od.FclkFmin
        result['uclk']['od_FclkFmax'] = od.FclkFmax
    else:
        result['od_features']['error'] = "Failed to read OD table"

    try:
        resp, raw = read_smu_table_raw(smu, virt, TABLE_PPTABLE, read_size=0x3000)
        if raw is not None and len(raw) > PP_OD_CAPABILITY_FLAGS_OFFSET + 4:
            cap_flags = struct.unpack_from('<I', raw, PP_OD_CAPABILITY_FLAGS_OFFSET)[0]
            result['pptable_raw_caps'] = cap_flags
            result['caps']['raw_at_0x105C'] = f"0x{cap_flags:08X}"
            result['caps']['UCLK_bit_in_pptable'] = bool(
                cap_flags & (1 << PP_OD_FEATURE_UCLK_BIT))
            result['caps']['FCLK_bit_in_pptable'] = bool(
                cap_flags & (1 << PP_OD_FEATURE_FCLK_BIT))
    except Exception as e:
        result['caps']['pptable_error'] = str(e)

    _elog(f"check_od_mem_timing_caps: result keys={list(result.keys())}")
    return result


def read_clock_block(inpout, phys_addr):
    """Read clock block (Base, Game, Boost) at phys_addr. Offsets 0, 2, 4.
    Returns dict with baseclock_ac, gameclock_ac, boostclock_ac, or None if unreadable."""
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    if page_off + 6 > 4096:
        map_size = 8192
    else:
        map_size = 4096
    try:
        virt, handle = inpout.map_phys(page_base, map_size)
    except (IOError, OSError):
        return None
    try:
        data = read_buf(virt + page_off, 6)
    finally:
        inpout.unmap_phys(virt, handle)
    return {
        'baseclock_ac':  struct.unpack_from('<H', data, 0)[0],
        'gameclock_ac':  struct.unpack_from('<H', data, 2)[0],
        'boostclock_ac': struct.unpack_from('<H', data, 4)[0],
    }


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
        'temp_hsgfx':   struct.unpack_from('<H', data, ML_TEMP_HSGFX)[0],
        'temp_hssoc':   struct.unpack_from('<H', data, ML_TEMP_HSSOC)[0],
        'temp_mem':     struct.unpack_from('<H', data, ML_TEMP_MEM)[0],
        'temp_vr_gfx':  struct.unpack_from('<H', data, ML_TEMP_VR_GFX)[0],
        'temp_vr_soc':  struct.unpack_from('<H', data, ML_TEMP_VR_SOC)[0],
    }


def read_pptable_at_addr(inpout, phys_addr):
    """Read clocks + MsgLimits from PPTable at phys_addr.
    phys_addr = clock block base; MsgLimits at phys_addr+28.
    Returns dict with baseclock_ac, gameclock_ac, boostclock_ac, ppt0_ac, ppt0_dc,
    tdc_gfx, tdc_soc, temp_edge, temp_hotspot, temp_mem, temp_vr_gfx, temp_vr_soc,
    temp_hsgfx, temp_hssoc, ppt1_ac, ppt1_dc. Returns None if unreadable."""
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    if page_off + 72 > 4096:
        map_size = 8192
    else:
        map_size = 4096
    try:
        virt, handle = inpout.map_phys(page_base, map_size)
    except (IOError, OSError):
        return None
    try:
        clock_data = read_buf(virt + page_off, 6)
        ml_data = read_buf(virt + page_off + 28, 44)
    except (IOError, OSError):
        return None
    finally:
        inpout.unmap_phys(virt, handle)

    return {
        'baseclock_ac':  struct.unpack_from('<H', clock_data, 0)[0],
        'gameclock_ac':  struct.unpack_from('<H', clock_data, 2)[0],
        'boostclock_ac': struct.unpack_from('<H', clock_data, 4)[0],
        'ppt0_ac':       struct.unpack_from('<H', ml_data, ML_PPT0_AC)[0],
        'ppt0_dc':       struct.unpack_from('<H', ml_data, ML_PPT0_DC)[0],
        'ppt1_ac':       struct.unpack_from('<H', ml_data, ML_PPT1_AC)[0],
        'ppt1_dc':       struct.unpack_from('<H', ml_data, ML_PPT1_DC)[0],
        'tdc_gfx':       struct.unpack_from('<H', ml_data, ML_TDC_GFX)[0],
        'tdc_soc':       struct.unpack_from('<H', ml_data, ML_TDC_SOC)[0],
        'temp_edge':     struct.unpack_from('<H', ml_data, ML_TEMP_EDGE)[0],
        'temp_hotspot':  struct.unpack_from('<H', ml_data, ML_TEMP_HOTSPOT)[0],
        'temp_hsgfx':    struct.unpack_from('<H', ml_data, ML_TEMP_HSGFX)[0],
        'temp_hssoc':    struct.unpack_from('<H', ml_data, ML_TEMP_HSSOC)[0],
        'temp_mem':      struct.unpack_from('<H', ml_data, ML_TEMP_MEM)[0],
        'temp_vr_gfx':   struct.unpack_from('<H', ml_data, ML_TEMP_VR_GFX)[0],
        'temp_vr_soc':   struct.unpack_from('<H', ml_data, ML_TEMP_VR_SOC)[0],
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


def read_u16(inpout, phys_addr, offset):
    """Read a uint16 at physical address + offset."""
    addr = phys_addr + offset
    page_base = addr & ~0xFFF
    page_off = addr - page_base
    virt, handle = inpout.map_phys(page_base, 4096)
    try:
        return ctypes.c_ushort.from_address(virt + page_off).value
    finally:
        inpout.unmap_phys(virt, handle)


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


def patch_u8(inpout, phys_addr, offset, new_val):
    """Patch a uint8 at physical address + offset. Returns (old, verified)."""
    addr = phys_addr + offset
    page_base = addr & ~0xFFF
    page_off = addr - page_base
    virt, handle = inpout.map_phys(page_base, 4096)
    try:
        old = ctypes.c_ubyte.from_address(virt + page_off).value
        ctypes.c_ubyte.from_address(virt + page_off).value = int(new_val) & 0xFF
        verify = ctypes.c_ubyte.from_address(virt + page_off).value
        return old, verify
    finally:
        inpout.unmap_phys(virt, handle)


def patch_u32(inpout, phys_addr, offset, new_val):
    """Patch a uint32 at physical address + offset. Returns (old, verified)."""
    addr = phys_addr + offset
    page_base = addr & ~0xFFF
    page_off = addr - page_base
    map_size = 8192 if page_off + 4 > 4096 else 4096
    virt, handle = inpout.map_phys(page_base, map_size)
    try:
        old = ctypes.c_uint.from_address(virt + page_off).value
        ctypes.c_uint.from_address(virt + page_off).value = int(new_val) & 0xFFFFFFFF
        verify = ctypes.c_uint.from_address(virt + page_off).value
        return old, verify
    finally:
        inpout.unmap_phys(virt, handle)


def validated_patch_u16(inpout, phys_addr, offset, expected_old, new_val):
    """Patch a uint16 only if current value matches expected_old.

    Returns (old, verified, patched: bool).
    patched is False if old != expected_old (value drifted, skip).
    """
    addr = phys_addr + offset
    page_base = addr & ~0xFFF
    page_off = addr - page_base
    virt, handle = inpout.map_phys(page_base, 4096)
    try:
        old = ctypes.c_ushort.from_address(virt + page_off).value
        if old != expected_old and old != new_val:
            return old, old, False
        ctypes.c_ushort.from_address(virt + page_off).value = new_val
        verify = ctypes.c_ushort.from_address(virt + page_off).value
        return old, verify, True
    finally:
        inpout.unmap_phys(virt, handle)


# ---------------------------------------------------------------------------
# Firmware physical-RAM map
# ---------------------------------------------------------------------------

_CM_RESOURCE_TYPE_MEMORY       = 3
_CM_RESOURCE_TYPE_MEMORY_LARGE = 7
_CM_RESOURCE_MEMORY_LARGE_40   = 0x0200
_CM_RESOURCE_MEMORY_LARGE_48   = 0x0400
_CM_RESOURCE_MEMORY_LARGE_64   = 0x0800

def get_physical_ram_ranges():
    """Read firmware-reported physical RAM ranges from the Windows registry.

    Parses the CM_RESOURCE_LIST stored at:
        HKLM\\HARDWARE\\RESOURCEMAP\\System Resources\\Physical Memory\\.Translated

    Returns a sorted list of (start_phys, end_phys) tuples on success,
    or None on any failure (non-Windows, permission denied, parse error).
    """
    try:
        import winreg
    except ImportError:
        _elog("get_physical_ram_ranges: winreg unavailable (non-Windows)")
        return None

    _REG_KEY = r"HARDWARE\RESOURCEMAP\System Resources\Physical Memory"
    _REG_VAL = ".Translated"

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _REG_KEY, 0, winreg.KEY_READ
        ) as key:
            raw, regtype = winreg.QueryValueEx(key, _REG_VAL)
    except OSError as exc:
        _elog(f"get_physical_ram_ranges: registry open failed: {exc}")
        return None

    if not isinstance(raw, bytes) or len(raw) < 16:
        _elog(f"get_physical_ram_ranges: unexpected data type/size ({type(raw).__name__}, {len(raw) if isinstance(raw, bytes) else '?'})")
        return None

    try:
        ranges = _parse_cm_resource_list(raw)
    except Exception as exc:
        _elog(f"get_physical_ram_ranges: parse error: {exc}")
        return None

    if not ranges:
        _elog("get_physical_ram_ranges: no CmResourceTypeMemory entries found")
        return None

    ranges.sort()
    _elog(f"get_physical_ram_ranges: {len(ranges)} range(s) from firmware")
    for start, end in ranges:
        size_mb = (end - start + 1) / (1024 * 1024)
        _elog(f"  0x{start:09X}-0x{end:09X} ({size_mb:.1f} MB)")
    return ranges


def _parse_cm_resource_list(data):
    """Parse a CM_RESOURCE_LIST binary blob into [(start, end), ...].

    Layout (all little-endian):
      CM_RESOURCE_LIST:
        u32  Count                           (number of full descriptors)
        CM_FULL_RESOURCE_DESCRIPTOR[Count]:
          u8   InterfaceType
          u32  BusNumber
          CM_PARTIAL_RESOURCE_LIST:
            u16  Version
            u16  Revision
            u32  Count                       (number of partial descriptors)
            CM_PARTIAL_RESOURCE_DESCRIPTOR[Count]:   (each 16 bytes)
              u8   Type       -- 3 = Memory, 7 = MemoryLarge
              u8   ShareDisposition
              u16  Flags      -- encodes Large40/48/64 variant
              u64  Start      (PHYSICAL_ADDRESS)
              u32  Length     -- raw; shifted for MemoryLarge
    """
    off = 0

    if len(data) < off + 4:
        raise ValueError("truncated: no CM_RESOURCE_LIST.Count")
    list_count = struct.unpack_from("<I", data, off)[0]
    off += 4

    ranges = []

    for _ in range(list_count):
        # CM_FULL_RESOURCE_DESCRIPTOR header: InterfaceType(u8) + 3 pad + BusNumber(u32)
        if len(data) < off + 8:
            raise ValueError("truncated: CM_FULL_RESOURCE_DESCRIPTOR header")
        off += 8  # skip InterfaceType(4 bytes with alignment) + BusNumber(4)

        # CM_PARTIAL_RESOURCE_LIST header
        if len(data) < off + 8:
            raise ValueError("truncated: CM_PARTIAL_RESOURCE_LIST header")
        _ver, _rev, partial_count = struct.unpack_from("<HHI", data, off)
        off += 8

        for _ in range(partial_count):
            if len(data) < off + 16:
                raise ValueError("truncated: CM_PARTIAL_RESOURCE_DESCRIPTOR")
            rtype  = struct.unpack_from("<B", data, off)[0]
            flags  = struct.unpack_from("<H", data, off + 2)[0]
            start  = struct.unpack_from("<Q", data, off + 4)[0]
            length = struct.unpack_from("<I", data, off + 12)[0]
            off += 16

            if rtype == _CM_RESOURCE_TYPE_MEMORY and length > 0:
                ranges.append((start, start + length - 1))
            elif rtype == _CM_RESOURCE_TYPE_MEMORY_LARGE and length > 0:
                if flags & _CM_RESOURCE_MEMORY_LARGE_64:
                    actual = length << 32
                elif flags & _CM_RESOURCE_MEMORY_LARGE_48:
                    actual = length << 16
                elif flags & _CM_RESOURCE_MEMORY_LARGE_40:
                    actual = length << 8
                else:
                    actual = length
                ranges.append((start, start + actual - 1))

    return ranges


_FOUR_GB = 0x100000000
_FALLBACK_MMIO_HOLE = (0xC0000000, 0xFFFFFFFF)
_FALLBACK_MAX_GB = 32


def _get_device_mmio_ranges():
    """Query Windows WMI for device MMIO ranges (Win32_DeviceMemoryAddress).

    Returns a sorted list of (start, end) tuples for all device memory
    regions, including 64-bit PCIe BARs (GPU ReBAR, NVMe, etc.) that
    sit above the 4 GB line.  Returns an empty list on failure.
    """
    try:
        import subprocess
        cmd = (
            'powershell -NoProfile -Command "'
            'Get-CimInstance Win32_DeviceMemoryAddress '
            '| ForEach-Object {'
            " Write-Output ('{0},{1}' -f $_.StartingAddress,$_.EndingAddress)"
            '}"'
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, shell=True,
        )
        ranges = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(",")
            if len(parts) == 2:
                try:
                    s, e = int(parts[0]), int(parts[1])
                    ranges.append((s, e))
                except ValueError:
                    continue
        ranges.sort()
        return ranges
    except Exception as exc:
        _elog(f"_get_device_mmio_ranges: WMI query failed: {exc}")
        return []


def _get_total_physical_memory():
    """Return total installed physical RAM in bytes, or None on failure."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys
    except Exception:
        pass
    return None


def _find_mmio_hole(ram_ranges):
    """Derive the PCI MMIO hole from firmware RAM ranges.

    The hole is the gap between the top of the highest below-4 GB RAM
    range and 0xFFFFFFFF.  Returns (hole_start, hole_end) or None.

    When firmware ranges are incomplete (common on some BIOSes that only
    enumerate a subset of physical RAM in the .Translated resource list),
    the derived hole start can be much lower than the real MMIO window.
    On systems with >4 GB RAM the hole is clamped to at least 0xC0000000
    (the standard PCI MMIO base on x86) so that legitimate RAM between
    the firmware top and 0xC0000000 is not skipped.
    """
    if not ram_ranges:
        return None
    top = 0
    for start, end in ram_ranges:
        if start < _FOUR_GB:
            top = max(top, min(end, _FOUR_GB - 1))
    if top == 0:
        return None
    hole_start = (top + 1 + CHUNK_SIZE - 1) & ~(CHUNK_SIZE - 1)

    if hole_start < _FALLBACK_MMIO_HOLE[0]:
        total_phys = _get_total_physical_memory()
        if total_phys is not None and total_phys > _FOUR_GB:
            _elog(f"_find_mmio_hole: firmware top 0x{top:09X} yields "
                  f"0x{hole_start:09X}, below standard "
                  f"0x{_FALLBACK_MMIO_HOLE[0]:09X} on "
                  f"{total_phys / (1024**3):.1f} GB system — "
                  f"clamping to standard MMIO base")
            hole_start = _FALLBACK_MMIO_HOLE[0]

    hole_end = _FOUR_GB - 1
    if hole_start >= hole_end:
        return None
    return (hole_start, hole_end)


def _compute_scan_ceiling(ram_ranges):
    """Determine the physical address ceiling for scanning.

    Prefers the firmware range ceiling, but cross-checks with
    GlobalMemoryStatusEx.  When the firmware map appears incomplete
    (only lists low-memory ranges while the system clearly has more RAM),
    reconstructs the ceiling from total physical memory + MMIO hole size.
    """
    fw_ceiling = 0
    if ram_ranges:
        fw_ceiling = max(end for _, end in ram_ranges) + 1

    total_phys = _get_total_physical_memory()
    if total_phys is None or total_phys <= fw_ceiling:
        _elog(f"_compute_scan_ceiling: fw={fw_ceiling / (1024 ** 3):.1f} GB, "
              f"os={total_phys / (1024 ** 3):.1f} GB (using firmware)"
              if total_phys else
              f"_compute_scan_ceiling: fw={fw_ceiling / (1024 ** 3):.1f} GB, "
              f"os=unavailable (using firmware)")
        return fw_ceiling

    low_ram = 0
    if ram_ranges:
        for start, end in ram_ranges:
            if start < _FOUR_GB:
                low_ram += min(end, _FOUR_GB - 1) - start + 1

    high_ram = total_phys - low_ram
    ceiling = _FOUR_GB + high_ram if high_ram > 0 else total_phys
    _elog(f"_compute_scan_ceiling: fw={fw_ceiling / (1024 ** 3):.1f} GB, "
          f"os={total_phys / (1024 ** 3):.1f} GB, "
          f"low_ram={low_ram / (1024 ** 3):.1f} GB → "
          f"ceiling={ceiling / (1024 ** 3):.1f} GB "
          f"(firmware ranges incomplete, using OS total)")
    return ceiling


def _is_scannable(phys, mmio_hole, device_mmio_ranges=()):
    """True unless *phys* falls inside any known MMIO region."""
    if mmio_hole and mmio_hole[0] <= phys <= mmio_hole[1]:
        return False
    for s, e in device_mmio_ranges:
        if s <= phys <= e:
            return False
    return True


def _build_mmio_exclusion_set(mmio_hole, device_mmio_ranges):
    """Build a set of chunk indices that overlap any known MMIO region.

    Merges the legacy below-4GB MMIO hole with above-4GB device MMIO
    ranges discovered via WMI (Win32_DeviceMemoryAddress).
    """
    all_ranges = []
    if mmio_hole:
        all_ranges.append(mmio_hole)
    for s, e in device_mmio_ranges:
        all_ranges.append((s, e))

    excluded = set()
    for rng_start, rng_end in all_ranges:
        ci_first = rng_start // CHUNK_SIZE
        ci_last = rng_end // CHUNK_SIZE
        for ci in range(ci_first, ci_last + 1):
            excluded.add(ci)
    return excluded


def _build_scannable_chunks(max_gb, ram_ranges):
    """Build the list of chunk indices to scan.

    Scans from address 0 up to the RAM ceiling, excluding:
      - The PCI MMIO hole (3-4 GB) derived from the firmware map
      - Any above-4GB device MMIO regions discovered via WMI
        (GPU Resizable BAR, NVMe BARs, etc.)

    When *ram_ranges* is None (registry read failed), falls back to the
    hardcoded 0xC0000000-0xFFFFFFFF exclusion with a *_FALLBACK_MAX_GB*
    ceiling.

    *max_gb* of 0 means "auto": computed ceiling, or *_FALLBACK_MAX_GB*
    when firmware is unavailable.
    """
    device_mmio = _get_device_mmio_ranges()
    above_4g = [(s, e) for s, e in device_mmio if s >= _FOUR_GB]
    if above_4g:
        _elog(f"_build_scannable_chunks: {len(above_4g)} above-4GB "
              f"MMIO region(s) from WMI (will exclude from scan):")
        for s, e in above_4g:
            size_mb = (e - s + 1) / (1024 * 1024)
            _elog(f"  0x{s:010X}-0x{e:010X} ({size_mb:.1f} MB)")
    else:
        _elog("_build_scannable_chunks: no above-4GB MMIO regions "
              "detected via WMI")

    if ram_ranges is None:
        effective_gb = max_gb if max_gb > 0 else _FALLBACK_MAX_GB
        max_bytes = effective_gb * 1024 * 1024 * 1024
        total_chunks = max_bytes // CHUNK_SIZE
        excluded = _build_mmio_exclusion_set(_FALLBACK_MMIO_HOLE, above_4g)
        chunk_indices = [ci for ci in range(total_chunks)
                         if ci not in excluded]
        _elog(f"_build_scannable_chunks: {len(chunk_indices)} chunks "
              f"(excluded {len(excluded)} MMIO chunks, "
              f"fallback mode, ceiling={effective_gb} GB)")
        return chunk_indices

    ceiling = _compute_scan_ceiling(ram_ranges)
    if max_gb > 0:
        ceiling = min(ceiling, max_gb * 1024 * 1024 * 1024)

    mmio_hole = _find_mmio_hole(ram_ranges)
    excluded = _build_mmio_exclusion_set(mmio_hole, above_4g)
    total_chunks = (ceiling + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunk_indices = [ci for ci in range(total_chunks)
                     if ci not in excluded]
    _elog(f"_build_scannable_chunks: {len(chunk_indices)} chunks "
          f"(excluded {len(excluded)} MMIO chunks, "
          f"ceiling={ceiling / (1024**3):.1f} GB)")
    return chunk_indices


# ---------------------------------------------------------------------------
# Memory scanning primitives
# ---------------------------------------------------------------------------

_YIELD_EVERY = 8     # chunks between GIL yields per thread
_YIELD_SECS  = 0.002 # 2 ms yield — enough for Qt event processing

# -- Multiprocessing worker state (per-process globals) -----------------------

_mp_inpout = None
_mp_progress = None


_DEEP_PROBE_SIZE = 4096


def _probe_phys_region(dll_path, phys_addr, size):
    """Subprocess target: map phys_addr and memmove *size* bytes.

    Exits 0 on success.  If the physical page is unmapped / MMIO the
    memmove triggers an access-violation that kills *this* process (exit
    code != 0) without affecting the parent.
    """
    inp = InpOut32(dll_path=dll_path)
    page_base = phys_addr & ~0xFFF
    page_off = phys_addr - page_base
    map_size = ((page_off + size + 0xFFF) // 0x1000) * 0x1000
    virt, handle = inp.map_phys(page_base, map_size)
    try:
        buf = (ctypes.c_ubyte * size)()
        ctypes.memmove(buf, virt + page_off, size)
    finally:
        inp.unmap_phys(virt, handle)


def probe_phys_readable(inpout, phys_addr, size=_DEEP_PROBE_SIZE, timeout=5.0):
    """Check if *size* bytes at *phys_addr* are readable without AV.

    Runs the read in a disposable subprocess so an access-violation kills
    the child, not the caller.  Returns True if the read succeeded.
    """
    p = mp.Process(target=_probe_phys_region,
                   args=(inpout._dll._name, phys_addr, size))
    p.start()
    p.join(timeout=timeout)
    if p.is_alive():
        p.terminate()
        p.join()
        return False
    return p.exitcode == 0


def _mp_init_worker(dll_path, progress_counter):
    """Per-process initializer: load InpOut32 and store shared counter."""
    global _mp_inpout, _mp_progress
    _mp_inpout = InpOut32(dll_path=dll_path)
    _mp_progress = progress_counter


def _mp_scan_range(indices, inc_patterns):
    """Scan a range of chunk indices in a worker process.

    Receives pre-incremented patterns (+1 per byte).  Physical memory is
    also incremented via translate() before comparison, so the real search
    pattern never appears in the worker heap — eliminating ghost matches
    from self-contamination.
    """
    local_found = []
    for ci in indices:
        phys_base = ci * CHUNK_SIZE
        try:
            virt, handle = _mp_inpout.map_phys(phys_base, CHUNK_SIZE)
        except (IOError, OSError):
            with _mp_progress.get_lock():
                _mp_progress.value += 1
            continue
        try:
            buf, safe_len = _safe_read_mapped(virt, CHUNK_SIZE)
            if safe_len == CHUNK_SIZE:
                data = bytes(buf)
                inc_data = data.translate(_INC_TABLE)
                for pi, inc_pat in enumerate(inc_patterns):
                    pos = 0
                    while True:
                        idx = inc_data.find(inc_pat, pos)
                        if idx < 0:
                            break
                        local_found.append((phys_base + idx, pi))
                        pos = idx + 2
        finally:
            _mp_inpout.unmap_phys(virt, handle)
        with _mp_progress.get_lock():
            _mp_progress.value += 1
    return local_found


def _mp_scan_range_windows(phys_bases, inc_patterns):
    """Scan physical base addresses in a worker process (window scan)."""
    local_found = []
    for phys_base in phys_bases:
        try:
            virt, handle = _mp_inpout.map_phys(phys_base, CHUNK_SIZE)
        except (IOError, OSError):
            with _mp_progress.get_lock():
                _mp_progress.value += 1
            continue
        try:
            buf, safe_len = _safe_read_mapped(virt, CHUNK_SIZE)
            if safe_len == CHUNK_SIZE:
                data = bytes(buf)
                inc_data = data.translate(_INC_TABLE)
                for pi, inc_pat in enumerate(inc_patterns):
                    pos = 0
                    while True:
                        idx = inc_data.find(inc_pat, pos)
                        if idx < 0:
                            break
                        local_found.append((phys_base + idx, pi))
                        pos = idx + 2
        finally:
            _mp_inpout.unmap_phys(virt, handle)
        with _mp_progress.get_lock():
            _mp_progress.value += 1
    return local_found


def scan_memory(inpout, patterns, max_gb=0, num_threads=0,
                progress_callback=None, _inc_patterns=None):
    """Scan physical memory for byte patterns using worker processes.

    Uses ProcessPoolExecutor for true CPU parallelism.  Patterns are
    byte-incremented (+1 per byte) before dispatch so the real search
    pattern never appears in worker heaps — eliminating ghost matches
    from self-contamination.  Falls back to ThreadPoolExecutor if
    process spawning fails.

    If *_inc_patterns* is provided, uses them directly (caller already
    incremented the patterns and scrubbed originals from the heap).

    *max_gb* of 0 (default) means auto-detect from firmware RAM map.

    Returns list of (phys_addr, matched_pattern) tuples.
    progress_callback(pct, msg) is called periodically during the scan.
    """
    if isinstance(patterns, bytes):
        patterns = [patterns]
    cb = progress_callback or _noop_cb

    ram_ranges = get_physical_ram_ranges()
    chunk_indices = _build_scannable_chunks(max_gb, ram_ranges)
    scannable_gb = len(chunk_indices) * CHUNK_SIZE / (1024 ** 3)

    if ram_ranges is not None:
        range_strs = [
            f"0x{s:09X}-0x{e:09X} ({(e - s + 1) / (1024 ** 3):.1f} GB)"
            for s, e in ram_ranges
        ]
        mmio_hole = _find_mmio_hole(ram_ranges)
        ceiling = _compute_scan_ceiling(ram_ranges)
        if max_gb > 0:
            ceiling = min(ceiling, max_gb * 1024 * 1024 * 1024)
        map_line = f"Physical RAM map: {', '.join(range_strs)}"
        if mmio_hole:
            hole_mb = (mmio_hole[1] - mmio_hole[0] + 1) / (1024 * 1024)
            hole_line = (f"MMIO hole: 0x{mmio_hole[0]:09X}-0x{mmio_hole[1]:09X} "
                         f"({hole_mb:.0f} MB excluded)")
        else:
            hole_line = "MMIO hole: not detected"
        scan_line = (f"Scannable: {scannable_gb:.1f} GB, "
                     f"ceiling 0x{ceiling:09X} "
                     f"({ceiling / (1024 ** 3):.1f} GB)")
        for msg in (map_line, hole_line, scan_line):
            _elog(msg)
            cb(0, msg)
    else:
        effective = max_gb if max_gb > 0 else _FALLBACK_MAX_GB
        fallback_msg = (f"Physical RAM map: unavailable, using hardcoded "
                        f"MMIO exclusion (0xC0000000-0xFFFFFFFF), "
                        f"ceiling {effective} GB")
        _elog(fallback_msg)
        cb(0, fallback_msg)

    scannable = len(chunk_indices)

    if num_threads and num_threads > 0:
        num_workers = num_threads
    else:
        num_workers = max(1, min(4, (os.cpu_count() or 4)))
    per_thread = (scannable + num_workers - 1) // num_workers
    ranges = [chunk_indices[i * per_thread:(i + 1) * per_thread]
              for i in range(num_workers)]
    ranges = [r for r in ranges if r]

    t0 = time.perf_counter()
    inc_patterns = _inc_patterns or [p.translate(_INC_TABLE) for p in patterns]

    try:
        dll_path = inpout._dll._name
        shared_progress = mp.Value('i', 0)

        all_found = []
        with ProcessPoolExecutor(
            max_workers=len(ranges),
            initializer=_mp_init_worker,
            initargs=(dll_path, shared_progress),
        ) as pool:
            futures = [pool.submit(_mp_scan_range, r, inc_patterns)
                       for r in ranges]

            while not all(f.done() for f in futures):
                done = shared_progress.value
                if done > 0:
                    pct = done / scannable * 100
                    gb = done * CHUNK_SIZE / (1024 ** 3)
                    cb(pct, f"{pct:.1f}% ({gb:.1f} GB scanned)")
                time.sleep(0.15)

            for fut in futures:
                all_found.extend(fut.result())

    except Exception as exc:
        _elog(f"ProcessPoolExecutor failed ({exc}), falling back to threads")

        lock = threading.Lock()
        progress = [0]

        def _scan_range(indices):
            local_found = []
            batch = 0
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
                    inc_data = data.translate(_INC_TABLE)
                    for pi, inc_pat in enumerate(inc_patterns):
                        pos = 0
                        while True:
                            idx = inc_data.find(inc_pat, pos)
                            if idx < 0:
                                break
                            local_found.append((phys_base + idx, pi))
                            pos = idx + 2
                finally:
                    inpout.unmap_phys(virt, handle)

                with lock:
                    progress[0] += 1
                    done = progress[0]
                batch += 1

                if done % 8 == 0 or done == scannable:
                    pct = done / scannable * 100
                    gb = done * CHUNK_SIZE / (1024 ** 3)
                    cb(pct, f"{pct:.1f}% ({gb:.1f} GB scanned)")

                if batch % _YIELD_EVERY == 0:
                    time.sleep(_YIELD_SECS)

            return local_found

        all_found = []
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [pool.submit(_scan_range, r) for r in ranges]
            for fut in as_completed(futures):
                all_found.extend(fut.result())

    all_found = [(addr, patterns[pi]) for addr, pi in all_found]

    seen = set()
    deduped = []
    for addr, pat in all_found:
        if addr not in seen:
            seen.add(addr)
            deduped.append((addr, pat))

    elapsed = time.perf_counter() - t0
    total_gb = scannable * CHUNK_SIZE / (1024 ** 3)
    cb(100, f"Done: {len(deduped)} match(es) in {elapsed:.1f}s "
       f"[{num_workers} workers, {total_gb:.1f} GB]")
    return deduped


def scan_memory_windows(inpout, patterns, centers, window_mb=512,
                        max_centers=None, num_threads=0,
                        progress_callback=None, _inc_patterns=None):
    """Scan small windows around candidate physical addresses.

    Uses ProcessPoolExecutor with byte-incremented patterns (same
    anti-ghost technique as scan_memory).  Falls back to ThreadPoolExecutor.
    If *_inc_patterns* is provided, uses them directly.
    """
    if isinstance(patterns, bytes):
        patterns = [patterns]
    cb = progress_callback or _noop_cb
    if not centers:
        return []
    if max_centers is not None and max_centers > 0:
        centers = centers[:max_centers]

    ram_ranges = get_physical_ram_ranges()
    if ram_ranges is not None:
        mmio_hole = _find_mmio_hole(ram_ranges)
        range_strs = [
            f"0x{s:09X}-0x{e:09X} ({(e - s + 1) / (1024 ** 3):.1f} GB)"
            for s, e in ram_ranges
        ]
        map_line = f"Physical RAM map: {', '.join(range_strs)}"
        _elog(map_line)
        cb(0, map_line)
    else:
        mmio_hole = _FALLBACK_MMIO_HOLE
        fallback_msg = ("Physical RAM map: unavailable, using hardcoded "
                        "MMIO exclusion (0xC0000000-0xFFFFFFFF)")
        _elog(fallback_msg)
        cb(0, fallback_msg)

    device_mmio = _get_device_mmio_ranges()
    above_4g_mmio = [(s, e) for s, e in device_mmio if s >= _FOUR_GB]

    half = (window_mb * 1024 * 1024) // 2
    chunks = set()
    for c in centers:
        start = max(0, c - half)
        end = c + half
        phys = (start // CHUNK_SIZE) * CHUNK_SIZE
        while phys < end:
            if _is_scannable(phys, mmio_hole, above_4g_mmio):
                chunks.add(phys)
            phys += CHUNK_SIZE

    if not chunks:
        return []

    chunk_list = sorted(chunks)
    total = len(chunk_list)
    scannable_gb = total * CHUNK_SIZE / (1024 ** 3)
    win_msg = (f"Window scan: {len(centers)} center(s), "
               f"{window_mb} MB window, "
               f"{scannable_gb:.2f} GB scannable in {total} chunk(s)")
    _elog(win_msg)
    cb(0, win_msg)

    if num_threads and num_threads > 0:
        num_workers = num_threads
    else:
        num_workers = max(1, min(4, (os.cpu_count() or 4)))
    per_thread = (total + num_workers - 1) // num_workers
    ranges = [chunk_list[i * per_thread:(i + 1) * per_thread]
              for i in range(num_workers)]
    ranges = [r for r in ranges if r]

    t0 = time.perf_counter()
    inc_patterns = _inc_patterns or [p.translate(_INC_TABLE) for p in patterns]

    try:
        dll_path = inpout._dll._name
        shared_progress = mp.Value('i', 0)

        found = []
        with ProcessPoolExecutor(
            max_workers=len(ranges),
            initializer=_mp_init_worker,
            initargs=(dll_path, shared_progress),
        ) as pool:
            futures = [pool.submit(_mp_scan_range_windows, r, inc_patterns)
                       for r in ranges]

            while not all(f.done() for f in futures):
                done = shared_progress.value
                if done > 0:
                    pct = done / total * 100
                    gb = done * CHUNK_SIZE / (1024 ** 3)
                    cb(pct, f"Window scan: {gb:.1f} GB")
                time.sleep(0.15)

            for fut in futures:
                found.extend(fut.result())

    except Exception as exc:
        _elog(f"ProcessPoolExecutor failed ({exc}), falling back to threads")

        lock = threading.Lock()
        progress = [0]

        def _scan_range(phys_ranges):
            local = []
            batch = 0
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
                    inc_data = data.translate(_INC_TABLE)
                    for pi, inc_pat in enumerate(inc_patterns):
                        pos = 0
                        while True:
                            idx = inc_data.find(inc_pat, pos)
                            if idx < 0:
                                break
                            local.append((phys_base + idx, pi))
                            pos = idx + 2
                finally:
                    inpout.unmap_phys(virt, handle)

                with lock:
                    progress[0] += 1
                    done = progress[0]
                batch += 1

                if done % 8 == 0 or done == total:
                    pct = done / total * 100
                    gb = done * CHUNK_SIZE / (1024 ** 3)
                    cb(pct, f"Window scan: {gb:.1f} GB")

                if batch % _YIELD_EVERY == 0:
                    time.sleep(_YIELD_SECS)

            return local

        found = []
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = [pool.submit(_scan_range, r) for r in ranges]
            for fut in as_completed(futures):
                found.extend(fut.result())

    found = [(addr, patterns[pi]) for addr, pi in found]

    seen = set()
    deduped = []
    for addr, pat in found:
        if addr not in seen:
            seen.add(addr)
            deduped.append((addr, pat))

    elapsed = time.perf_counter() - t0
    total_gb = total * CHUNK_SIZE / (1024 ** 3)
    cb(100, f"Window scan done: {len(deduped)} match(es) in {elapsed:.2f}s "
       f"[{total_gb:.2f} GB, {len(ranges)} workers]")
    return deduped


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def _validate_metrics_at(virt, smu, transfer_msg, table_id=TABLE_SMU_METRICS):
    """Send a metrics transfer and check the result looks like real data.

    Fills the buffer with a sentinel pattern first, sends the transfer,
    waits briefly for DMA completion, then checks for valid metrics.
    Returns True when MetricsCounter > 0 and GFXCLK is in 100-5000 MHz.
    """
    import time
    try:
        sentinel = bytes([0xAA] * 64)
        write_buf(virt, sentinel)
        smu.hdp_flush()

        resp, _ = smu.send_msg(transfer_msg, table_id)
        if resp != 1:
            _elog(f"_validate: SMU resp=0x{resp:X} (not OK)")
            return False

        for attempt in range(3):
            smu.hdp_flush()
            if attempt > 0:
                time.sleep(0.05 * (2 ** attempt))

            raw = read_buf(virt, SMU_METRICS_SIZE)
            head = raw[:16].hex()

            still_sentinel = raw[:64] == sentinel
            if not still_sentinel:
                break
        else:
            _elog(f"_validate: FAILED — sentinel unchanged after 3 flush+read "
                  f"attempts (DMA target wrong or HDP remap not active). "
                  f"head={head}")
            return False

        m = parse_metrics(raw)
        gfxclk = m.CurrClock[0]
        mc = m.MetricsCounter
        _elog(f"_validate: sentinel cleared on attempt {attempt}, head={head} "
              f"MetricsCounter={mc} GFXCLK={gfxclk}")

        if mc == 0:
            _elog("_validate: FAILED — MetricsCounter=0")
            return False
        if not (100 <= gfxclk <= 5000):
            _elog(f"_validate: FAILED — GFXCLK={gfxclk} out of range")
            return False
        return True
    except Exception as e:
        _elog(f"_validate: exception: {e}")
        return False


# SMN addresses for MMMC_VM_FB_LOCATION_BASE across MMHUB generations.
# Register index 0x0554, base varies by IP version.  Byte addr = (base+0x554)*4.
_FB_LOC_BASE_SMN_CANDIDATES = [
    ("mmhub_v4.1_seg0", (0x0001A000 + 0x0554) * 4),   # 0x69550 -- dGPU RDNA1-4
    ("mmhub_v4.1_seg2", (0x02408800 + 0x0554) * 4),   # RDNA3/4 alt segment
    ("mmhub_apu_seg0",  (0x00013200 + 0x0554) * 4),   # APU (Yellow Carp etc.)
]


def _read_vram_start(mmio):
    """Read gmc.vram_start from the MMHUB FB_LOCATION_BASE register via SMN.

    Tries several candidate SMN addresses (varies by MMHUB IP version).
    Returns (vram_start, raw_reg) or (0, 0) if unreadable.
    """
    for name, smn_addr in _FB_LOC_BASE_SMN_CANDIDATES:
        try:
            raw = mmio.smn_read32(smn_addr)
            fb_base = (raw & 0x00FFFFFF) << 24
            if raw != 0 and raw != 0xFFFFFFFF:
                return fb_base, raw
        except Exception as e:
            _elog(f"_read_vram_start: {name} SMN 0x{smn_addr:X} failed: {e}")
    return 0, 0


def _write_read_test(inpout, vram_bar, offset):
    """Write a marker to vram_bar+offset and read back. Returns True if OK."""
    import struct as _st
    phys = vram_bar + offset
    try:
        v, h = inpout.map_phys(phys, 0x1000)
        marker = _st.pack('<I', 0xDEADBEEF)
        write_buf(v, marker)
        rb = read_buf(v, 4)
        inpout.unmap_phys(v, h)
        return rb == marker
    except Exception:
        return False


def _detect_bar_size(inpout, vram_bar):
    """Probe the BAR aperture size by write-read testing at power-of-2 offsets.

    Returns the largest accessible offset (conservative lower bound for BAR size).
    """
    last_good = 0
    for shift in range(20, 35):  # 1MB .. 16GB
        offset = 1 << shift
        if _write_read_test(inpout, vram_bar, offset):
            last_good = offset
        else:
            break
    _elog(f"_detect_bar: last_good_offset=0x{last_good:X} "
          f"(~{last_good // (1 << 20)}MB BAR)")
    return last_good


_DISPLAY_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
)
_AMD_VENDOR_PREFIX = "VEN_1002"


def _detect_vram_size(inpout=None, vram_bar=None):
    """Return GPU VRAM size in bytes via the Windows registry.

    Reads ``HardwareInformation.qwMemorySize`` (REG_QWORD) from each
    numeric subkey of the Display adapter class key, selecting the first
    AMD entry (MatchingDeviceId contains VEN_1002).

    Falls back to ``_detect_bar_size()`` when the registry read fails
    (non-Windows, missing key, permissions, etc.).
    """
    try:
        import winreg
    except ImportError:
        _elog("_detect_vram_size: winreg unavailable, falling back to BAR probe")
        if inpout is not None and vram_bar is not None:
            return _detect_bar_size(inpout, vram_bar)
        return 0

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_PATH, 0, winreg.KEY_READ
        ) as parent:
            idx = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(parent, idx)
                except OSError:
                    break
                idx += 1

                if not subkey_name.isdigit():
                    continue

                sub_path = _DISPLAY_CLASS_PATH + "\\" + subkey_name
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, sub_path, 0, winreg.KEY_READ
                    ) as k:
                        try:
                            mdid, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
                            if not (isinstance(mdid, str)
                                    and _AMD_VENDOR_PREFIX in mdid.upper()):
                                continue
                        except OSError:
                            continue

                        # REG_QWORD (8-byte) — preferred
                        try:
                            val, vtype = winreg.QueryValueEx(
                                k, "HardwareInformation.qwMemorySize")
                            if vtype == winreg.REG_QWORD and isinstance(val, int) and val > 0:
                                _elog(f"_detect_vram_size: registry qwMemorySize = "
                                      f"0x{val:X} ({val // (1 << 20)} MB) "
                                      f"[subkey {subkey_name}]")
                                return val
                        except OSError:
                            pass

                        # REG_DWORD fallback (older drivers store 32-bit MemorySize)
                        try:
                            val, vtype = winreg.QueryValueEx(
                                k, "HardwareInformation.MemorySize")
                            if vtype in (winreg.REG_DWORD,
                                         winreg.REG_DWORD_LITTLE_ENDIAN) \
                                    and isinstance(val, int) and val > 0:
                                _elog(f"_detect_vram_size: registry MemorySize = "
                                      f"0x{val:X} ({val // (1 << 20)} MB) "
                                      f"[subkey {subkey_name}]")
                                return val
                        except OSError:
                            pass
                except OSError:
                    continue

    except OSError as e:
        _elog(f"_detect_vram_size: registry enumeration failed: {e}")

    _elog("_detect_vram_size: registry read failed, falling back to BAR probe")
    if inpout is not None and vram_bar is not None:
        return _detect_bar_size(inpout, vram_bar)
    return 0


def _scan_for_driver_buffer(smu, inpout, vram_bar, bar_limit):
    """Scan the visible BAR for the driver's DMA buffer.

    Sends TransferTableSmu2Dram (0x12) so the SMU writes metrics to the
    address the Windows driver registered, then reads the BAR in chunks
    looking for 4KB-aligned pages that match the SmuMetrics_t field layout.

    Returns (offset, virt, handle, phys) on success,
    or (None, None, None, None) when no valid buffer is found.
    """
    import struct as _st
    import time

    smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_SMU_METRICS)
    smu.hdp_flush()
    time.sleep(0.15)
    smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_SMU_METRICS)
    smu.hdp_flush()
    time.sleep(0.15)

    scan_size = min(bar_limit + 0x1000, 0x8000000)
    PAGE = 0x1000
    CHUNK = 0x400000

    OFF_GFXCLK = 0
    OFF_UCLK   = 8
    OFF_MC     = SmuMetrics_t.MetricsCounter.offset
    OFF_PWR    = SmuMetrics_t.AverageSocketPower.offset

    candidates = []

    for chunk_base in range(0, scan_size, CHUNK):
        chunk_sz = min(CHUNK, scan_size - chunk_base)
        try:
            cv, ch = inpout.map_phys(vram_bar + chunk_base, chunk_sz)
            snap = read_buf(cv, chunk_sz)
            inpout.unmap_phys(cv, ch)
        except Exception as e:
            _elog(f"_scan: chunk 0x{chunk_base:X} failed: {e}")
            continue

        for pg in range(0, chunk_sz, PAGE):
            page = snap[pg:pg + PAGE]
            if len(page) < OFF_PWR + 2:
                continue

            mc  = _st.unpack_from('<I', page, OFF_MC)[0]
            pwr = _st.unpack_from('<H', page, OFF_PWR)[0]
            if mc == 0 or mc >= 0x80000000 or pwr == 0 or pwr > 600:
                continue

            gfx  = _st.unpack_from('<I', page, OFF_GFXCLK)[0]
            uclk = _st.unpack_from('<I', page, OFF_UCLK)[0]
            if gfx == 0 or gfx > 5000 or uclk > 5000:
                continue

            off = chunk_base + pg
            candidates.append((off, gfx, uclk, mc, pwr))

    _elog(f"_scan: {len(candidates)} candidate(s)")

    if not candidates:
        return (None, None, None, None)

    time.sleep(0.5)
    smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_SMU_METRICS)
    smu.hdp_flush()
    time.sleep(0.15)

    for off, gfx, uclk, mc_old, pwr in candidates:
        try:
            v, h = inpout.map_phys(vram_bar + off, 0x4000)
            raw = read_buf(v, SMU_METRICS_SIZE)
            m = parse_metrics(raw)
            if m.MetricsCounter > mc_old and m.MetricsCounter < 0x80000000:
                return (off, v, h, vram_bar + off)
            inpout.unmap_phys(v, h)
        except Exception:
            continue

    _elog("_scan: no candidate passed MetricsCounter validation")
    return (None, None, None, None)


def _scan_for_driver_buffer_fp(smu, inpout, vram_bar, bar_limit,
                                pp_fingerprint, fp_offset_in_pp):
    """Scan BAR for the PP table fingerprint to locate the DMA buffer.

    Sends TransferTableSmu2Dram(TABLE_PPTABLE) so the SMU writes the full
    PP table at offset 0 of the driver's DMA buffer, then scans the BAR
    for the known-immutable fingerprint bytes.  Single-pass exact match --
    no heuristics, no timing delays.

    Returns (offset, virt, handle, phys) on success,
    or (None, None, None, None) when no valid buffer is found.
    """
    import time

    scan_size = 0x20000000  # 512 MB — fingerprint scan is read-only, safe beyond BAR probe limit
    CHUNK = 0x400000
    fp_len = len(pp_fingerprint)

    _elog(f"_scan_fp: scanning {scan_size / (1 << 20):.0f} MB for "
          f"{fp_len}-byte fingerprint (fp_offset_in_pp=0x{fp_offset_in_pp:X})")

    for chunk_base in range(0, scan_size, CHUNK):
        # Re-send PP table transfer before every chunk read to defeat
        # the Windows driver racing us with metrics transfers.
        smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_PPTABLE)
        smu.hdp_flush()

        chunk_sz = min(CHUNK, scan_size - chunk_base)
        try:
            cv, ch = inpout.map_phys(vram_bar + chunk_base, chunk_sz)
            snap = read_buf(cv, chunk_sz)
            inpout.unmap_phys(cv, ch)
        except Exception as e:
            _elog(f"_scan_fp: chunk 0x{chunk_base:X} failed: {e}")
            continue

        search_start = 0
        while True:
            idx = snap.find(pp_fingerprint, search_start)
            if idx < 0:
                break
            search_start = idx + 1

            buf_offset = chunk_base + idx - fp_offset_in_pp
            if buf_offset < 0:
                _elog(f"_scan_fp: fingerprint at 0x{chunk_base + idx:X} gives "
                      f"negative buf_offset 0x{buf_offset:X}, skipping")
                continue

            _elog(f"_scan_fp: fingerprint match at BAR+0x{chunk_base + idx:X}, "
                  f"DMA base offset=0x{buf_offset:X}")

            try:
                v, h = inpout.map_phys(vram_bar + buf_offset, 0x4000)
                valid = _validate_metrics_at(
                    v, smu, PPSMC.TransferTableSmu2Dram)
                if valid:
                    _elog(f"_scan_fp: metrics validation OK at "
                          f"offset 0x{buf_offset:X}")
                    return (buf_offset, v, h, vram_bar + buf_offset)
                _elog(f"_scan_fp: metrics validation FAILED at "
                      f"offset 0x{buf_offset:X}")
                inpout.unmap_phys(v, h)
            except Exception as e:
                _elog(f"_scan_fp: validation map failed at "
                      f"offset 0x{buf_offset:X}: {e}")

    _elog("_scan_fp: no fingerprint match found in BAR")
    return (None, None, None, None)


def vram_scan_for_dma(smu, inpout, vram_bar, vbios_values=None,
                      progress_callback=None):
    """Scan full GPU VRAM for the DMA buffer using a single PP table transfer.

    Unlike ``_scan_for_driver_buffer_fp`` which re-sends TABLE_PPTABLE every
    chunk (making a 16 GB scan infeasible), this function sends a single
    TransferTableSmu2Dram(TABLE_PPTABLE) up front.  The inner fingerprint
    (MsgLimits region at ~DMA+0xA48) lies beyond the zone that metrics
    transfers overwrite, so it persists across subsequent driver activity.

    Falls back to heuristic metrics scan (``_scan_for_driver_buffer``) when
    no fingerprint match is found.

    Args:
        smu:               SmuCmd instance.
        inpout:            InpOut32 driver instance.
        vram_bar:          Physical base address of the GPU BAR.
        vbios_values:      VbiosValues with pp_inner_fingerprint / pp_inner_fp_dma_offset.
        progress_callback: fn(pct: float, msg: str) for progress updates.

    Returns:
        dict with ``offset``, ``method``, ``vram_size`` on success, or ``None``.
    """
    cb = progress_callback or _noop_cb

    # --- Determine scan range -------------------------------------------------
    vram_size = _detect_vram_size(inpout, vram_bar)
    if vram_size <= 0:
        vram_size = _detect_bar_size(inpout, vram_bar)
    if vram_size <= 0:
        cb(100, "VRAM scan failed: could not determine VRAM/BAR size")
        _elog("vram_scan_for_dma: cannot determine scan range")
        return None

    vram_mb = vram_size / (1 << 20)
    cb(2, f"VRAM size: {vram_mb:.0f} MB — scanning full range")
    _elog(f"vram_scan_for_dma: vram_size=0x{vram_size:X} ({vram_mb:.0f} MB)")

    # --- Resolve fingerprint --------------------------------------------------
    inner_fp = (vbios_values.pp_inner_fingerprint
                if vbios_values is not None else b'')
    fp_dma_offset = (vbios_values.pp_inner_fp_dma_offset
                     if vbios_values is not None else 0)

    if not inner_fp:
        cb(5, "No inner PP fingerprint — skipping fingerprint phase, "
              "falling back to heuristic scan")
        _elog("vram_scan_for_dma: no inner fingerprint, going straight "
              "to heuristic fallback")
    else:
        cb(5, f"Fingerprint: {len(inner_fp)} bytes at DMA+0x{fp_dma_offset:X}")
        _elog(f"vram_scan_for_dma: inner_fp={len(inner_fp)}B "
              f"fp_dma_offset=0x{fp_dma_offset:X}")

        # --- Single TABLE_PPTABLE transfer ------------------------------------
        cb(6, "Sending single TransferTableSmu2Dram(TABLE_PPTABLE)...")
        smu.send_msg(PPSMC.TransferTableSmu2Dram, TABLE_PPTABLE)
        smu.hdp_flush()

        # --- Chunked BAR scan -------------------------------------------------
        CHUNK = 4 * 1024 * 1024  # 4 MB
        scan_limit = min(vram_size, 0x400000000)  # cap at 16 GB
        total_chunks = (scan_limit + CHUNK - 1) // CHUNK

        # Build interleaved scan order: alternate bottom/top converging
        # toward the middle.  DMA buffers tend to sit near either end of
        # VRAM, so this finds them much faster than a linear sweep.
        scan_order = []
        lo, hi = 0, total_chunks - 1
        while lo <= hi:
            scan_order.append(lo)
            if hi != lo:
                scan_order.append(hi)
            lo += 1
            hi -= 1

        _elog(f"vram_scan_for_dma: scanning {total_chunks} x 4 MB chunks "
              f"({scan_limit / (1 << 20):.0f} MB) [interleaved bottom/top]")

        for done, ci in enumerate(scan_order):
            chunk_base = ci * CHUNK
            chunk_sz = min(CHUNK, scan_limit - chunk_base)

            pct = 8 + (done / total_chunks) * 82  # progress 8-90%
            if done % 4 == 0 or done == total_chunks - 1:
                scanned_mb = (done + 1) * CHUNK / (1 << 20)
                total_mb = scan_limit / (1 << 20)
                cb(pct, f"Scanning VRAM: {scanned_mb:.0f} / {total_mb:.0f} MB "
                        f"(chunk 0x{chunk_base:X})")

            try:
                cv, ch = inpout.map_phys(vram_bar + chunk_base, chunk_sz)
                snap = read_buf(cv, chunk_sz)
                inpout.unmap_phys(cv, ch)
            except Exception as e:
                _elog(f"vram_scan_for_dma: chunk 0x{chunk_base:X} map failed: {e}")
                continue

            search_start = 0
            while True:
                idx = snap.find(inner_fp, search_start)
                if idx < 0:
                    break
                search_start = idx + 1

                buf_offset = chunk_base + idx - fp_dma_offset
                if buf_offset < 0:
                    _elog(f"vram_scan_for_dma: fp at BAR+0x{chunk_base + idx:X} "
                          f"gives negative offset, skipping")
                    continue

                _elog(f"vram_scan_for_dma: fp match at BAR+0x{chunk_base + idx:X}, "
                      f"DMA base=0x{buf_offset:X}")
                cb(pct, f"Fingerprint match at offset 0x{buf_offset:X} — validating...")

                try:
                    v, h = inpout.map_phys(vram_bar + buf_offset, 0x4000)
                    valid = _validate_metrics_at(
                        v, smu, PPSMC.TransferTableSmu2Dram)
                    inpout.unmap_phys(v, h)

                    if valid:
                        _elog(f"vram_scan_for_dma: VALIDATED at offset "
                              f"0x{buf_offset:X}")
                        _save_dma_cache(buf_offset, "vram-fp-scan")
                        cb(100, f"DMA buffer found at offset 0x{buf_offset:X} "
                                f"(VRAM fingerprint scan)")
                        return {
                            "offset": buf_offset,
                            "method": "vram-fp-scan",
                            "vram_size": vram_size,
                        }
                    _elog(f"vram_scan_for_dma: validation FAILED at "
                          f"offset 0x{buf_offset:X}")
                except Exception as e:
                    _elog(f"vram_scan_for_dma: validation map error at "
                          f"0x{buf_offset:X}: {e}")

        cb(90, "Fingerprint scan complete — no valid match found")
        _elog("vram_scan_for_dma: fingerprint phase found nothing")

    # --- Fallback: heuristic metrics scan -------------------------------------
    cb(91, "Falling back to heuristic metrics scan...")
    _elog("vram_scan_for_dma: heuristic fallback via _scan_for_driver_buffer")
    bar_limit = _detect_bar_size(inpout, vram_bar)
    found = _scan_for_driver_buffer(smu, inpout, vram_bar, bar_limit)
    if found[0] is not None:
        drv_off, v, h, phys = found
        inpout.unmap_phys(v, h)
        _save_dma_cache(drv_off, "vram-heuristic-scan")
        cb(100, f"DMA buffer found at offset 0x{drv_off:X} (heuristic scan)")
        _elog(f"vram_scan_for_dma: heuristic SUCCESS at 0x{drv_off:X}")
        return {
            "offset": drv_off,
            "method": "vram-heuristic-scan",
            "vram_size": vram_size,
        }

    cb(100, "VRAM scan failed: no DMA buffer found")
    _elog("vram_scan_for_dma: all methods exhausted — returning None")
    return None


def _discover_dma_buffer(smu, inpout, vram_bar, vbios_values=None,
                         gui_log=None):
    """Discover the Windows driver's existing DMA buffer.

    The SMU firmware locks the Driver DRAM address to whatever the Windows
    driver registered at boot (SetDriverDramAddr is a one-shot command).
    We cannot redirect DMA to our own buffer.  Instead we locate the
    driver's buffer by:

      0. Try the cached offset from a previous successful discovery.
      1. Try the default offset (0xFBCC000) — works on most systems.
      2. If VBIOS fingerprint available: scan BAR for the PP table
         fingerprint (exact byte match, no heuristics).
      3. Scan the BAR for a live metrics page (MetricsCounter incrementing).
      4. Fall back to the default offset without validation.

    On success the validated offset is persisted to .dma_offset_cache.json
    so subsequent launches can skip the scan.

    Args:
        vbios_values: Optional VbiosValues with pp_fingerprint for
                      deterministic DMA buffer discovery.
        gui_log:      Optional callable(str) for user-visible log messages.

    Returns (virt, handle, phys, dma_path_name) or raises on total failure.
    """
    def _glog(msg):
        _elog(msg)
        if gui_log:
            try:
                gui_log(msg)
            except Exception:
                pass

    bar_limit = _detect_bar_size(inpout, vram_bar)

    # --- Attempt 0: Cached offset from previous run ----------------------
    cached_offset = _load_dma_cache()
    if cached_offset is not None:
        _elog(f"_discover_dma: Attempt 0 (cached): offset=0x{cached_offset:X}")
        drv_phys = vram_bar + cached_offset
        try:
            virt, handle = inpout.map_phys(drv_phys, 0x4000)
            valid = _validate_metrics_at(virt, smu, PPSMC.TransferTableSmu2Dram)
            _elog(f"_discover_dma: cached validation -> "
                  f"{'OK' if valid else 'FAILED'}")
            if valid:
                _glog(f"DMA buffer: cached offset 0x{cached_offset:X} validated OK")
                return virt, handle, drv_phys, f"cached-0x{cached_offset:X}"
            inpout.unmap_phys(virt, handle)
            _glog(f"DMA buffer: cached offset 0x{cached_offset:X} is stale, "
                  f"rescanning...")
        except Exception as e:
            _elog(f"_discover_dma: cached map failed: {e}")
            _glog(f"DMA buffer: cached offset 0x{cached_offset:X} is stale, "
                  f"rescanning...")
    else:
        _glog("DMA buffer: no cached offset, discovering...")

    # --- Attempt 1: Validate the default driver buffer offset ------------
    _elog(f"_discover_dma: Attempt 1 (driver-default): "
          f"offset=0x{DRIVER_BUF_OFFSET_DEFAULT:X}")
    drv_phys = vram_bar + DRIVER_BUF_OFFSET_DEFAULT
    try:
        virt, handle = inpout.map_phys(drv_phys, 0x4000)
        valid = _validate_metrics_at(virt, smu, PPSMC.TransferTableSmu2Dram)
        _elog(f"_discover_dma: driver-default validation -> "
              f"{'OK' if valid else 'FAILED'}")
        if valid:
            _save_dma_cache(DRIVER_BUF_OFFSET_DEFAULT, "driver-default")
            _glog(f"DMA buffer found at offset 0x{DRIVER_BUF_OFFSET_DEFAULT:X} "
                  f"(driver-default) — cached for next startup")
            return virt, handle, drv_phys, "driver-default"
        inpout.unmap_phys(virt, handle)
    except Exception as e:
        _elog(f"_discover_dma: driver-default map failed: {e}")

    # --- Attempt 2: Fingerprint-based BAR scan ---------------------------
    inner_fp = (vbios_values.pp_inner_fingerprint
                if vbios_values is not None else b'')
    if inner_fp:
        fp_offset_in_pp = vbios_values.pp_inner_fp_dma_offset
        _elog(f"_discover_dma: Attempt 2 (fingerprint-scan): "
              f"{len(inner_fp)}-byte inner fingerprint, "
              f"dma_offset=0x{fp_offset_in_pp:X}")
        _glog("DMA buffer: fingerprint scan in progress...")
        found = _scan_for_driver_buffer_fp(
            smu, inpout, vram_bar, bar_limit,
            inner_fp, fp_offset_in_pp)
        if found[0] is not None:
            drv_off, virt, handle, phys = found
            _elog(f"_discover_dma: SUCCESS via fingerprint-scan "
                  f"at offset 0x{drv_off:X}")
            _save_dma_cache(drv_off, "fp-scan")
            _glog(f"DMA buffer found at offset 0x{drv_off:X} "
                  f"(fingerprint-scan) — cached for next startup")
            return virt, handle, phys, f"fp-scan-0x{drv_off:X}"
    else:
        _elog("_discover_dma: Attempt 2 (fingerprint-scan): "
              "skipped — no inner PPTable_t fingerprint available")

    # --- Attempt 3: Heuristic metrics scan --------------------------------
    _elog("_discover_dma: Attempt 3 (driver-scan): "
          "scanning BAR for existing driver buffer")
    _glog("DMA buffer: heuristic metrics scan in progress...")
    found = _scan_for_driver_buffer(smu, inpout, vram_bar, bar_limit)
    if found[0] is not None:
        drv_off, virt, handle, phys = found
        _elog(f"_discover_dma: SUCCESS via driver-scan at offset 0x{drv_off:X}")
        _save_dma_cache(drv_off, "driver-scan")
        _glog(f"DMA buffer found at offset 0x{drv_off:X} "
              f"(heuristic-scan) — cached for next startup")
        return virt, handle, phys, f"driver-scan-0x{drv_off:X}"

    # --- Attempt 4: Use default offset without validation ----------------
    _elog("_discover_dma: Attempt 4 (driver-unvalidated): "
          "using default offset without validation")
    drv_phys = vram_bar + DRIVER_BUF_OFFSET_DEFAULT
    virt, handle = inpout.map_phys(drv_phys, 0x4000)
    _glog(f"DMA buffer: using unvalidated default offset "
          f"0x{DRIVER_BUF_OFFSET_DEFAULT:X}")
    return virt, handle, drv_phys, "driver-unvalidated"


def _load_vbios_values():
    """Try to load VbiosValues from the on-disk VBIOS ROM.

    Returns VbiosValues or None if the file is missing or unparseable.
    """
    try:
        from src.io.vbios_storage import read_vbios_decoded
        from src.io.vbios_parser import parse_vbios_from_bytes
    except ImportError:
        _elog("_load_vbios_values: import failed — VBIOS modules unavailable")
        return None

    vbios_path = os.path.join(_project_root, "bios", "vbios.rom")
    if not os.path.isfile(vbios_path):
        _elog(f"_load_vbios_values: {vbios_path} not found")
        return None

    rom_bytes, _ = read_vbios_decoded(vbios_path)
    if rom_bytes is None:
        _elog("_load_vbios_values: read_vbios_decoded returned None")
        return None

    vals = parse_vbios_from_bytes(rom_bytes, rom_path=vbios_path)
    if vals is not None:
        _elog(f"_load_vbios_values: OK — outer fp "
              f"{len(vals.pp_fingerprint)}B, inner fp "
              f"{len(vals.pp_inner_fingerprint)}B, "
              f"baseclock_pp_offset=0x{vals.baseclock_pp_offset:X}")
    else:
        _elog("_load_vbios_values: parse_vbios_from_bytes returned None")
    return vals


def init_hardware(gui_log=None, skip_dma_discovery=False):
    """Initialize hardware drivers and map the driver DMA buffer.

    Locates the Windows driver's existing DMA buffer in VRAM (the SMU
    firmware locks the DMA address at boot and ignores later overrides).
    Falls back to a default offset if validation fails.

    Uses an in-memory cache so that after the first successful discovery
    (e.g. by ScanThread), subsequent calls from workers skip the costly
    BAR scan entirely.

    Args:
        gui_log: Optional callable(str) for user-visible log messages
                 (DMA discovery status).
        skip_dma_discovery: If True, skip the expensive DMA buffer scan
                 when no cached offset is available.  Returns hw dict
                 with virt/handle/phys = None and dma_path = 'none'.
                 In-memory and disk caches are still checked (instant).

    Returns a dict with keys:
        wr0, inpout, mmio, smu, vram_bar, virt, handle, phys, dma_path
    Caller must call cleanup_hardware() when done.
    """
    _elog("init_hardware: starting")
    wr0, inpout, mmio, smu, vram_bar = create_smu(verbose=False)

    # Fast path: reuse the offset discovered by a previous init_hardware
    # call in this process (avoids redundant 30+ second BAR scans).
    mem_offset, mem_path = _get_inmemory_dma()
    if mem_offset is not None:
        _elog(f"init_hardware: trying in-memory cached offset "
              f"0x{mem_offset:X} (path={mem_path})")
        drv_phys = vram_bar + mem_offset
        try:
            virt, handle = inpout.map_phys(drv_phys, 0x4000)
            dma_path = f"inmemory-{mem_path}"
            _elog(f"init_hardware: OK — dma_path={dma_path}")
            return {
                'wr0': wr0, 'inpout': inpout, 'mmio': mmio, 'smu': smu,
                'vram_bar': vram_bar, 'virt': virt, 'handle': handle,
                'phys': drv_phys, 'dma_path': dma_path,
            }
        except Exception as e:
            _elog(f"init_hardware: in-memory cached offset failed: {e}")

    if skip_dma_discovery:
        cached_offset = _load_dma_cache()
        if cached_offset is not None:
            _elog(f"init_hardware: skip_dma_discovery=True, trying disk cache "
                  f"offset=0x{cached_offset:X}")
            drv_phys = vram_bar + cached_offset
            try:
                virt, handle = inpout.map_phys(drv_phys, 0x4000)
                dma_path = f"disk-cached-0x{cached_offset:X}"
                _set_inmemory_dma(cached_offset, dma_path)
                _elog(f"init_hardware: OK — dma_path={dma_path}")
                return {
                    'wr0': wr0, 'inpout': inpout, 'mmio': mmio, 'smu': smu,
                    'vram_bar': vram_bar, 'virt': virt, 'handle': handle,
                    'phys': drv_phys, 'dma_path': dma_path,
                }
            except Exception as e:
                _elog(f"init_hardware: disk cache offset failed: {e}")

        _elog("init_hardware: skip_dma_discovery=True, no cached DMA offset "
              "— DMA unavailable")
        return {
            'wr0': wr0, 'inpout': inpout, 'mmio': mmio, 'smu': smu,
            'vram_bar': vram_bar, 'virt': None, 'handle': None,
            'phys': None, 'dma_path': 'none',
        }

    vbios_values = _load_vbios_values()
    virt, handle, phys, dma_path = _discover_dma_buffer(
        smu, inpout, vram_bar, vbios_values=vbios_values,
        gui_log=gui_log)
    _elog(f"init_hardware: OK — dma_path={dma_path}")

    _set_inmemory_dma(phys - vram_bar, dma_path)

    return {
        'wr0': wr0, 'inpout': inpout, 'mmio': mmio, 'smu': smu,
        'vram_bar': vram_bar, 'virt': virt, 'handle': handle, 'phys': phys,
        'dma_path': dma_path,
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


def detect_bar_size(inpout, vram_bar):
    """Public wrapper: probe BAR aperture via write-read at power-of-2 offsets."""
    return _detect_bar_size(inpout, vram_bar)


def detect_vram_size(inpout=None, vram_bar=None):
    """Public wrapper: read GPU VRAM size from registry, fallback to BAR probe."""
    return _detect_vram_size(inpout, vram_bar)


def read_vram_start(mmio):
    """Public wrapper: read gmc.vram_start from MMHUB FB_LOCATION_BASE register."""
    return _read_vram_start(mmio)


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


_OFFSET_SCAN_WINDOW = 64 * 1024 * 1024   # 64 MB default mapping window


# ---------------------------------------------------------------------------
# Safe physical memory read (SEH-protected via ReadProcessMemory)
# ---------------------------------------------------------------------------
# On some platforms, mapped physical addresses backed by device MMIO or
# unmapped space trigger a fatal access violation when read via plain
# ctypes/memoryview.  kernel32.ReadProcessMemory has built-in Structured
# Exception Handling — it returns FALSE instead of crashing on bad pages.

_k32_ReadProcessMemory = ctypes.windll.kernel32.ReadProcessMemory
_k32_ReadProcessMemory.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_k32_ReadProcessMemory.restype = ctypes.c_int
_k32_self = ctypes.windll.kernel32.GetCurrentProcess()


def _safe_read_mapped(virt, size):
    """Safely copy *size* bytes from virtual address *virt* into a local buffer.

    Uses kernel32.ReadProcessMemory on our own process.  If the mapped
    physical pages behind *virt* trigger an access violation (unmapped
    address space, device MMIO, etc.) the call returns short instead of
    killing the process.

    Returns (ctypes_buffer, bytes_read).  *bytes_read* may be less than
    *size* when the region is only partially readable.
    """
    buf = (ctypes.c_ubyte * size)()
    n = ctypes.c_size_t(0)
    _k32_ReadProcessMemory(_k32_self, virt, buf, size, ctypes.byref(n))
    return buf, n.value


def _offset_scan_chunk_list(chunk_indices, page_offset, fingerprint):
    """Scan a contiguous slice of chunk indices (worker entry point).

    Uses the per-process globals ``_mp_inpout`` and ``_mp_progress``
    set by ``_mp_init_worker``.
    """
    inpout = _mp_inpout
    prog = _mp_progress
    fp_len = len(fingerprint)
    fp_head = fingerprint[0:1]
    ci_set = set(chunk_indices)
    matches = []
    ci_pos = 0
    total = len(chunk_indices)

    while ci_pos < total:
        ci = chunk_indices[ci_pos]
        phys_base = ci * CHUNK_SIZE

        max_cw = _OFFSET_SCAN_WINDOW // CHUNK_SIZE
        cw = min(max_cw, total - ci_pos)
        actual = 1
        for k in range(1, cw):
            if (ci + k) in ci_set:
                actual = k + 1
            else:
                break
        cw = actual
        window = cw * CHUNK_SIZE

        virt = handle = None
        while window >= CHUNK_SIZE:
            try:
                virt, handle = inpout.map_phys(phys_base, window)
                break
            except (IOError, OSError):
                window //= 2
                cw = window // CHUNK_SIZE

        if virt is None:
            ci_pos += 1
            if prog is not None:
                with prog.get_lock():
                    prog.value += 1
            continue

        try:
            buf, safe_len = _safe_read_mapped(virt, window)
            if safe_len < window:
                _elog(f"_offset_scan_chunk_list: partial read at "
                      f"0x{phys_base:X} ({safe_len}/{window} bytes)")
            if safe_len >= page_offset + fp_len:
                mv = memoryview(buf)[:safe_len]
                probe = bytes(mv[page_offset::4096])
                pos = -1
                while True:
                    pos = probe.find(fp_head, pos + 1)
                    if pos == -1:
                        break
                    off = pos * 4096 + page_offset
                    if off + fp_len <= safe_len:
                        candidate = ctypes.string_at(
                            ctypes.addressof(buf) + off, fp_len)
                        if candidate == fingerprint:
                            matches.append(phys_base + off)
        finally:
            inpout.unmap_phys(virt, handle)

        ci_pos += cw
        if prog is not None:
            with prog.get_lock():
                prog.value += cw

    return matches


def _offset_scan(inpout, fingerprint, page_offset, max_gb=0,
                 progress_callback=None):
    """Fast parallel scan checking one page offset per 4KB page.

    Splits the scannable address range across multiple worker
    processes (one per CPU core, capped at 8).  Each worker maps
    large 64 MB windows, uses a strided memoryview to extract one
    probe byte per page in C, then bytes.find() to locate
    candidates.  Falls back to single-threaded on frozen builds
    or when ProcessPoolExecutor fails.

    Returns list of physical addresses where fingerprint matches.
    """
    cb = progress_callback or _noop_cb
    fp_len = len(fingerprint)

    ram_ranges = get_physical_ram_ranges()
    chunk_indices = _build_scannable_chunks(max_gb, ram_ranges)
    total_chunks = len(chunk_indices)

    if page_offset + fp_len > 4096:
        cb(100, "Offset scan: page_offset + fingerprint exceeds page size")
        return []

    t0 = time.perf_counter()
    num_workers = min(os.cpu_count() or 4, 8, max(1, total_chunks // 32))

    if num_workers < 2:
        global _mp_inpout, _mp_progress
        saved_inp, _mp_inpout = _mp_inpout, inpout
        saved_prog, _mp_progress = _mp_progress, None
        try:
            matches = _offset_scan_chunk_list(
                chunk_indices, page_offset, fingerprint)
        finally:
            _mp_inpout = saved_inp
            _mp_progress = saved_prog
    else:
        per_worker = (total_chunks + num_workers - 1) // num_workers
        slices = [chunk_indices[i * per_worker:(i + 1) * per_worker]
                  for i in range(num_workers)]
        slices = [s for s in slices if s]

        matches = []
        try:
            dll_path = inpout._dll._name
            shared_progress = mp.Value('i', 0)

            with ProcessPoolExecutor(
                max_workers=len(slices),
                initializer=_mp_init_worker,
                initargs=(dll_path, shared_progress),
            ) as pool:
                futures = [
                    pool.submit(_offset_scan_chunk_list, s,
                                page_offset, fingerprint)
                    for s in slices
                ]
                while not all(f.done() for f in futures):
                    done = shared_progress.value
                    if done > 0:
                        pct = done / total_chunks * 100
                        gb = done * CHUNK_SIZE / (1024 ** 3)
                        cb(pct, f"Offset scan: {pct:.0f}% ({gb:.1f} GB)")
                    time.sleep(0.10)

                for fut in futures:
                    matches.extend(fut.result())
        except Exception as exc:
            _elog(f"_offset_scan: ProcessPool failed ({exc}), "
                  f"falling back to single-process")
            saved_inp, _mp_inpout = _mp_inpout, inpout
            saved_prog, _mp_progress = _mp_progress, None
            try:
                matches = _offset_scan_chunk_list(
                    chunk_indices, page_offset, fingerprint)
            finally:
                _mp_inpout = saved_inp
                _mp_progress = saved_prog

    elapsed = time.perf_counter() - t0
    total_gb = total_chunks * CHUNK_SIZE / (1024 ** 3)
    cb(100, f"Offset scan done: {len(matches)} match(es) in {elapsed:.1f}s "
       f"[{total_gb:.1f} GB, offset 0x{page_offset:03X}]")
    _elog(f"_offset_scan: {len(matches)} match(es) in {elapsed:.1f}s "
          f"({total_gb:.1f} GB, offset 0x{page_offset:03X})")
    return matches


def scan_for_pptable(inpout, settings, scan_opts=None, progress_callback=None,
                     vbios_values=None):
    """Scan physical memory for the driver's cached PPTable.

    Uses an immutable PP-table header fingerprint (golden_pp_id, golden_revision,
    format_id, platform_caps, thermal_controller_type) when available from the
    VBIOS parse.  These bytes never change regardless of driver clock/power
    modifications, eliminating the failure mode where the driver alters clocks
    on startup.

    Falls back to the legacy clock-pattern scan when no fingerprint is available.

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

    # Decide scan strategy: fingerprint (preferred) vs legacy clock pattern
    use_fingerprint = (vbios_values is not None
                       and (vbios_values.pp_inner_fingerprint
                            or (vbios_values.pp_fingerprint
                                and vbios_values.fingerprint_to_clocks > 0)))

    if use_fingerprint:
        # The driver copies only the inner smc_pptable (PPTable_t) into its
        # kernel cache, stripping the outer smu_14_0_2_powerplay_table wrapper.
        # The outer fingerprint (golden_pp_id..thermal_controller_type) only
        # exists in full VBIOS ROM copies (e.g. our own Python process heap).
        # Prefer the inner fingerprint (MsgLimits/Power) which matches the
        # driver's actual kernel cache.
        inner_fp = vbios_values.pp_inner_fingerprint
        if inner_fp:
            search_patterns = [inner_fp]
            fp_to_clk_list = [vbios_values.inner_fp_to_clocks]
            cb(5, f"Using inner PP table fingerprint ({len(inner_fp)}B, "
                  f"clocks at {vbios_values.inner_fp_to_clocks})")
            cb(10, f"Inner FP: {inner_fp.hex(' ').upper()}")
        else:
            fingerprint = vbios_values.pp_fingerprint
            fp_to_clk = vbios_values.fingerprint_to_clocks
            search_patterns = [fingerprint]
            fp_to_clk_list = [fp_to_clk]
            cb(5, f"Using outer PP table fingerprint ({len(fingerprint)}B, "
                  f"clocks at +{fp_to_clk})")
            cb(10, f"Outer FP: {fingerprint[:20].hex(' ').upper()}"
                   f"{'...' if len(fingerprint) > 20 else ''}")
        cb(12, f"VBIOS power: PPT={vbios_values.power_ac}W "
               f"TDC_GFX={vbios_values.tdc_gfx}A TDC_SOC={vbios_values.tdc_soc}A")
    else:
        fp_to_clk = 0
        if vbios_values is not None:
            clock_pattern = vbios_values.clock_pattern()
            base_mhz = vbios_values.baseclock_ac
            cb(5, f"Using VBIOS clock pattern: base={vbios_values.baseclock_ac} "
                  f"game={vbios_values.gameclock_ac} boost={vbios_values.boostclock_ac} MHz")
        else:
            clock_pattern = CLOCK_PATTERN
            base_mhz = ORIG_BASECLOCK_AC
            cb(5, f"No VBIOS values — using hardcoded defaults: base={ORIG_BASECLOCK_AC} "
                  f"game={ORIG_GAMECLOCK_AC} boost={ORIG_BOOSTCLOCK_AC} MHz")

        patched_clock_pattern = struct.pack('<3H',
            base_mhz, settings._game_clock(), settings._boost_clock())
        search_patterns = [clock_pattern, patched_clock_pattern]
        cb(15, f"Search pattern (original): {clock_pattern.hex(' ').upper()}")
        cb(20, f"Search pattern (patched):  {patched_clock_pattern.hex(' ').upper()}")

    # ------------------------------------------------------------------
    # FAST PATH: offset-targeted scan at the known PPTable page offset.
    # Always tried first when a fingerprint is available.  Falls back to
    # the full byte-search scan only when no valid match is found.
    # ------------------------------------------------------------------
    _PPTABLE_PAGE_OFFSET = 0xF74
    _fp_offset = fp_to_clk_list[0] if use_fingerprint else 0
    _active_fp = (search_patterns[0] if (use_fingerprint and search_patterns)
                  else None)

    if use_fingerprint and _active_fp:
        fp_page_offset = (_PPTABLE_PAGE_OFFSET - _fp_offset) & 0xFFF
        cb(15, f"Offset scan: clock offset 0x{_PPTABLE_PAGE_OFFSET:03X}, "
              f"fingerprint offset 0x{fp_page_offset:03X}")
        cb(20, "Starting offset-targeted scan...")
        raw_addrs = _offset_scan(
            inpout, _active_fp, fp_page_offset,
            max_gb=scan_opts.max_gb,
            progress_callback=_map_progress(cb, 20, 85),
        )
        if raw_addrs:
            clock_addrs = [a + _fp_offset for a in raw_addrs]
            valid_addrs = []
            rejected_addrs = []
            match_details = []
            already_patched = []
            target_game = settings._game_clock()
            target_boost = settings._boost_clock()
            orig_game = vbios_values.gameclock_ac

            for addr in clock_addrs:
                ml_addr = addr + 28
                try:
                    ml = read_msglimits(inpout, ml_addr)
                except (IOError, OSError):
                    rejected_addrs.append(addr)
                    match_details.append({
                        'addr': addr, 'valid': False, 'already_patched': False,
                        'game_clock': 0, 'boost_clock': 0, 'msglimits': {},
                        'reject_reasons': ["Unreadable"],
                    })
                    continue

                page_base = addr & ~0xFFF
                page_off = addr - page_base
                v, h = inpout.map_phys(page_base, 4096)
                game_val = ctypes.c_ushort.from_address(v + page_off + 2).value
                boost_val = ctypes.c_ushort.from_address(v + page_off + 4).value
                inpout.unmap_phys(v, h)

                valid, reasons = is_valid_pptable(ml)
                if valid and vbios_values is not None:
                    ppt = ml['ppt0_ac']
                    exp_ppt = vbios_values.power_ac
                    ppt_lo, ppt_hi = int(exp_ppt * 0.5), int(exp_ppt * 1.5)
                    if not (ppt_lo <= ppt <= ppt_hi):
                        valid = False
                        reasons.append(
                            f"PPT={ppt}W outside VBIOS±50% [{ppt_lo}-{ppt_hi}]")

                is_patched = (game_val == target_game and boost_val == target_boost
                              and game_val != orig_game)
                if is_patched:
                    already_patched.append(addr)

                if valid:
                    valid_addrs.append(addr)
                else:
                    rejected_addrs.append(addr)
                match_details.append({
                    'addr': addr, 'valid': valid,
                    'already_patched': is_patched,
                    'game_clock': game_val, 'boost_clock': boost_val,
                    'msglimits': ml, 'reject_reasons': reasons,
                })

            if valid_addrs:
                cb(100, f"Offset scan: {len(valid_addrs)} valid PPTable(s) "
                   f"at offset 0x{_PPTABLE_PAGE_OFFSET:03X}")
                return ScanResult(
                    valid_addrs=valid_addrs,
                    already_patched_addrs=[a for a in already_patched
                                           if a in valid_addrs],
                    rejected_addrs=rejected_addrs,
                    all_clock_addrs=clock_addrs,
                    did_full_scan=True,
                    match_details=match_details,
                    fingerprint_validated=True,
                )
        cb(88, "Offset scan: no valid match — falling back to full scan")
        _elog("_offset_scan: fallback to full scan")

    # ------------------------------------------------------------------
    # FULL SCAN: byte-search across all physical memory
    # ------------------------------------------------------------------
    if use_fingerprint:
        inc_patterns = [p.translate(_INC_TABLE) for p in search_patterns]
        _saved_inner_inc = vbios_values.pp_inner_fingerprint.translate(_INC_TABLE)
        _saved_outer_inc = (vbios_values.pp_fingerprint.translate(_INC_TABLE)
                            if vbios_values.pp_fingerprint else b'')
        vbios_values.pp_inner_fingerprint = b''
        vbios_values.pp_fingerprint = b''
        search_patterns.clear()
        inner_fp = None
        gc.collect()

        cb(30, "Starting full physical memory scan (heap scrubbed)...")
        raw_results = scan_memory(
            inpout, [b'\x00'], scan_opts.max_gb,
            num_threads=scan_opts.num_threads,
            progress_callback=_map_progress(cb, 30, 90),
            _inc_patterns=inc_patterns,
        )

        vbios_values.pp_inner_fingerprint = _saved_inner_inc.translate(_DEC_TABLE)
        vbios_values.pp_fingerprint = (
            _saved_outer_inc.translate(_DEC_TABLE) if _saved_outer_inc else b'')
        del _saved_inner_inc, _saved_outer_inc
    else:
        cb(30, "Starting full physical memory scan...")
        raw_results = scan_memory(
            inpout, search_patterns, scan_opts.max_gb,
            num_threads=scan_opts.num_threads,
            progress_callback=_map_progress(cb, 30, 90),
        )
    did_full_scan = True

    if not raw_results:
        label = "fingerprint" if use_fingerprint else "clock pattern"
        cb(100, f"No PPTable {label} found in memory")
        return ScanResult([], [], [], [], did_full_scan, [],
                          error=f"PPTable {label} not found in memory")

    _elog(f"scan_for_pptable: {len(raw_results)} raw matches")

    if use_fingerprint:
        clock_results = [(addr + _fp_offset, pat) for addr, pat in raw_results]
    else:
        clock_results = [(addr + fp_to_clk, pat) for addr, pat in raw_results]

    target_game = settings._game_clock()
    target_boost = settings._boost_clock()
    orig_game = vbios_values.gameclock_ac if vbios_values else ORIG_GAMECLOCK_AC
    orig_boost = vbios_values.boostclock_ac if vbios_values else ORIG_BOOSTCLOCK_AC

    if use_fingerprint:
        already_patched = []
        for addr, _pat in clock_results:
            try:
                g = read_u16(inpout, addr, 2)
                b = read_u16(inpout, addr, 4)
                if g == target_game and b == target_boost and g != orig_game:
                    already_patched.append(addr)
            except (IOError, OSError):
                pass
    else:
        already_patched = [addr for addr, pat in clock_results
                           if pat == patched_clock_pattern]

    clock_addrs = [addr for addr, pat in clock_results]
    msglimits_addrs = [a + 28 for a in clock_addrs]

    valid_addrs = []
    rejected_addrs = []
    match_details = []

    cb(90, f"Validating {len(clock_addrs)} match(es)...")

    for i, addr in enumerate(clock_addrs):
        is_patched = addr in already_patched
        try:
            ml = read_msglimits(inpout, msglimits_addrs[i])
        except (IOError, OSError):
            rejected_addrs.append(addr)
            match_details.append({
                'addr': addr, 'valid': False, 'already_patched': is_patched,
                'game_clock': 0, 'boost_clock': 0, 'msglimits': {},
                'reject_reasons': ["Unreadable physical page"],
            })
            continue

        page_base = addr & ~0xFFF
        page_off = addr - page_base
        v, h = inpout.map_phys(page_base, 4096)
        game_val = ctypes.c_ushort.from_address(v + page_off + 2).value
        boost_val = ctypes.c_ushort.from_address(v + page_off + 4).value
        inpout.unmap_phys(v, h)

        valid, reasons = is_valid_pptable(ml)

        if valid and vbios_values is not None:
            ppt = ml['ppt0_ac']
            exp_ppt = vbios_values.power_ac
            ppt_lo, ppt_hi = int(exp_ppt * 0.5), int(exp_ppt * 1.5)
            if not (ppt_lo <= ppt <= ppt_hi):
                valid = False
                reasons.append(
                    f"PPT={ppt}W outside VBIOS±50% [{ppt_lo}-{ppt_hi}]"
                )

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

    if not valid_addrs:
        cb(100, "No valid PPTable copies found (all false positives)")
        return ScanResult([], already_patched, rejected_addrs,
                          clock_addrs, True, match_details,
                          error="All matches were false positives",
                          fingerprint_validated=use_fingerprint)

    # Page-offset consensus filter (full scan only)
    if len(valid_addrs) >= 2:
        offsets = Counter(a & 0xFFF for a in valid_addrs)
        dominant_offset, dominant_count = offsets.most_common(1)[0]
        if dominant_count >= 2:
            ghost_addrs = [a for a in valid_addrs if (a & 0xFFF) != dominant_offset]
            if ghost_addrs:
                cb(94, f"Page-offset filter: keeping offset 0x{dominant_offset:03X} "
                       f"({dominant_count} copies), rejecting {len(ghost_addrs)} ghost(s)")
                for ga in ghost_addrs:
                    rejected_addrs.append(ga)
                    for md in match_details:
                        if md['addr'] == ga:
                            md['valid'] = False
                            md['reject_reasons'] = [
                                f"Page offset 0x{ga & 0xFFF:03X} != consensus 0x{dominant_offset:03X}"
                            ]
                valid_addrs = [a for a in valid_addrs if (a & 0xFFF) == dominant_offset]

    # Stability re-read (full scan only — workers may leave stale pages)
    if valid_addrs:
        initial_clocks = {}
        for addr in valid_addrs:
            try:
                clk = read_clock_block(inpout, addr)
                if clk:
                    initial_clocks[addr] = (clk['gameclock_ac'], clk['boostclock_ac'])
            except (IOError, OSError):
                pass

        time.sleep(0.5)
        stable_addrs = []
        for addr in valid_addrs:
            try:
                clk = read_clock_block(inpout, addr)
                if clk is None:
                    rejected_addrs.append(addr)
                    continue
                if use_fingerprint:
                    prev = initial_clocks.get(addr)
                    if prev and (clk['gameclock_ac'], clk['boostclock_ac']) == prev:
                        stable_addrs.append(addr)
                    else:
                        cb(95, f"Stability check: 0x{addr:012X} drifted, rejecting")
                        rejected_addrs.append(addr)
                else:
                    expected_game = settings._game_clock() if addr in already_patched else orig_game
                    expected_boost = settings._boost_clock() if addr in already_patched else orig_boost
                    if (clk['gameclock_ac'] in (expected_game, settings._game_clock()) and
                            clk['boostclock_ac'] in (expected_boost, settings._boost_clock())):
                        stable_addrs.append(addr)
                    else:
                        cb(95, f"Stability check: 0x{addr:012X} drifted "
                               f"(game={clk['gameclock_ac']}, boost={clk['boostclock_ac']}), rejecting")
                        rejected_addrs.append(addr)
            except (IOError, OSError):
                rejected_addrs.append(addr)
        valid_addrs = stable_addrs

    # Deep probe (full scan only — offset scan targets known-good layout)
    if valid_addrs:
        probed = []
        for addr in valid_addrs:
            if probe_phys_readable(inpout, addr, _DEEP_PROBE_SIZE):
                probed.append(addr)
            else:
                cb(98, f"Deep probe: 0x{addr:012X} unreadable at depth "
                       f"{_DEEP_PROBE_SIZE} — rejecting")
                rejected_addrs.append(addr)
        valid_addrs = probed

    cb(100, f"Found {len(valid_addrs)} valid PPTable(s), "
       f"{len(rejected_addrs)} rejected"
       f"{' [fingerprint mode]' if use_fingerprint else ''}")

    return ScanResult(
        valid_addrs=valid_addrs,
        already_patched_addrs=[a for a in already_patched if a in valid_addrs],
        rejected_addrs=rejected_addrs,
        all_clock_addrs=clock_addrs,
        did_full_scan=did_full_scan,
        match_details=match_details,
        fingerprint_validated=use_fingerprint,
    )


def scan_for_od_table(inpout, pattern, pptable_addrs=None, scan_opts=None,
                     progress_callback=None):
    """Scan physical memory for OD table using SMU-extracted pattern.

    Uses tiered strategy: probe pptable addrs -> window scan -> full scan
    when pptable_addrs provided. Validates each match via validate_od_candidate().

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

    # Phase 1: Probe pptable addrs + window scan (when pptable_addrs provided)
    centers = list(set(pptable_addrs or []))

    if centers:
        cb(5, f"Probing {len(centers)} address(es) for OD pattern...")
        max_pat = len(pattern)
        for addr in centers:
            page_base = addr & ~0xFFF
            page_off = addr - page_base
            map_size = 8192 if page_off + max_pat <= 8192 else 12288
            try:
                virt, handle = inpout.map_phys(page_base, map_size)
            except (IOError, OSError):
                continue
            try:
                raw = read_buf(virt + page_off, max_pat)
                if raw[:max_pat] == pattern:
                    all_matches.append((addr, pattern))
            finally:
                inpout.unmap_phys(virt, handle)

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
            max_gb=scan_opts.max_gb,
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

    Validates original values before patching each copy.  If the value at an
    address doesn't match the expected original or the target, the copy is
    skipped (memory drifted / wrong address).

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

    orig_game = vbios_values.gameclock_ac if vbios_values else ORIG_GAMECLOCK_AC
    orig_boost = vbios_values.boostclock_ac if vbios_values else ORIG_BOOSTCLOCK_AC
    orig_ppt = vbios_values.power_ac if vbios_values else ORIG_POWER_AC

    fp_mode = getattr(scan_result, 'fingerprint_validated', False)

    reports = []
    patched_count = 0
    skipped_count = 0

    for i, addr in enumerate(valid_addrs):
        is_ap = addr in already_patched
        pct = (i / max(len(valid_addrs), 1)) * 70
        cb(pct, f"Patching copy {i+1}/{len(valid_addrs)} "
           f"at 0x{addr:012X}")

        ml_base = addr + 28
        game_clock = settings._game_clock()
        boost_clock = settings._boost_clock()
        power_ac = settings._power_ac()
        power_dc = settings._power_dc()
        tdc_gfx = settings._tdc_gfx()

        current_game = read_u16(inpout, addr, 2)
        if not fp_mode and current_game not in (orig_game, game_clock):
            cb(pct, f"SKIP 0x{addr:012X}: GameClock={current_game} != expected "
                    f"{orig_game} or {game_clock}, memory drifted")
            skipped_count += 1
            reports.append({'addr': addr, 'skipped': True, 'reason': 'drifted'})
            continue

        patches = []

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

        patched_count += 1
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

    cb(100, f"Patched {patched_count}/{len(valid_addrs)} PPTable(s) "
       f"({skipped_count} skipped), {len(extra_power)} extra MsgLimits")
    return reports


def apply_clocks_only(inpout, smu, scan_result, settings, vbios_values=None,
                      progress_callback=None):
    """Patch PPTable clocks in RAM, set SMU freq limits, then workload cycle.

    After patching GameClockAc/BoostClockAc in physical RAM, sends
    SetSoftMax/HardMax/SoftMin/HardMin frequency commands to the SMU so
    the firmware enforces the new clock range immediately.

    Validates via MsgLimits PPT values (stable between VBIOS and RAM, unlike
    clocks which the driver adjusts via silicon binning).
    Returns dict with results including 'patched_count' and 'skipped_count'.
    """
    cb = progress_callback or _noop_cb
    fp_mode = scan_result and getattr(scan_result, 'fingerprint_validated', False)
    results = {'patched_count': 0, 'skipped_count': 0, 'skipped_addrs': []}
    if scan_result and scan_result.valid_addrs:
        target_game = settings._game_clock()
        target_boost = settings._boost_clock()
        expected_ppt0 = vbios_values.power_ac if vbios_values else ORIG_POWER_AC

        for addr in scan_result.valid_addrs:
            if not fp_mode:
                ml_base = addr + 28
                current_ppt0 = read_u16(inpout, ml_base, ML_PPT0_AC)
                current_ppt1 = read_u16(inpout, ml_base, ML_PPT1_AC)
                if current_ppt0 != expected_ppt0 and current_ppt1 != 1200:
                    current_game = read_u16(inpout, addr, 2)
                    cb(0, f"SKIP 0x{addr:012X}: PPT0={current_ppt0}W (expected {expected_ppt0}), "
                          f"PPT1={current_ppt1}W (expected 1200), "
                          f"GameClock={current_game} — memory drifted")
                    results['skipped_count'] += 1
                    results['skipped_addrs'].append(addr)
                    continue
            patch_u16(inpout, addr, 2, target_game)
            patch_u16(inpout, addr, 4, target_boost)
            results['patched_count'] += 1
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
                         vbios_values=None, progress_callback=None):
    """Apply only MsgLimits patches (PPT, TDC, temps) and SetPptLimit.

    Validates original PPT value before patching to avoid corrupting drifted memory.
    """
    cb = progress_callback or _noop_cb
    if scan_opts is None:
        scan_opts = ScanOptions()
    fp_mode = scan_result and getattr(scan_result, 'fingerprint_validated', False)
    power_pattern = vbios_values.power_pattern() if vbios_values else POWER_PATTERN
    results = {'patched_count': 0, 'skipped_count': 0}
    orig_ppt = vbios_values.power_ac if vbios_values else ORIG_POWER_AC
    target_ppt = settings._power_ac()

    if scan_result and scan_result.valid_addrs:
        for addr in scan_result.valid_addrs:
            ml_base = addr + 28
            current_ppt = read_u16(inpout, ml_base, ML_PPT0_AC)
            if not fp_mode and current_ppt not in (orig_ppt, target_ppt):
                cb(0, f"SKIP MsgLimits 0x{addr:012X}: PPT={current_ppt} != expected "
                      f"{orig_ppt} or {target_ppt}, memory drifted")
                results['skipped_count'] += 1
                continue
            patch_u16(inpout, ml_base, ML_PPT0_AC, target_ppt)
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
            results['patched_count'] += 1
        power_results = scan_memory(inpout, power_pattern, scan_opts.max_gb,
                                    num_threads=scan_opts.num_threads)
        all_known = set(a + 28 for a in scan_result.all_clock_addrs)
        extra = [a for a, _ in power_results
                 if not any(abs(a - ap) < 256 for ap in all_known)]
        for addr in extra:
            patch_u16(inpout, addr, 0, target_ppt)
            patch_u16(inpout, addr, 2, settings._power_dc())
    resp, _ = smu.send_msg(PPSMC.SetPptLimit, target_ppt)
    results['ppt_limit'] = resp
    return results


def _apply_pp_field_groups(inpout, scan_result, field_values, field_offset_map, groups,
                           progress_callback=None):
    """Patch arbitrary PP fields by decoded offsets and ctypes type metadata."""
    cb = progress_callback or _noop_cb
    results = {
        'group_writes': 0,
        'patched_count': 0,
        'skipped_count': 0,
        'field_writes': 0,
    }
    if not scan_result or not scan_result.valid_addrs or not field_values or not field_offset_map:
        return results

    for addr in scan_result.valid_addrs:
        writes_this_addr = 0
        for key, value in field_values.items():
            meta = field_offset_map.get(key)
            if not meta:
                continue
            if groups is not None and meta.get("group") not in groups:
                continue
            off = meta.get("offset")
            typ = str(meta.get("type", "H"))
            if off is None:
                continue
            try:
                if typ in ("B", "b"):
                    _, verify = patch_u8(inpout, addr, int(off), int(value))
                elif typ in ("I", "L", "i", "l"):
                    _, verify = patch_u32(inpout, addr, int(off), int(value))
                else:
                    _, verify = patch_u16(inpout, addr, int(off), int(value))
                if int(verify) == int(value):
                    writes_this_addr += 1
                    results['field_writes'] += 1
            except Exception:
                cb(0, f"SKIP 0x{addr:012X}: patch failed for {key}")
                continue
        if writes_this_addr > 0:
            results['patched_count'] += 1
            results['group_writes'] += writes_this_addr
        else:
            results['skipped_count'] += 1
    return results


def patch_pp_single_field(inpout, scan_result, offset, value, type_code="H"):
    """Patch exactly one PP field (1/2/4 bytes) across all valid physical addresses.

    Returns {"ok": bool, "writes": int, "addrs": int}.
    """
    result = {"ok": False, "writes": 0, "addrs": 0}
    if not scan_result or not scan_result.valid_addrs:
        return result
    result["addrs"] = len(scan_result.valid_addrs)
    off = int(offset)
    val = int(value)
    typ = str(type_code)
    for addr in scan_result.valid_addrs:
        try:
            if typ in ("B", "b"):
                _, verify = patch_u8(inpout, addr, off, val)
            elif typ in ("I", "L", "i", "l"):
                _, verify = patch_u32(inpout, addr, off, val)
            else:
                _, verify = patch_u16(inpout, addr, off, val)
            if int(verify) == val:
                result["writes"] += 1
        except Exception:
            continue
    result["ok"] = result["writes"] > 0
    return result


def apply_pp_fan_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=None):
    """Patch expanded PP fan fields in RAM copies."""
    return _apply_pp_field_groups(
        inpout, scan_result, field_values, field_offset_map, {"fan"}, progress_callback=progress_callback
    )


def apply_pp_voltage_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=None):
    """Patch expanded PP voltage fields in RAM copies."""
    return _apply_pp_field_groups(
        inpout, scan_result, field_values, field_offset_map, {"voltage"}, progress_callback=progress_callback
    )


def apply_pp_freq_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=None):
    """Patch expanded PP frequency-table fields in RAM copies."""
    return _apply_pp_field_groups(
        inpout, scan_result, field_values, field_offset_map, {"freq"}, progress_callback=progress_callback
    )


def apply_pp_board_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=None):
    """Patch expanded PP board fields in RAM copies."""
    return _apply_pp_field_groups(
        inpout, scan_result, field_values, field_offset_map, {"board"}, progress_callback=progress_callback
    )


def apply_pp_custom_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=None):
    """Patch expanded PP groups beyond clocks/MsgLimits (fan, voltage, freq, board)."""
    cb = progress_callback or _noop_cb
    fan = apply_pp_fan_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=cb)
    voltage = apply_pp_voltage_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=cb)
    freq = apply_pp_freq_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=cb)
    board = apply_pp_board_fields(inpout, scan_result, field_values, field_offset_map, progress_callback=cb)
    return {
        "fan": fan,
        "voltage": voltage,
        "freq": freq,
        "board": board,
        "patched_count": fan["patched_count"] + voltage["patched_count"] + freq["patched_count"] + board["patched_count"],
        "field_writes": fan["field_writes"] + voltage["field_writes"] + freq["field_writes"] + board["field_writes"],
    }


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
        smu.hdp_flush()
        resp, _ = smu.send_msg(smu.transfer_write, TABLE_OVERDRIVE)
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


def apply_od_single_field(smu, virt, modify_fn):
    """Apply a single OD field: read table, modify in-place, write back.

    modify_fn(od) must set the field and the appropriate FeatureCtrlMask bit.
    Does not send SetSoftMin/Max, DisallowGfxOff, etc. — just the OD table.

    After TransferTableDram2Smu, runs a SetWorkloadMask cycle (PowerSave -> 3D Fullscreen)
    to trigger the SMU to re-evaluate DPM. Without this, the table is stored but the
    SMU does not apply the new OD values to runtime behavior.

    Returns:
        (ok: bool, error_detail: str | None)
        On success: (True, None)
        On failure: (False, decoded error message from SMU PARAM, or generic message)
    """
    od = read_od(smu, virt)
    if od is None:
        return False, "Could not read OD table from SMU"
    modify_fn(od)
    write_buf(virt, bytes(od))
    smu.hdp_flush()
    resp, param = smu.send_msg(smu.transfer_write, TABLE_OVERDRIVE)
    if resp != 1:
        return False, decode_od_fail(param)
    # Workload cycle to trigger DPM refresh — SMU picks up OD table changes
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)  # PowerSave
    time.sleep(0.3)
    smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)  # 3D Fullscreen
    time.sleep(0.3)
    return True, None


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


def query_smu_state(smu):
    """Query all readable SMU state for UI display.

    Returns dict with keys like smu_version, smu_drv_if, smu_freq_GFXCLK_min,
    smu_freq_GFXCLK_max, smu_ppt, smu_voltage, smu_feature_0..63, etc.
    """
    state = {}

    try:
        ver = smu.get_smu_version()
        state["smu_version"] = f"{ver[0]}.{ver[1]}.{ver[2]}.{ver[3]}"
        _elog(f"query_smu_state: version={state['smu_version']}")
    except Exception as e:
        state["smu_version"] = "Error"
        _elog(f"query_smu_state: version FAILED: {e}")

    try:
        drv_if = smu.get_driver_if_version()
        state["smu_drv_if"] = f"0x{drv_if:08X}"
    except Exception as e:
        state["smu_drv_if"] = "Error"
        _elog(f"query_smu_state: drv_if FAILED: {e}")

    for clk_id, clk_name in sorted(_CLK_NAMES.items()):
        try:
            fmin = smu.get_min_freq(clk_id)
            state[f"smu_freq_{clk_name}_min"] = fmin
        except Exception:
            pass
        try:
            fmax = smu.get_max_freq(clk_id)
            state[f"smu_freq_{clk_name}_max"] = fmax
        except Exception:
            pass
        try:
            dc_max = smu.get_dc_mode_max_freq(clk_id)
            state[f"smu_dcmax_{clk_name}"] = dc_max
        except Exception:
            pass

    try:
        state["smu_ppt"] = smu.get_ppt_limit()
    except Exception:
        pass

    try:
        voltage = smu.get_voltage()
        if voltage is not None:
            state["smu_voltage"] = voltage
    except Exception:
        pass

    try:
        features = smu.get_running_features()
        state["smu_features_raw"] = features
        for bit in range(64):
            state[f"smu_feature_{bit}"] = bool(features & (1 << bit))
        _elog(f"query_smu_state: features=0x{features:016X}")
    except Exception as e:
        _elog(f"query_smu_state: features FAILED: {e}")

    _elog(f"query_smu_state: returned {len(state)} keys")
    return state


def settings_to_od8_entries(
    settings,
    *,
    fan_curve_pwm=None,
    fan_curve_temp=None,
    fan_mode=None,
    fan_zero_rpm=None,
):
    """Translate OverclockSettings into OD8 index entries for D3DKMTEscape.

    Converts the existing OverclockSettings dataclass fields into a dict
    of ``{od8_index: (value, is_set)}`` entries suitable for
    ``build_v2_od_write()``.  Only fields with user-specified (non-default)
    values produce ``is_set=1`` entries; everything else is omitted so the
    driver leaves those settings unchanged.

    Fan curve data is accepted as separate keyword arguments because
    OverclockSettings doesn't carry fan curve fields — those are managed
    by the GUI's OD table editor.

    Confidence tags from OD8_RDNA4_FIELD_MAP are logged for entries that
    rely on inferred [I] indices so callers know which writes are less
    certain than the Frida-confirmed [F] ones.

    Args:
        settings:       OverclockSettings instance.
        fan_curve_pwm:  Optional list of up to 6 PWM values (0-255) for
                        fan curve points 0-5.  Length determines how many
                        points are set.
        fan_curve_temp: Optional list of up to 6 temperature values (°C)
                        for fan curve points 0-5.  Must match the length
                        of fan_curve_pwm if both are provided.
        fan_mode:       Optional fan mode (0=auto, 1=manual linear).
        fan_zero_rpm:   Optional fan zero-RPM enable (0 or 1).

    Returns:
        Dict mapping OD8 index (int) to ``(value: int, is_set: int)``
        tuples.  Suitable for passing directly to ``build_v2_od_write()``
        or ``D3DKMTClient.od_write()``.
    """
    entries = {}

    def _set(idx, value):
        mapping = OD8_RDNA4_FIELD_MAP.get(idx)
        confidence = mapping.confidence if mapping else "?"
        if confidence == "I":
            _elog(f"od8_translate: idx {idx} ({Od8Setting(idx).name}) = {value} "
                  f"[inferred — not Frida-confirmed]")
        entries[idx] = (int(value), 1)

    # ── Core OC settings (Frida-confirmed [F]) ──────────────────────────

    _set(Od8Setting.GFXCLK_FOFFSET, settings.offset)
    _set(Od8Setting.PPT, settings.od_ppt)

    # ── Inferred settings — only when user explicitly set them ──────────

    if settings.od_tdc != 0:
        _set(Od8Setting.TDC, settings.od_tdc)

    if settings.uclk_min > 0:
        _set(Od8Setting.UCLK_FMIN, settings.uclk_min)
    if settings.uclk_max > 0:
        _set(Od8Setting.UCLK_FMAX, settings.uclk_max)

    if settings.fclk_min > 0:
        _set(Od8Setting.FCLK_FMIN, settings.fclk_min)
    if settings.fclk_max > 0:
        _set(Od8Setting.FCLK_FMAX, settings.fclk_max)

    # ── Fan curve (Frida-confirmed [F] for points 0-4, [G] for pt 5) ───

    _FAN_PWM_INDICES = [
        Od8Setting.FAN_CURVE_PWM_0, Od8Setting.FAN_CURVE_PWM_1,
        Od8Setting.FAN_CURVE_PWM_2, Od8Setting.FAN_CURVE_PWM_3,
        Od8Setting.FAN_CURVE_PWM_4, Od8Setting.FAN_CURVE_PWM_5,
    ]
    _FAN_TEMP_INDICES = [
        Od8Setting.FAN_CURVE_TEMP_0, Od8Setting.FAN_CURVE_TEMP_1,
        Od8Setting.FAN_CURVE_TEMP_2, Od8Setting.FAN_CURVE_TEMP_3,
        Od8Setting.FAN_CURVE_TEMP_4, Od8Setting.FAN_CURVE_TEMP_5,
    ]

    if fan_curve_pwm is not None and fan_curve_temp is not None:
        if len(fan_curve_pwm) != len(fan_curve_temp):
            raise ValueError(
                f"fan_curve_pwm ({len(fan_curve_pwm)} pts) and "
                f"fan_curve_temp ({len(fan_curve_temp)} pts) must have "
                f"equal length")
        n = min(len(fan_curve_pwm), len(_FAN_PWM_INDICES))
        for i in range(n):
            _set(_FAN_PWM_INDICES[i], fan_curve_pwm[i])
            _set(_FAN_TEMP_INDICES[i], fan_curve_temp[i])
    elif fan_curve_pwm is not None or fan_curve_temp is not None:
        raise ValueError(
            "fan_curve_pwm and fan_curve_temp must both be provided "
            "or both be None")

    # ── Fan mode and zero-RPM (Frida-confirmed [F]) ─────────────────────

    if fan_mode is not None:
        _set(Od8Setting.FAN_MODE, fan_mode)

    if fan_zero_rpm is not None:
        _set(Od8Setting.FAN_ZERO_RPM_ENABLE, fan_zero_rpm)

    _elog(f"od8_translate: {len(entries)} entries from settings "
          f"(offset={settings.offset}, ppt={settings.od_ppt}, "
          f"tdc={settings.od_tdc}, uclk={settings.uclk_min}/{settings.uclk_max}, "
          f"fclk={settings.fclk_min}/{settings.fclk_max}, "
          f"fan_pts={'%d' % len(fan_curve_pwm) if fan_curve_pwm else 'none'})")

    return entries


def apply_od_via_escape(
    settings,
    *,
    fan_curve_pwm=None,
    fan_curve_temp=None,
    fan_mode=None,
    fan_zero_rpm=None,
):
    """Apply OD settings via the D3DKMTEscape path (no admin required).

    Uses the v2 0x00C000A1 OD8 write protocol — the same mechanism AMD
    Adrenalin uses at runtime.  Only OD table parameters are applied;
    SMU frequency floor commands (SetSoftMin, SetHardMin, etc.) and
    feature disables have no escape equivalent and are skipped.

    Args:
        settings:       OverclockSettings instance.
        fan_curve_pwm:  Optional list of up to 6 PWM values for fan curve.
        fan_curve_temp: Optional list of up to 6 temperature values for fan curve.
        fan_mode:       Optional fan mode (0=auto, 1=manual).
        fan_zero_rpm:   Optional fan zero-RPM enable (0 or 1).

    Returns:
        Dict with keys:
            'ok'              - bool, True if write succeeded
            'error'           - str or None, error description on failure
            'od_fail'         - OdFail enum value (0 = no error)
            'od_fail_name'    - human-readable OdFail name
            'status'          - raw response status int
            'baseline'        - dict of OD8 values before write
            'verified'        - dict of OD8 values after write
            'entries_sent'    - dict of entries that were sent
            'changed_indices' - list of OD8 indices whose values changed
    """
    result = {
        'ok': False,
        'error': None,
        'od_fail': OdFail.NO_ERROR,
        'od_fail_name': 'NO_ERROR',
        'status': -1,
        'baseline': {},
        'verified': {},
        'entries_sent': {},
        'changed_indices': [],
    }

    # ── Translate settings to OD8 entries ─────────────────────────────
    try:
        entries = settings_to_od8_entries(
            settings,
            fan_curve_pwm=fan_curve_pwm,
            fan_curve_temp=fan_curve_temp,
            fan_mode=fan_mode,
            fan_zero_rpm=fan_zero_rpm,
        )
    except (ValueError, TypeError) as e:
        result['error'] = f"settings translation failed: {e}"
        _elog(f"apply_od_via_escape: {result['error']}")
        return result

    result['entries_sent'] = {int(k): v for k, v in entries.items()}

    if not entries:
        result['error'] = "no OD8 entries to send (all settings are default)"
        _elog(f"apply_od_via_escape: {result['error']}")
        return result

    # ── Open D3DKMTClient ─────────────────────────────────────────────
    try:
        client = D3DKMTClient.open_amd_adapter()
    except (RuntimeError, D3DKMTError, OSError) as e:
        result['error'] = f"failed to open AMD adapter: {e}"
        _elog(f"apply_od_via_escape: {result['error']}")
        return result

    try:
        # ── Session queries (matches Adrenalin pre-write sequence) ────
        try:
            client.query_session()
            client.query_session()
        except (D3DKMTError, OSError) as e:
            _elog(f"apply_od_via_escape: session query failed (non-fatal): {e}")

        # ── Read baseline values (no-op write) ───────────────────────
        try:
            baseline = client.od_read_current_values()
            result['baseline'] = baseline
            _elog(f"apply_od_via_escape: baseline read OK, "
                  f"{len(baseline)} values")
        except (D3DKMTError, OSError) as e:
            _elog(f"apply_od_via_escape: baseline read failed (non-fatal): {e}")

        # ── Send OD8 entries ─────────────────────────────────────────
        try:
            resp = client.od_write(entries)
        except (D3DKMTError, OSError) as e:
            result['error'] = f"D3DKMTEscape od_write failed: {e}"
            _elog(f"apply_od_via_escape: {result['error']}")
            return result

        result['status'] = resp.status

        # ── Check OdFail code ────────────────────────────────────────
        try:
            fail_code = OdFail(resp.status)
        except ValueError:
            fail_code = OdFail.NO_ERROR if resp.status == 0 else None

        if fail_code is not None:
            result['od_fail'] = fail_code
            result['od_fail_name'] = fail_code.name
        else:
            result['od_fail'] = resp.status
            result['od_fail_name'] = f"UNKNOWN_{resp.status}"

        if resp.status != 0:
            result['error'] = (
                f"OD write rejected by driver: status={resp.status} "
                f"({result['od_fail_name']})")
            _elog(f"apply_od_via_escape: {result['error']}")
            return result

        _elog(f"apply_od_via_escape: od_write OK, "
              f"status={resp.status}, success_flag={resp.success_flag}")

        # ── Verify by re-reading current values ──────────────────────
        try:
            verified = client.od_read_current_values()
            result['verified'] = verified

            changed = []
            for idx in entries:
                old_val = result['baseline'].get(idx)
                new_val = verified.get(idx)
                if old_val != new_val:
                    changed.append(idx)
                    _elog(f"  idx {idx} ({Od8Setting(idx).name}): "
                          f"{old_val} -> {new_val}")
            result['changed_indices'] = changed
            _elog(f"apply_od_via_escape: verify OK, "
                  f"{len(changed)}/{len(entries)} indices changed")
        except (D3DKMTError, OSError) as e:
            _elog(f"apply_od_via_escape: verify read failed (non-fatal): {e}")

        result['ok'] = True

    finally:
        client.close()

    return result


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
        smu.hdp_flush()
        resp, _ = smu.send_msg(smu.transfer_write, TABLE_OVERDRIVE)
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
