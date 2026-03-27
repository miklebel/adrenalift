"""
Background worker threads for Adrenalift.

Each worker runs a blocking hardware operation on a QThread so the
UI stays responsive.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from collections import Counter

from PySide6.QtCore import QThread, Signal

from src.engine.overclock_engine import (
    OverclockSettings,
    ScanOptions,
    ScanResult,
    _set_inmemory_dma,
    cleanup_hardware,
    init_hardware,
    is_valid_pptable,
    query_smu_state,
    read_metrics,
    read_od,
    read_pptable_at_addr,
    read_smu_metrics_full,
    read_smu_table_raw,
    read_f32,
    read_i16,
    read_i32,
    read_u8,
    read_u16,
    read_u32,
    scan_for_pptable,
    vram_scan_for_dma,
    read_pfe_settings,
    patch_features_to_run,
    patch_debug_overrides,
    patch_features_tools_path,
    patch_debug_tools_path,
    check_od_mem_timing_caps,
    decode_debug_overrides,
)
from src.io.vbios_parser import parse_vbios_or_defaults

# ---------------------------------------------------------------------------
# Logging helpers -- reuse the "overclock" logger configured by main.py
# ---------------------------------------------------------------------------

_file_logger = logging.getLogger("overclock")


def _log_to_file(msg: str):
    try:
        _file_logger.info(msg)
        for h in _file_logger.handlers:
            h.flush()
    except Exception:
        pass


def _log_exception_to_file(context: str = ""):
    try:
        tb = traceback.format_exc()
        _file_logger.error(f"EXCEPTION ({context}):\n{tb}")
        for h in _file_logger.handlers:
            h.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Apply Worker (runs blocking apply in background to avoid UI freeze)
# ---------------------------------------------------------------------------


class ApplyWorker(QThread):
    """Background worker for apply operations. Prevents UI freeze during scan_memory."""
    finished_signal = Signal(str, object)  # (action_name, error_msg | None)

    def __init__(self, action_name: str, apply_fn, parent=None):
        super().__init__(parent)
        self.action_name = action_name
        self.apply_fn = apply_fn

    def run(self):
        err = None
        hw = None
        _log_to_file(f"ApplyWorker[{self.action_name}]: starting")
        try:
            hw = init_hardware(skip_dma_discovery=True)
            _log_to_file(f"ApplyWorker[{self.action_name}]: hardware initialized, "
                         f"dma_path={hw['dma_path']}")
            result = self.apply_fn(hw)
            if isinstance(result, tuple) and len(result) == 2 and result[0] is False:
                err = result[1]
                _log_to_file(f"ApplyWorker[{self.action_name}]: apply_fn reported failure: {err}")
            else:
                _log_to_file(f"ApplyWorker[{self.action_name}]: apply_fn completed OK")
        except Exception as e:
            err = str(e)
            _log_exception_to_file(f"ApplyWorker[{self.action_name}]")
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    _log_exception_to_file(f"ApplyWorker[{self.action_name}] cleanup")
        self.finished_signal.emit(self.action_name, err)


class EscapeWorker(QThread):
    """Background worker for D3DKMTEscape operations (no admin/hardware needed)."""
    result_signal = Signal(str, object)  # (action_name, result_dict)

    def __init__(self, action_name: str, fn, parent=None):
        super().__init__(parent)
        self.action_name = action_name
        self.fn = fn

    def run(self):
        _log_to_file(f"EscapeWorker[{self.action_name}]: starting")
        try:
            result = self.fn()
            _log_to_file(f"EscapeWorker[{self.action_name}]: completed OK")
            self.result_signal.emit(self.action_name, result)
        except Exception as e:
            _log_exception_to_file(f"EscapeWorker[{self.action_name}]")
            self.result_signal.emit(self.action_name, {"error": str(e)})


class RegistryPatchWorker(QThread):
    """Background worker for registry read/apply/restore operations."""
    finished_signal = Signal(str, object, object)  # (action, result_or_error, is_error)

    def __init__(self, action: str, fn, parent=None):
        super().__init__(parent)
        self.action = action
        self.fn = fn

    def run(self):
        err = None
        result = None
        _log_to_file(f"RegistryPatchWorker[{self.action}]: starting")
        try:
            result = self.fn()
            _log_to_file(f"RegistryPatchWorker[{self.action}]: completed OK")
        except Exception as e:
            err = str(e)
            _log_exception_to_file(f"RegistryPatchWorker[{self.action}]")
        self.finished_signal.emit(self.action, err if err else result, bool(err))


# ---------------------------------------------------------------------------
# Memory Tab Refresh Worker
# ---------------------------------------------------------------------------


class MemoryRefreshWorker(QThread):
    """Background worker to read PPTable data from RAM at each valid_addr."""

    results_signal = Signal(list)  # list of (addr, status, data_dict | None)

    def __init__(self, valid_addrs: list, parent=None):
        super().__init__(parent)
        self.valid_addrs = valid_addrs

    def run(self):
        if not self.valid_addrs:
            self.results_signal.emit([])
            return
        hw = None
        _log_to_file(f"MemoryRefreshWorker: starting ({len(self.valid_addrs)} addrs)")
        try:
            hw = init_hardware(skip_dma_discovery=True)
            _log_to_file(f"MemoryRefreshWorker: init OK, dma_path={hw['dma_path']}")
            inpout = hw["inpout"]
        except Exception:
            _log_exception_to_file("MemoryRefreshWorker init_hardware")
            self.results_signal.emit([
                (addr, "Error", None) for addr in self.valid_addrs
            ])
            return

        results = []
        try:
            for addr in self.valid_addrs:
                try:
                    data = read_pptable_at_addr(inpout, addr)
                    if data is None:
                        results.append((addr, "Unavailable", None))
                    else:
                        valid, _ = is_valid_pptable(data)
                        if valid:
                            results.append((addr, "OK", data))
                        else:
                            results.append((addr, "Invalid data", data))
                except Exception:
                    _log_exception_to_file(f"MemoryRefreshWorker addr=0x{addr:012X}")
                    results.append((addr, "Error", None))
        finally:
            cleanup_hardware(hw)
        _log_to_file(f"MemoryRefreshWorker: done, {len(results)} results")
        self.results_signal.emit(results)


# ---------------------------------------------------------------------------
# Detailed Tab Refresh Worker
# ---------------------------------------------------------------------------


class MetricsRefreshWorker(QThread):
    """Background worker to read full SmuMetrics_t for the Tables sub-tab."""

    results_signal = Signal(object)  # dict of metrics, or {"error": str}

    def run(self):
        hw = None
        try:
            hw = init_hardware(skip_dma_discovery=True)
            _log_to_file(f"MetricsRefreshWorker: init OK, dma_path={hw['dma_path']}")
            if hw.get("virt") is None:
                self.results_signal.emit({"error": "DMA buffer not available — run DRAM Scan first"})
                return
            _m, d = read_smu_metrics_full(hw["smu"], hw["virt"])
            if d:
                self.results_signal.emit(d)
            else:
                self.results_signal.emit({"error": "Metrics read returned empty data"})
        except Exception as e:
            _log_exception_to_file("MetricsRefreshWorker")
            self.results_signal.emit({"error": str(e)})
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    pass


class SmuTableReadWorker(QThread):
    """Background worker to read a raw SMU table (PPTable, DriverInfo, etc.)."""

    results_signal = Signal(str, object)  # (table_name, raw_bytes or {"error": str})

    def __init__(self, table_name, table_id, read_size=8192, parent=None):
        super().__init__(parent)
        self.table_name = table_name
        self.table_id = table_id
        self.read_size = read_size

    def run(self):
        hw = None
        try:
            hw = init_hardware(skip_dma_discovery=True)
            _log_to_file(f"SmuTableReadWorker: init OK, dma_path={hw['dma_path']}")
            if hw.get("virt") is None:
                self.results_signal.emit(
                    self.table_name, {"error": "DMA buffer not available — run DRAM Scan first"})
                return
            resp, raw = read_smu_table_raw(
                hw["smu"], hw["virt"], self.table_id, self.read_size
            )
            if raw is not None:
                self.results_signal.emit(self.table_name, raw)
            else:
                self.results_signal.emit(
                    self.table_name, {"error": f"SMU returned no data (resp={resp})"}
                )
        except Exception as e:
            _log_exception_to_file(f"SmuTableReadWorker[{self.table_name}]")
            self.results_signal.emit(self.table_name, {"error": str(e)})
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    pass


class DetailedRefreshWorker(QThread):
    """Background worker to read Live RAM (PPTable) and Live SMU (OD + metrics + full state) for Detailed tab."""

    results_signal = Signal(object, object, object, object)  # ram_data, od_table, metrics, smu_state
    log_signal = Signal(str)

    def __init__(self, valid_addrs: list, pp_ram_offset_map: dict[str, dict] | None = None, parent=None):
        super().__init__(parent)
        self.valid_addrs = valid_addrs
        self.pp_ram_offset_map = pp_ram_offset_map or {}

    def run(self):
        ram_data = None
        od_table = None
        metrics = None
        smu_state = None
        hw = None
        _log_to_file("DetailedRefreshWorker: starting")
        try:
            hw = init_hardware(gui_log=self.log_signal.emit, skip_dma_discovery=True)
            _log_to_file(f"DetailedRefreshWorker: init OK, dma_path={hw['dma_path']}")
            inpout = hw["inpout"]
            smu = hw["smu"]
            virt = hw["virt"]
            dma_ok = virt is not None

            if self.valid_addrs:
                try:
                    data = read_pptable_at_addr(inpout, self.valid_addrs[0])
                    if data is not None:
                        valid, _ = is_valid_pptable(data)
                        if valid:
                            ram_data = data
                except Exception:
                    _log_exception_to_file("DetailedRefreshWorker read_pptable")

                if self.pp_ram_offset_map:
                    if ram_data is None:
                        ram_data = {}
                    base = self.valid_addrs[0]
                    _type_reader = {
                        "B": read_u8, "b": read_u8,
                        "H": read_u16, "h": read_i16,
                        "I": read_u32, "L": read_u32, "i": read_i32, "l": read_i32,
                        "f": read_f32,
                    }
                    for key, meta in self.pp_ram_offset_map.items():
                        off = meta.get("offset")
                        if off is None:
                            continue
                        try:
                            reader = _type_reader.get(meta.get("type", "H"), read_u16)
                            ram_data[key] = reader(inpout, base, int(off))
                        except Exception:
                            continue

            if dma_ok:
                try:
                    od_table = read_od(smu, virt)
                    _log_to_file(f"DetailedRefreshWorker: read_od={'OK' if od_table else 'None'}")
                except Exception:
                    _log_exception_to_file("DetailedRefreshWorker read_od")

                try:
                    metrics = read_metrics(smu, virt)
                    _log_to_file(f"DetailedRefreshWorker: read_metrics={metrics}")
                except Exception:
                    _log_exception_to_file("DetailedRefreshWorker read_metrics")
            else:
                _log_to_file("DetailedRefreshWorker: DMA unavailable, skipping OD/metrics reads")

            try:
                smu_state = query_smu_state(smu)
                _log_to_file(f"DetailedRefreshWorker: query_smu_state returned {len(smu_state)} keys")
            except Exception:
                _log_exception_to_file("DetailedRefreshWorker query_smu_state")
        except Exception:
            _log_exception_to_file("DetailedRefreshWorker outer (init_hardware?)")
        finally:
            if hw:
                cleanup_hardware(hw)
        _log_to_file(f"DetailedRefreshWorker: done, smu_state={'None' if smu_state is None else f'{len(smu_state)} keys'}")
        self.results_signal.emit(ram_data, od_table, metrics, smu_state)


# ---------------------------------------------------------------------------
# Background Scan Thread
# ---------------------------------------------------------------------------


class ScanThread(QThread):
    """Background scan for PPTable addresses in physical memory."""

    progress_signal = Signal(float, str)  # pct, msg
    finished_signal = Signal(object)  # ScanResult or None on error

    def __init__(self, get_vbios_fn, *, merge_with_addrs=None, default_vbios_path=None,
                 num_threads=0, parent=None):
        super().__init__(parent)
        self.get_vbios_fn = get_vbios_fn
        self.merge_with_addrs = list(merge_with_addrs or [])
        self.default_vbios_path = default_vbios_path
        self.num_threads = num_threads

    def run(self):
        _log_to_file("ScanThread: starting")
        vbios_values = self.get_vbios_fn()
        if vbios_values is None and self.default_vbios_path:
            vbios_values = parse_vbios_or_defaults(self.default_vbios_path)

        hw = None
        try:
            hw = init_hardware(skip_dma_discovery=True)
            _log_to_file(f"ScanThread: init OK, dma_path={hw['dma_path']}")
            inpout = hw["inpout"]
        except Exception as e:
            _log_exception_to_file("ScanThread init_hardware")
            self.finished_signal.emit(
                ScanResult([], [], [], [], False, [], error=f"Hardware init failed: {e}")
            )
            return

        dma_ok = hw.get("virt") is not None

        try:
            settings = OverclockSettings(
                game_clock=vbios_values.gameclock_ac,
                boost_clock=vbios_values.boostclock_ac,
                clock=vbios_values.gameclock_ac,
            )
            scan_opts = ScanOptions()
            scan_opts.num_threads = self.num_threads

            def on_progress(pct: float, msg: str):
                self.progress_signal.emit(pct, msg)

            if not dma_ok:
                on_progress(2, "DMA buffer not found — run DRAM Scan to enable "
                               "OD/metrics readback and save the address for future sessions")

            result = scan_for_pptable(
                inpout,
                settings,
                scan_opts=scan_opts,
                progress_callback=on_progress,
                vbios_values=vbios_values,
            )
            if result and self.merge_with_addrs:
                if result.valid_addrs:
                    offsets = Counter(a & 0xFFF for a in result.valid_addrs)
                    dominant_offset, _ = offsets.most_common(1)[0]
                    filtered_old = [a for a in self.merge_with_addrs
                                    if (a & 0xFFF) == dominant_offset]
                else:
                    filtered_old = list(self.merge_with_addrs)
                merged = sorted(set(result.valid_addrs) | set(filtered_old))
                result = ScanResult(
                    valid_addrs=merged,
                    already_patched_addrs=result.already_patched_addrs,
                    rejected_addrs=result.rejected_addrs,
                    all_clock_addrs=result.all_clock_addrs,
                    did_full_scan=result.did_full_scan,
                    match_details=result.match_details,
                    od_table=result.od_table,
                )
            if hw and result and dma_ok:
                try:
                    od = read_od(hw["smu"], hw["virt"])
                    if od is not None:
                        result.od_table = od
                except Exception:
                    pass
            _log_to_file("ScanThread: completed OK")
            self.finished_signal.emit(result)
        except Exception as e:
            _log_exception_to_file("ScanThread")
            self.finished_signal.emit(
                ScanResult([], [], [], [], False, [], error=f"Scan failed: {e}")
            )
        finally:
            if hw:
                cleanup_hardware(hw)


class VramDmaScanWorker(QThread):
    """Background full-VRAM scan for the DMA buffer on ReBAR systems."""

    progress_signal = Signal(float, str)   # pct 0-100, message
    finished_signal = Signal(object)       # result dict or None
    log_signal = Signal(str)               # GUI log messages

    def __init__(self, get_vbios_fn, *, default_vbios_path=None, parent=None):
        super().__init__(parent)
        self.get_vbios_fn = get_vbios_fn
        self.default_vbios_path = default_vbios_path

    def run(self):
        _log_to_file("VramDmaScanWorker: starting")
        vbios_values = self.get_vbios_fn()
        if vbios_values is None and self.default_vbios_path:
            vbios_values = parse_vbios_or_defaults(self.default_vbios_path)

        hw = None
        try:
            hw = init_hardware(gui_log=self.log_signal.emit, skip_dma_discovery=True)
            _log_to_file(f"VramDmaScanWorker: init OK, vram_bar=0x{hw['vram_bar']:X}")
        except Exception as e:
            _log_exception_to_file("VramDmaScanWorker init_hardware")
            self.log_signal.emit(f"Hardware init failed: {e}")
            self.finished_signal.emit(None)
            return

        try:
            def on_progress(pct: float, msg: str):
                self.progress_signal.emit(pct, msg)

            result = vram_scan_for_dma(
                hw["smu"],
                hw["inpout"],
                hw["vram_bar"],
                vbios_values=vbios_values,
                progress_callback=on_progress,
            )
            if result:
                _log_to_file(f"VramDmaScanWorker: found offset=0x{result['offset']:X} "
                             f"method={result['method']}")
                _set_inmemory_dma(result['offset'], result['method'])
                self.log_signal.emit(
                    f"VRAM scan found DMA buffer at offset 0x{result['offset']:X} "
                    f"(method: {result['method']})")
            else:
                _log_to_file("VramDmaScanWorker: no DMA buffer found")
                self.log_signal.emit("VRAM scan completed — DMA buffer not found")
            self.finished_signal.emit(result)
        except Exception as e:
            _log_exception_to_file("VramDmaScanWorker")
            self.log_signal.emit(f"VRAM scan failed: {e}")
            self.finished_signal.emit(None)
        finally:
            if hw:
                cleanup_hardware(hw)


# ---------------------------------------------------------------------------
# PFE Settings Worker (PPTable PFE_Settings_t read / patch)
# ---------------------------------------------------------------------------


class PfeWorker(QThread):
    """Background worker for PFE_Settings_t operations (read, patch, cap check)."""

    result_signal = Signal(str, object)  # (action, result_dict)

    def __init__(self, action: str, parent=None, **kwargs):
        super().__init__(parent)
        self.action = action
        self.kwargs = kwargs

    def run(self):
        hw = None
        _log_to_file(f"PfeWorker[{self.action}]: starting")
        try:
            hw = init_hardware(skip_dma_discovery=True)
            smu = hw["smu"]
            virt = hw["virt"]
            _log_to_file(f"PfeWorker[{self.action}]: hw init OK")

            if virt is None:
                self.result_signal.emit(self.action,
                    {"error": "DMA buffer not available — run DRAM Scan first"})
                return

            if self.action == "read_pfe":
                result = read_pfe_settings(smu, virt)
                if result is None:
                    result = {"error": "Failed to read PFE_Settings_t"}
                else:
                    result['debug_overrides_decoded'] = decode_debug_overrides(
                        result['debug_overrides'])

            elif self.action == "patch_features":
                extra_bits = self.kwargs.get("extra_bits", [])
                result = patch_features_to_run(smu, virt, extra_bits)

            elif self.action == "patch_debug":
                flags = self.kwargs.get("flags", 0)
                result = patch_debug_overrides(smu, virt, flags)

            elif self.action == "patch_features_tools":
                extra_bits = self.kwargs.get("extra_bits", [])
                mmio = hw["mmio"]
                phys = hw["phys"]
                vram_bar = hw["vram_bar"]
                result = patch_features_tools_path(
                    smu, virt, extra_bits, mmio, phys, vram_bar)

            elif self.action == "patch_debug_tools":
                flags = self.kwargs.get("flags", 0)
                mmio = hw["mmio"]
                phys = hw["phys"]
                vram_bar = hw["vram_bar"]
                result = patch_debug_tools_path(
                    smu, virt, flags, mmio, phys, vram_bar)

            elif self.action == "check_od_caps":
                result = check_od_mem_timing_caps(smu, virt)

            else:
                result = {"error": f"Unknown action: {self.action}"}

            _log_to_file(f"PfeWorker[{self.action}]: done")
            self.result_signal.emit(self.action, result)
        except Exception as e:
            _log_exception_to_file(f"PfeWorker[{self.action}]")
            self.result_signal.emit(self.action, {"error": str(e)})
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    _log_exception_to_file(f"PfeWorker[{self.action}] cleanup")
