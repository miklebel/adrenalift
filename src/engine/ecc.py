"""
ECC Counters -- SMU ECC Table Transfer (RDNA3/RDNA4)
====================================================

Retrieves ECC (Error Correcting Code) counters from the SMU via table transfer.
Uses the same mechanism as the Linux amdgpu driver's smu_get_ecc_info().

Uses the DRIVER path (TransferTableSmu2Dram, msg 0x12) and the driver's
pre-configured DRAM buffer — the same one used for OD / metrics tables.
The Tools path (0x52 / SetToolsDramAddr) does not work on RDNA4.

On consumer RDNA4 (RX 9070 series) the SMU fills the ECC table with a
debug/stub pattern (0x11111111, 0x22222222, ...) because there is no
ECC memory hardware to query.  detect_test_pattern() identifies this.

Source reference:
    linux/drivers/gpu/drm/amd/pm/swsmu/inc/pmfw_if/smu14_driver_if_v14_0.h
    linux/drivers/gpu/drm/amd/pm/swsmu/smu13/smu_v13_0_0_ppt.c
"""

import ctypes
import logging

from .od_table import TABLE_ECCINFO

_ecc_log = logging.getLogger("overclock.ecc")


NUM_UMC_CHANNELS = 24


# ---------------------------------------------------------------------------
# EccInfo_t / EccInfoTable_t (smu14_driver_if_v14_0.h)
# ---------------------------------------------------------------------------

class EccInfo_t(ctypes.Structure):
    """Per-channel ECC info (UMC)."""
    _pack_ = 1
    _fields_ = [
        ("mca_umc_status",  ctypes.c_uint64),
        ("mca_umc_addr",    ctypes.c_uint64),
        ("ce_count_lo_chip", ctypes.c_uint16),
        ("ce_count_hi_chip", ctypes.c_uint16),
        ("eccPadding",     ctypes.c_uint32),
    ]


class EccInfoTable_t(ctypes.Structure):
    """ECC info for all UMC channels (24 on RDNA3/RDNA4)."""
    _pack_ = 1
    _fields_ = [
        ("EccInfo", EccInfo_t * NUM_UMC_CHANNELS),
    ]


# ---------------------------------------------------------------------------
# Test pattern detection
# ---------------------------------------------------------------------------

def detect_test_pattern(raw_bytes):
    """
    Detect the SMU stub/debug fill pattern that consumer GPUs emit
    when they lack ECC hardware.

    Returns a descriptive string if a test pattern is detected, or None
    if the data looks like genuine ECC counters.
    """
    if raw_bytes is None or len(raw_bytes) < 48:
        return None

    entry_size = ctypes.sizeof(EccInfo_t)  # 24

    # Check 1: all entries identical (first N entries are byte-equal)
    first_entry = raw_bytes[:entry_size]
    entries_to_check = min(NUM_UMC_CHANNELS, len(raw_bytes) // entry_size)
    all_identical = all(
        raw_bytes[i * entry_size:(i + 1) * entry_size] == first_entry
        for i in range(1, entries_to_check)
    )

    if all_identical and entries_to_check >= 3:
        # Check 2: values look like repeating-nibble fill (0x11111111, 0x22222222, etc.)
        has_fill = False
        for off in range(0, min(entry_size, 16), 4):
            word = int.from_bytes(first_entry[off:off + 4], 'little')
            nibble = word & 0xF
            fill = nibble * 0x11111111
            if word == fill and word != 0:
                has_fill = True
                break

        if has_fill:
            return (
                "SMU returned a debug/stub fill pattern (all channels identical, "
                "repeating-nibble values like 0x11111111). This GPU does not have "
                "ECC memory — only Instinct/PRO datacenter variants do."
            )

        # Even without nibble fill, all-identical non-zero entries are suspicious
        if first_entry != b'\x00' * entry_size:
            return (
                "All UMC channels returned identical non-zero data. "
                "This is likely a firmware stub — consumer GPUs lack ECC VRAM."
            )

    return None


# ---------------------------------------------------------------------------
# Accumulator (mirrors Linux driver's += behaviour)
# ---------------------------------------------------------------------------

class EccAccumulator:
    """Accumulates read-and-clear ECC counters across multiple SMU transfers."""

    def __init__(self):
        self.ce_total = [0] * NUM_UMC_CHANNELS
        self.last_mca_status = [0] * NUM_UMC_CHANNELS
        self.last_mca_addr = [0] * NUM_UMC_CHANNELS
        self.reads = 0

    def add(self, ecc_table):
        """Fold a freshly-read EccInfoTable_t into the running totals."""
        if ecc_table is None:
            return
        self.reads += 1
        for i in range(NUM_UMC_CHANNELS):
            info = ecc_table.EccInfo[i]
            ce = info.ce_count_lo_chip | (info.ce_count_hi_chip << 16)
            self.ce_total[i] += ce
            if info.mca_umc_status != 0:
                self.last_mca_status[i] = info.mca_umc_status
                self.last_mca_addr[i] = info.mca_umc_addr

    def grand_total(self):
        return sum(self.ce_total)

    def per_channel_summary(self):
        """Return list of (channel, accumulated_ce, status_str) for non-zero channels."""
        result = []
        for i in range(NUM_UMC_CHANNELS):
            ce = self.ce_total[i]
            mca = self.last_mca_status[i]
            if ce > 0 or mca != 0:
                status = "CE" if ce > 0 else "—"
                if mca != 0:
                    status += f" MCA=0x{mca:X}"
                result.append((i, ce, status))
        return result


# ---------------------------------------------------------------------------
# Read ECC table from SMU via the DRIVER buffer
# ---------------------------------------------------------------------------

def read_ecc_info(smu, virt):
    """
    Transfer ECC table from SMU to DRAM and parse it.

    Uses TransferTableSmu2Dram (0x12) — the same driver-path transfer
    that read_od / read_metrics use.  The SMU writes TABLE_ECCINFO into
    the driver's pre-configured DRAM buffer, which is already mapped at
    `virt` by init_hardware().

    Args:
        smu:  SmuCmd instance.
        virt: Virtual address of the driver DMA buffer (from init_hardware).

    Returns:
        (EccInfoTable_t, raw_bytes) on success, (None, None) on failure.
    """
    tbl_size = ctypes.sizeof(EccInfoTable_t)

    try:
        resp, ret = smu.send_msg(0x12, TABLE_ECCINFO)  # TransferTableSmu2Dram
        _ecc_log.info("ECC: TransferTableSmu2Dram(TABLE_ECCINFO) resp=0x%X ret=0x%X",
                      resp, ret)
    except (RuntimeError, TimeoutError) as e:
        _ecc_log.warning("ECC: SMU transfer failed: %s", e)
        return None, None

    if resp != 1:
        _ecc_log.warning("ECC: SMU returned non-OK resp=0x%X", resp)
        return None, None

    try:
        buf = (ctypes.c_char * tbl_size)()
        ctypes.memmove(ctypes.byref(buf), virt, tbl_size)
        raw = bytes(buf)
        _ecc_log.info("ECC: first 64 bytes: %s", raw[:64].hex())
        ecc_table = EccInfoTable_t.from_buffer_copy(raw)
        return ecc_table, raw
    except Exception as e:
        _ecc_log.warning("ECC: buffer read/parse failed: %s", e)
        return None, None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_ecc_summary(ecc_table):
    """
    Format EccInfoTable_t as human-readable summary.

    Returns list of (channel, ce_count, status_str) tuples.
    """
    if ecc_table is None:
        return []

    result = []
    for i in range(NUM_UMC_CHANNELS):
        info = ecc_table.EccInfo[i]
        ce_lo = info.ce_count_lo_chip
        ce_hi = info.ce_count_hi_chip
        ce_count = ce_lo | (ce_hi << 16)
        if ce_count > 0 or info.mca_umc_status != 0:
            status = "CE" if ce_count > 0 else "—"
            if info.mca_umc_status != 0:
                status += f" MCA=0x{info.mca_umc_status:X}"
            result.append((i, ce_count, status))
    return result


def total_ce_count(ecc_table):
    """Sum correctable error count across all channels."""
    if ecc_table is None:
        return 0
    total = 0
    for i in range(NUM_UMC_CHANNELS):
        info = ecc_table.EccInfo[i]
        total += info.ce_count_lo_chip | (info.ce_count_hi_chip << 16)
    return total


def format_raw_hex(raw_bytes, columns=16):
    """Format raw bytes as a compact hex dump string (for log/diagnostics)."""
    if raw_bytes is None:
        return "(no data)"
    lines = []
    for off in range(0, len(raw_bytes), columns):
        chunk = raw_bytes[off:off + columns]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        lines.append(f"  {off:04X}: {hex_part}")
    return "\n".join(lines)
