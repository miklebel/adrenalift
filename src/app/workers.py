"""
Background worker threads for the RDNA4 Overclock GUI.

Each worker runs a blocking hardware operation on a QThread so the
UI stays responsive.
"""

from __future__ import annotations

import gzip
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
    cleanup_hardware,
    detect_bar_size,
    init_hardware,
    is_valid_pptable,
    query_smu_state,
    read_buf,
    read_metrics,
    read_od,
    read_pptable_at_addr,
    read_smu_metrics_full,
    read_smu_table_raw,
    read_u16,
    read_vram_start,
    scan_for_pptable,
    vram_scan_for_dma,
)
from src.engine.od_table import TABLE_SMU_METRICS
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
            hw = init_hardware()
            _log_to_file(f"ApplyWorker[{self.action_name}]: hardware initialized, "
                         f"dma_path={hw['dma_path']}")
            result = self.apply_fn(hw)
            if isinstance(result, tuple) and len(result) == 2 and result[0] is False:
                err = result[1]  # e.g. OD reject: (False, "Unsupported feature")
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
            hw = init_hardware()
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
            hw = init_hardware()
            _log_to_file(f"MetricsRefreshWorker: init OK, dma_path={hw['dma_path']}")
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
            hw = init_hardware()
            _log_to_file(f"SmuTableReadWorker: init OK, dma_path={hw['dma_path']}")
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
            hw = init_hardware(gui_log=self.log_signal.emit)
            _log_to_file(f"DetailedRefreshWorker: init OK, dma_path={hw['dma_path']}")
            inpout = hw["inpout"]
            smu = hw["smu"]
            virt = hw["virt"]

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
                    for key, meta in self.pp_ram_offset_map.items():
                        off = meta.get("offset")
                        if off is None:
                            continue
                        try:
                            ram_data[key] = read_u16(inpout, base, int(off))
                        except Exception:
                            continue

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

    def __init__(self, get_vbios_fn, *, merge_with_addrs=None, default_vbios_path=None, num_threads=0, parent=None):
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
            hw = init_hardware()
            _log_to_file(f"ScanThread: init OK, dma_path={hw['dma_path']}")
            inpout = hw["inpout"]
        except Exception as e:
            _log_exception_to_file("ScanThread init_hardware")
            self.finished_signal.emit(
                ScanResult([], [], [], [], False, [], error=f"Hardware init failed: {e}")
            )
            return

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

            result = scan_for_pptable(
                inpout,
                settings,
                scan_opts=scan_opts,
                progress_callback=on_progress,
                vbios_values=vbios_values,
            )
            if result and self.merge_with_addrs:
                # Only merge old addrs whose page offset matches the new results
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
            # Read OD table from SMU for GUI "orig" values (before cleanup)
            if hw and result:
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
            hw = init_hardware(gui_log=self.log_signal.emit)
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


class VramDumpWorker(QThread):
    """Background worker that dumps the visible VRAM BAR to a gzip-compressed file."""

    progress_signal = Signal(float, str)      # (0..1, status message)
    finished_signal = Signal(str, object)     # (file_path | error_msg, metadata_dict | None)

    CHUNK = 0x400000  # 4 MB per read

    def __init__(self, save_path: str, parent=None):
        super().__init__(parent)
        self.save_path = save_path

    def run(self):
        _log_to_file("VramDumpWorker: starting")
        hw = None
        t0 = time.time()
        try:
            self.progress_signal.emit(0.0, "Initializing hardware...")
            hw = init_hardware()
            inpout = hw["inpout"]
            smu    = hw["smu"]
            vram_bar = hw["vram_bar"]
            dma_path = hw["dma_path"]
            mmio   = hw["mmio"]

            self.progress_signal.emit(0.0, "Detecting BAR size...")
            bar_size = detect_bar_size(inpout, vram_bar)
            if bar_size <= 0:
                self.finished_signal.emit(
                    "BAR size detection failed (0 bytes accessible)", None)
                return

            self.progress_signal.emit(0.0, "Triggering SMU metrics transfer...")
            smu.send_msg(smu.transfer_read, TABLE_SMU_METRICS)
            time.sleep(0.15)
            smu.send_msg(smu.transfer_read, TABLE_SMU_METRICS)
            time.sleep(0.15)

            smu_ver = smu.get_smu_version()
            vram_start, fb_raw = read_vram_start(mmio)

            metadata = {
                "vram_bar":          f"0x{vram_bar:X}",
                "bar_size_bytes":    bar_size,
                "bar_size_mb":       bar_size // (1 << 20),
                "dma_path":          dma_path,
                "smu_version":       list(smu_ver) if isinstance(smu_ver, tuple) else smu_ver,
                "transfer_read":     f"0x{smu.transfer_read:02X}",
                "mmhub_vram_start":  f"0x{vram_start:X}",
                "mmhub_fb_raw":      f"0x{fb_raw:08X}",
            }

            dump_size = bar_size + 0x1000
            total_chunks = max(1, (dump_size + self.CHUNK - 1) // self.CHUNK)
            total_mb = dump_size // (1 << 20)
            self.progress_signal.emit(0.0, f"Dumping {total_mb} MB...")

            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            with gzip.open(self.save_path, "wb", compresslevel=6) as gz:
                for i in range(total_chunks):
                    chunk_base = i * self.CHUNK
                    chunk_sz = min(self.CHUNK, dump_size - chunk_base)
                    try:
                        cv, ch = inpout.map_phys(vram_bar + chunk_base, chunk_sz)
                        snap = read_buf(cv, chunk_sz)
                        inpout.unmap_phys(cv, ch)
                        gz.write(snap)
                    except Exception as e:
                        _log_to_file(f"VramDumpWorker: chunk {i} @ 0x{chunk_base:X} failed: {e}")
                        gz.write(b'\x00' * chunk_sz)

                    pct = (i + 1) / total_chunks
                    mb_done = min((i + 1) * self.CHUNK // (1 << 20), total_mb)
                    self.progress_signal.emit(pct, f"Dumped {mb_done} / {total_mb} MB")

            elapsed = time.time() - t0
            compressed_size = os.path.getsize(self.save_path)
            metadata["dump_size_bytes"]       = dump_size
            metadata["compressed_size_bytes"] = compressed_size
            metadata["elapsed_seconds"]       = round(elapsed, 2)

            sidecar_path = self.save_path.rsplit(".bin.gz", 1)[0] + ".meta.json" \
                if self.save_path.endswith(".bin.gz") \
                else self.save_path + ".meta.json"
            with open(sidecar_path, "w") as f:
                json.dump(metadata, f, indent=2)

            _log_to_file(
                f"VramDumpWorker: completed in {elapsed:.1f}s, "
                f"{compressed_size / (1 << 20):.1f} MB compressed")
            self.finished_signal.emit(self.save_path, metadata)

        except Exception as e:
            _log_exception_to_file("VramDumpWorker")
            self.finished_signal.emit(f"VRAM dump failed: {e}", None)
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    _log_exception_to_file("VramDumpWorker cleanup")
