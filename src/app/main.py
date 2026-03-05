"""
RDNA4 Overclock GUI -- PySide6 Main Window
==========================================

Main application window with:
  - VBIOS gate screen: file picker + copy to bios/ when no VBIOS present
  - Main overclock UI: Simple Settings, PP, OD, SMU tabs, Memory, Registry Patch, log panel, Apply button
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from collections import Counter

from PySide6.QtCore import Qt, QSize, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStyle,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# When frozen by PyInstaller, use exe dir for bios/ and user data
if getattr(sys, "frozen", False):
    _script_dir = os.path.dirname(sys.executable)
else:
    _script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# ---------------------------------------------------------------------------
# File logger -- appends timestamped messages to overclock_log.txt
# ---------------------------------------------------------------------------

_LOG_FILE = os.path.join(_script_dir, "overclock_log.txt")
_file_logger = logging.getLogger("overclock")
_file_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_file_logger.addHandler(_fh)
_file_logger.info("=" * 60)
_file_logger.info("Session started")


def _log_to_file(msg: str):
    """Write a single log line to the persistent log file."""
    try:
        _file_logger.info(msg)
    except Exception:
        pass


def _log_exception_to_file(context: str = ""):
    """Log the current exception traceback to the persistent log file."""
    try:
        tb = traceback.format_exc()
        _file_logger.error(f"EXCEPTION ({context}):\n{tb}")
    except Exception:
        pass


def _install_global_exception_hook():
    """Replace sys.excepthook so unhandled exceptions are logged to file."""
    _original_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            _file_logger.critical(f"UNHANDLED EXCEPTION:\n{tb_text}")
            _fh.flush()
        except Exception:
            pass
        _original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

_install_global_exception_hook()


from src.io.vbios_parser import (
    VbiosValues,
    decode_pp_table_full,
    extract_od_limits_from_decoded,
    OdLimits,
    parse_vbios_from_bytes,
    parse_vbios_or_defaults,
)
from src.io.vbios_storage import read_vbios_decoded, write_vbios_encoded
from src.io.mmio import ensure_driver_files_copied

try:
    from src.tools.reg_patch import (
        RegistryPatch,
        PATCH_VALUES,
        VERIFY_VALUES,
        EXTRA_VALUES,
        RECOMMENDED_VALUES,
        REG_NAME_TO_DISPLAY,
        BACKUP_FILE,
    )
except (ImportError, RuntimeError):
    RegistryPatch = None
    PATCH_VALUES = []
    VERIFY_VALUES = []
    EXTRA_VALUES = []
    RECOMMENDED_VALUES = {}
    REG_NAME_TO_DISPLAY = {}
    BACKUP_FILE = None
from src.engine.overclock_engine import (
    OverclockSettings,
    ScanOptions,
    ScanResult,
    cleanup_hardware,
    init_hardware,
    apply_clocks_only,
    apply_msglimits_only,
    apply_pp_custom_fields,
    apply_od_table_only,
    apply_od_single_field,
    apply_smu_features_only,
    query_smu_state,
    scan_for_pptable,
    read_od,
    read_metrics,
    read_pptable_at_addr,
    is_valid_pptable,
    read_u16,
    read_ecc_info_from_hw,
    read_smu_metrics_full,
    read_smu_table_raw,
)
from src.engine.ecc import (
    EccAccumulator, format_ecc_summary, total_ce_count, format_raw_hex,
    detect_test_pattern,
)
from src.engine.smu import PPSMC, PPCLK, SMU_FEATURE, _CLK_NAMES, _FEATURE_NAMES, _FEATURE_NAMES_LOW
from src.engine.smu_metrics import (
    PPCLK_NAMES, SVI_PLANE_NAMES, TEMP_NAMES, THROTTLER_COUNT,
    D3HOT_SEQUENCE_NAMES,
)
from src.engine.od_table import (
    TABLE_PPTABLE,
    TABLE_DRIVER_INFO,
    TABLE_ECCINFO,
    PP_OD_FEATURE_GFX_VF_CURVE_BIT,
    PP_OD_FEATURE_GFX_VMAX_BIT,
    PP_OD_FEATURE_SOC_VMAX_BIT,
    PP_OD_FEATURE_PPT_BIT,
    PP_OD_FEATURE_TDC_BIT,
    PP_OD_FEATURE_GFXCLK_BIT,
    PP_OD_FEATURE_UCLK_BIT,
    PP_OD_FEATURE_FCLK_BIT,
    PP_OD_FEATURE_FAN_CURVE_BIT,
    PP_OD_FEATURE_FAN_LEGACY_BIT,
    PP_OD_FEATURE_ZERO_FAN_BIT,
    PP_OD_FEATURE_TEMPERATURE_BIT,
    PP_OD_FEATURE_EDC_BIT,
    PP_OD_FEATURE_FULL_CTRL_BIT,
    PP_NUM_OD_VF_CURVE_POINTS,
    NUM_OD_FAN_MAX_POINTS,
)

# ---------------------------------------------------------------------------
# Metrics display order (grouped sections for the Tables sub-tab)
# ---------------------------------------------------------------------------

_METRICS_DISPLAY_SECTIONS = [
    ("Current Clocks (MHz)", [f"CurrClock_{n}" for n in PPCLK_NAMES]),
    ("Power", [
        "AverageSocketPower", "AverageTotalBoardPower", "dGPU_W_MAX",
        "EnergyAccumulator",
    ]),
    ("Voltage (mV)", [f"AvgVoltage_{n}" for n in SVI_PLANE_NAMES]),
    ("Current (mA)", [f"AvgCurrent_{n}" for n in SVI_PLANE_NAMES]),
    ("Activity (%)", [
        "AverageGfxActivity", "AverageUclkActivity",
        "AverageVcn0ActivityPercentage", "Vcn1ActivityPercentage",
    ]),
    ("Fan", ["AvgFanPwm", "AvgFanRpm"]),
    ("Temperature", [f"AvgTemperature_{n}" for n in TEMP_NAMES]
     + ["AvgTemperatureFanIntake"]),
    ("PCIe", ["PcieRate", "PcieWidth"]),
    ("Throttling (%)", [f"ThrottlingPercentage_{i}" for i in range(THROTTLER_COUNT)]
     + ["VmaxThrottlingPercentage"]),
    ("Average Frequencies (MHz)", [
        "AverageGfxclkFrequencyTarget",
        "AverageGfxclkFrequencyPreDs", "AverageGfxclkFrequencyPostDs",
        "AverageFclkFrequencyPreDs", "AverageFclkFrequencyPostDs",
        "AverageMemclkFrequencyPreDs", "AverageMemclkFrequencyPostDs",
        "AverageVclk0Frequency", "AverageDclk0Frequency",
        "AverageVclk1Frequency", "AverageDclk1Frequency",
        "AveragePCIeBusy",
    ]),
    ("Moving Averages", [
        "MovingAverageGfxclkFrequencyTarget",
        "MovingAverageGfxclkFrequencyPreDs", "MovingAverageGfxclkFrequencyPostDs",
        "MovingAverageFclkFrequencyPreDs", "MovingAverageFclkFrequencyPostDs",
        "MovingAverageMemclkFrequencyPreDs", "MovingAverageMemclkFrequencyPostDs",
        "MovingAverageVclk0Frequency", "MovingAverageDclk0Frequency",
        "MovingAverageGfxActivity", "MovingAverageUclkActivity",
        "MovingAverageVcn0ActivityPercentage", "MovingAveragePCIeBusy",
        "MovingAverageUclkActivity_MAX", "MovingAverageSocketPower",
    ]),
    ("D3Hot Counters", (
        [f"D3HotEntry_{n}" for n in D3HOT_SEQUENCE_NAMES]
        + [f"D3HotExit_{n}" for n in D3HOT_SEQUENCE_NAMES]
        + [f"ArmMsgReceived_{n}" for n in D3HOT_SEQUENCE_NAMES]
    )),
    ("Misc", [
        "MetricsCounter",
        "ApuSTAPMSmartShiftLimit", "ApuSTAPMLimit",
        "AvgApuSocketPower", "AverageUclkActivity_MAX",
        "PublicSerialNumberLower", "PublicSerialNumberUpper",
    ]),
]

# Default VBIOS path relative to script directory
DEFAULT_VBIOS_PATH = os.path.join(_script_dir, "bios", "vbios.rom")


def _get_vbios_values(path: str = DEFAULT_VBIOS_PATH):
    """Decode on demand: read from disk, decode, parse. Returns VbiosValues or None.
    Never keeps decoded bytes in memory longer than needed for parsing."""
    rom_bytes, _ = read_vbios_decoded(path)
    if rom_bytes is None:
        return None
    return parse_vbios_from_bytes(rom_bytes, rom_path=path)


# ---------------------------------------------------------------------------
# VBIOS Gate Screen
# ---------------------------------------------------------------------------


class VbiosGateWidget(QWidget):
    """Initial screen shown when no VBIOS is present. Offers file picker and copies to bios/."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on_vbios_ready = None
        layout = QVBoxLayout(self)

        title = QLabel("RDNA4 Overclock")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 18pt; font-weight: bold;")
        layout.addWidget(title)

        hint = QLabel(
            "No VBIOS ROM found. Select a VBIOS ROM file to extract original clock and power limits."
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        layout.addSpacing(16)
        layout.addWidget(hint)

        self.browse_btn = QPushButton("Select VBIOS ROM...")
        self.browse_btn.clicked.connect(self._on_browse)
        layout.addWidget(self.browse_btn, alignment=Qt.AlignCenter)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #c00;")
        layout.addWidget(self.status_label)

        layout.addStretch()

    def set_on_vbios_ready(self, callback):
        """Set callback(vbios_values: VbiosValues) called when VBIOS is successfully loaded."""
        self._on_vbios_ready = callback

    def _on_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select VBIOS ROM",
            "",
            "ROM files (*.rom *.bin);;All files (*)",
        )
        if not path:
            return
        self._copy_and_load(path)

    def _copy_and_load(self, source_path: str):
        """Copy source to bios/vbios.rom and load. Call on_vbios_ready on success."""
        self.status_label.setText("Loading...")
        self.status_label.setStyleSheet("color: #666;")
        QApplication.processEvents()

        try:
            with open(source_path, "rb") as f:
                rom_bytes = f.read()
        except OSError as e:
            self.status_label.setText(f"Failed to read file: {e}")
            self.status_label.setStyleSheet("color: #c00;")
            return

        dest_path = os.path.join(_script_dir, "bios", "vbios.rom")
        if not write_vbios_encoded(dest_path, rom_bytes):
            self.status_label.setText("Failed to save VBIOS.")
            self.status_label.setStyleSheet("color: #c00;")
            return

        self.status_label.setText("Parsing VBIOS...")
        QApplication.processEvents()

        # Parse from in-memory bytes to avoid Windows buffer-flush timing issues
        vals = parse_vbios_from_bytes(rom_bytes, rom_path=dest_path)
        if vals is None:
            self.status_label.setText("Failed to parse VBIOS. No valid PPTable structure found.")
            self.status_label.setStyleSheet("color: #c00;")
            return

        self.status_label.setText("")
        if self._on_vbios_ready:
            self._on_vbios_ready(vals)


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
            _log_to_file(f"ApplyWorker[{self.action_name}]: hardware initialized")
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


class SmuTricksRefreshWorker(QThread):
    """Background worker to read GFXCLK DPM frequency limits from SMU."""

    results_signal = Signal(object)  # dict with min/max or error

    def run(self):
        hw = None
        _log_to_file("SmuTricksRefreshWorker: starting")
        try:
            hw = init_hardware()
            smu = hw["smu"]
            dpm_min = smu.get_min_freq(PPCLK.GFXCLK)
            dpm_max = smu.get_max_freq(PPCLK.GFXCLK)
            self.results_signal.emit({"min": dpm_min, "max": dpm_max})
        except Exception as e:
            _log_exception_to_file("SmuTricksRefreshWorker")
            self.results_signal.emit({"error": str(e)})
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    pass
        _log_to_file("SmuTricksRefreshWorker: done")


class EccRefreshWorker(QThread):
    """Background worker to read ECC counters from SMU."""

    results_signal = Signal(object)  # (ecc_table, raw_bytes) or {"error": str}

    def run(self):
        hw = None
        _log_to_file("EccRefreshWorker: starting")
        try:
            hw = init_hardware()
            ecc_table, raw_bytes = read_ecc_info_from_hw(hw)
            if raw_bytes:
                from src.engine.ecc import format_raw_hex
                _log_to_file(f"ECC raw hex (first 128 bytes):\n{format_raw_hex(raw_bytes[:128])}")
            else:
                _log_to_file("ECC: no data returned (transfer failed or unsupported)")
            self.results_signal.emit((ecc_table, raw_bytes))
        except Exception as e:
            _log_exception_to_file("EccRefreshWorker")
            self.results_signal.emit({"error": str(e)})
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    pass
        _log_to_file("EccRefreshWorker: done")


class MetricsRefreshWorker(QThread):
    """Background worker to read full SmuMetrics_t for the Tables sub-tab."""

    results_signal = Signal(object)  # dict of metrics, or {"error": str}

    def run(self):
        hw = None
        try:
            hw = init_hardware()
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
            hw = init_hardware()
            _log_to_file("DetailedRefreshWorker: init_hardware OK")
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

    def __init__(self, get_vbios_fn, *, merge_with_addrs=None, parent=None):
        super().__init__(parent)
        self.get_vbios_fn = get_vbios_fn
        self.merge_with_addrs = list(merge_with_addrs or [])

    def run(self):
        _log_to_file("ScanThread: starting")
        vbios_values = self.get_vbios_fn()
        if vbios_values is None:
            vbios_values = parse_vbios_or_defaults(DEFAULT_VBIOS_PATH)

        hw = None
        try:
            hw = init_hardware()
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


# ---------------------------------------------------------------------------
# Main Overclock UI
# ---------------------------------------------------------------------------


class MainOverclockWidget(QWidget):
    """Main UI with Simple/Detailed tabs, log panel, progress bar, and Apply button."""

    log_request_signal = Signal(str)

    def __init__(self, vbios_values: VbiosValues, *, used_defaults: bool = False, diagnostic_lines: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.log_request_signal.connect(self._log_gui, Qt.ConnectionType.QueuedConnection)
        self.vbios_values = vbios_values
        self.used_defaults = used_defaults
        self.diagnostic_lines = diagnostic_lines or []
        self.scan_result: ScanResult | None = None
        self._pp_ram_offset_map: dict[str, dict] = {}
        self._pp_patch_keys: set[str] = set()
        layout = QVBoxLayout(self)

        # Info banner
        if used_defaults:
            info_text = (
                "VBIOS structure not recognized — using fallback defaults. "
                "Delete bios/vbios.rom and restart to select a different ROM.\n"
                f"{vbios_values.summary()}"
            )
        else:
            info_text = f"VBIOS: {vbios_values.summary()}"
        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet("background: #2a2a2a; color: #ddd; padding: 8px; border-radius: 4px;")
        layout.addWidget(info)

        # Tabs
        self.tabs = QTabWidget()
        self.simple_tab = QWidget()
        self.pp_tab = QWidget()
        self.od_tab = QWidget()
        self.smu_tab = QWidget()
        self.smu_tricks_tab = QWidget()
        self.ecc_tab = QWidget()
        self.memory_tab = QWidget()
        self.registry_tab = QWidget()
        self._setup_simple_tab()
        self._setup_detailed_tabs()
        self._setup_smu_tab()
        self._setup_smu_tricks_tab()
        self._setup_ecc_tab()
        self._setup_memory_tab()
        self._setup_registry_tab()
        self.tabs.addTab(self.simple_tab, "Simple Settings")
        self.tabs.addTab(self.pp_tab, "PP")
        self.tabs.addTab(self.od_tab, "OD")
        self.tabs.addTab(self.smu_tab, "SMU")
        self.tabs.addTab(self.smu_tricks_tab, "SMU Tricks")
        self.tabs.addTab(self.ecc_tab, "ECC")
        self.tabs.addTab(self.memory_tab, "Memory")
        self.tabs.addTab(self.registry_tab, "Registry Patch")
        layout.addWidget(self.tabs)

        # Progress bar and scan status
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        scan_row = QHBoxLayout()
        self.scan_status_label = QLabel("Ready — press Scan to begin")
        self.scan_status_label.setStyleSheet("color: #888; font-size: 9pt;")
        scan_row.addWidget(self.scan_status_label)
        scan_row.addStretch()
        self.rescan_btn = QPushButton("Scan")
        self.rescan_btn.setToolTip("Scan memory for PPTable addresses")
        self.rescan_btn.clicked.connect(self._on_rescan)
        self.rescan_btn.setEnabled(True)
        scan_row.addWidget(self.rescan_btn)
        layout.addLayout(scan_row)

        # Log panel
        log_label = QLabel("Log")
        layout.addWidget(log_label)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(180)
        self.log_output.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 9pt;"
        )
        layout.addWidget(self.log_output)

        if used_defaults:
            self._log("VBIOS parse failed; using hardcoded defaults.")
            if self.diagnostic_lines:
                self._log("Parse diagnosis:")
                for line in self.diagnostic_lines:
                    self._log("  " + line.strip())
        else:
            self._log("VBIOS values loaded.")

        self._scan_thread = None
        self._set_apply_buttons_enabled(False)
        self._apply_worker = None

    _SIMPLE_HOW_IT_WORKS_HTML = """
<h3>How PP Table RAM Patching Works</h3>
<p>This tool uses a <b>multi-layer defense-in-depth</b> approach to keep your
overclock settings alive despite the Windows AMD driver constantly trying to
re-impose stock limits.</p>

<h4>The Problem</h4>
<p>The AMD GPU driver keeps a cached copy of the <b>PowerPlay (PP) table</b>
in system RAM. Whenever the driver re-evaluates power management &mdash;
on workload changes, thermal events, display mode switches, AC/DC transitions
&mdash; it reads from this cached table, derives the "allowed" clock and power
limits, and sends them to the SMU firmware. This <b>overwrites any direct SMU
commands</b> you may have sent.</p>

<h4>Layer 1 &mdash; PP Table RAM Patch (this tool's scan &amp; patch)</h4>
<p>Scans physical memory for the driver's cached PP table copies and overwrites
clock limits (GameClockAc, BoostClockAc) and MsgLimits (PPT, TDC, temps)
directly in RAM. This <b>poisons the driver's own data source</b> so that when
the driver re-derives limits, it sends your higher values instead of stock.</p>
<ul>
  <li>Without the RAM patch, the driver would periodically re-send stock max
      clocks to the SMU, undoing your overclock.</li>
  <li>With it, the driver unknowingly enforces <i>your</i> limits every time
      it re-evaluates.</li>
</ul>

<h4>Layer 2 &mdash; Direct SMU Messages (sent at the same time)</h4>
<p>After patching RAM, the tool sends SMU commands directly
(<code>SetSoftMaxByFreq</code>, <code>SetHardMaxByFreq</code>,
<code>SetPptLimit</code>, etc.). These take effect <b>immediately</b> on the
SMU firmware, setting clocks and power limits right now &mdash; without waiting
for the driver to re-evaluate.</p>
<ul>
  <li><b>Clock gating features</b> (DS_GFXCLK, GFX_ULV, GFXOFF) are disabled
      via <code>DisableSmuFeaturesLow</code>.</li>
  <li><code>DisallowGfxOff</code> prevents the GPU from entering sleep.</li>
  <li>A workload-mask cycle (PowerSave &rarr; 3D Fullscreen) forces the SMU to
      re-evaluate DPM with the new limits.</li>
</ul>

<h4>Layer 3 &mdash; OD Table (percentage-based, via SMU table transfer)</h4>
<p>The OverDrive table sets percentage offsets (PPT%, TDC%, GfxclkFoffset) via
the official <code>TransferTableDram2Smu</code> protocol. The SMU applies these
on top of the base limits from the PP table &mdash; so the RAM patch and OD
table multiply together.</p>

<h4>Layer 4 &mdash; Registry Anti-Clock-Gating (persistent)</h4>
<p>Registry patches disable the driver's own power management policies
(ULPS, GPU power-down, UVD/VCE clock gating, ASPM, clock stretcher).
Unlike RAM patches, these <b>survive reboots</b> and prevent the driver from
re-enabling power-saving features at the driver policy level.</p>

<h4>Layer 5 &mdash; Watchdog (continuous enforcement)</h4>
<p>The watchdog timer periodically re-sends min-frequency floors and feature
disables to catch any driver re-enables that slip through.</p>

<h4>Why All Layers Are Needed</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Mechanism</th><th>Purpose</th><th>Persistent?</th></tr>
<tr><td>PP Table RAM Patch</td><td>Prevents driver from re-sending stock limits</td>
    <td>Until reboot</td></tr>
<tr><td>Direct SMU Messages</td><td>Immediately sets clocks/power/features</td>
    <td>Until driver overrides</td></tr>
<tr><td>OD Table Commit</td><td>Percentage offset on top of base limits</td>
    <td>Until driver resets OD</td></tr>
<tr><td>Registry Patches</td><td>Disables driver power management policies</td>
    <td>Across reboots</td></tr>
<tr><td>Watchdog</td><td>Re-enforces floor periodically</td>
    <td>While app is running</td></tr>
</table>

<p><b>Bottom line:</b> SMU messages alone are not enough because the driver will
overwrite them. The PP table RAM patch ensures the driver works <i>for</i> you
rather than <i>against</i> you. Both are needed for a robust overclock.</p>
"""

    def _setup_simple_tab(self):
        outer = QVBoxLayout(self.simple_tab)

        hint_row = QHBoxLayout()
        hint_btn = QToolButton()
        hint_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxQuestion))
        hint_btn.setIconSize(QSize(18, 18))
        hint_btn.setToolTip("How PP Table RAM patching works")
        hint_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; }")
        hint_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
        hint_btn.clicked.connect(
            lambda: self._show_smu_cheatsheet(
                "How It Works", self._SIMPLE_HOW_IT_WORKS_HTML))
        hint_row.addWidget(hint_btn)
        hint_row.addWidget(QLabel("<b>How it works</b>"))
        hint_row.addStretch()
        outer.addLayout(hint_row)

        form = QFormLayout()

        self.clock_spin = QSpinBox()
        self.clock_spin.setRange(500, 5000)
        self.clock_spin.setValue(3500)
        self.clock_spin.setSuffix(" MHz")
        self.clock_spin.valueChanged.connect(self._update_effective_max)
        form.addRow("Clock:", self.clock_spin)

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(0, 2000)
        self.offset_spin.setValue(800)
        self.offset_spin.setSuffix(" MHz")
        self.offset_spin.valueChanged.connect(self._update_effective_max)
        form.addRow("Offset:", self.offset_spin)

        self.effective_max_label = QLabel()
        self.effective_max_label.setStyleSheet("font-weight: bold;")
        form.addRow("Effective max:", self.effective_max_label)
        self._update_effective_max()

        outer.addLayout(form)
        self.simple_apply_btn = QPushButton("Apply")
        self.simple_apply_btn.clicked.connect(self._on_apply_simple)
        outer.addWidget(self.simple_apply_btn)
        outer.addStretch()

    def _update_effective_max(self):
        total = self.clock_spin.value() + self.offset_spin.value()
        self.effective_max_label.setText(f"{total} MHz")

    def _on_scan_progress(self, pct: float, msg: str):
        self.progress_bar.setValue(int(pct))
        self.scan_status_label.setText(msg)
        if "Search pattern" in msg or "VBIOS" in msg or "hardcoded" in msg:
            self._log(msg)

    def _on_rescan(self):
        """Rescan memory and merge new PPTable addresses into the existing pool."""
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        existing = getattr(self.scan_result, "valid_addrs", []) or []
        self.rescan_btn.setEnabled(False)
        self._scan_thread = ScanThread(
            lambda: _get_vbios_values(),
            merge_with_addrs=existing,
        )
        self._scan_thread.progress_signal.connect(self._on_scan_progress)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.start()
        if existing:
            self._log("Rescanning for additional PPTable addresses...")
        else:
            self._log("Scanning for PPTable addresses...")

    def _update_detailed_live_columns(self, ram_data, od_table, metrics, smu_state=None):
        """Update Current value column (QLabel widgets) from refresh results."""
        def _fmt(val, suffix=""):
            if val is not None:
                return f"{val}{suffix}"
            return "—"

        updated = 0
        cv_widgets = getattr(self, "_param_current_value_widget", {})
        for section_name, table in self._detailed_tables.items():
            for row in range(table.rowCount()):
                key = table.item(row, 1).text() if table.item(row, 1) else ""
                cv_label = cv_widgets.get(key)
                if cv_label is None or not hasattr(cv_label, "setText"):
                    continue
                smu_key = self._param_smu_key.get(key)

                if smu_key == "od":
                    if od_table:
                        arr_spec = getattr(self, "_param_od_array_spec", {}).get(key)
                        if arr_spec:
                            attr, idx = arr_spec
                            arr = getattr(od_table, attr, None)
                            val = arr[idx] if arr is not None and idx < len(arr) else None
                        else:
                            val = getattr(od_table, key, None)
                        if val is not None:
                            unit = self._param_unit.get(key, "")
                            cv_label.setText(_fmt(val, unit))
                            updated += 1
                        else:
                            cv_label.setText("—")
                    else:
                        cv_label.setText("Unavailable" if section_name in ("OD",) or section_name.startswith("SMU") else "—")
                elif smu_key == "gfxclk" and metrics:
                    cv_label.setText(_fmt(metrics[0], " MHz"))
                    updated += 1
                elif smu_key == "ppt" and metrics:
                    cv_label.setText(_fmt(metrics[2], " W"))
                    updated += 1
                elif smu_key == "temp" and metrics:
                    cv_label.setText(_fmt(metrics[3], " °C"))
                    updated += 1
                elif smu_key and smu_key.startswith("smu_") and smu_state:
                    val = smu_state.get(smu_key)
                    if val is not None:
                        if isinstance(val, bool):
                            cv_label.setText("ON" if val else "OFF")
                        elif smu_key == "smu_features_raw" and isinstance(val, int):
                            cv_label.setText(f"0x{val:016X}")
                        else:
                            unit = self._param_unit.get(key, "")
                            cv_label.setText(_fmt(val, unit))
                        updated += 1
                    else:
                        cv_label.setText("—")
                elif smu_key and smu_key.startswith("smu_") and not smu_state:
                    # Do not overwrite: preserve values set by an earlier call with smu_state
                    # (e.g. _update_od_from_scan calls us with smu_state=None and would wipe SMU)
                    pass
                else:
                    cv_label.setText("—")
        if updated > 0:
            _log_to_file(f"_update_detailed_live_columns: updated {updated} Current value cells")

    def _update_od_from_scan(self, od):
        """Update OD Custom input spinboxes and Current value from scan_result.od_table."""
        if od is None:
            return
        w = self._detailed_param_widgets
        od_simple = {
            "GfxclkFoffset": lambda: max(0, od.GfxclkFoffset),
            "Ppt": lambda: od.Ppt,
            "Tdc": lambda: od.Tdc,
            "UclkFmin": lambda: od.UclkFmin,
            "UclkFmax": lambda: od.UclkFmax,
            "FclkFmin": lambda: od.FclkFmin,
            "FclkFmax": lambda: od.FclkFmax,
            "VddGfxVmax": lambda: od.VddGfxVmax,
            "VddSocVmax": lambda: od.VddSocVmax,
            "FanTargetTemperature": lambda: od.FanTargetTemperature,
            "FanMinimumPwm": lambda: od.FanMinimumPwm,
            "MaxOpTemp": lambda: od.MaxOpTemp,
            "GfxEdc": lambda: od.GfxEdc,
            "GfxPccLimitControl": lambda: od.GfxPccLimitControl,
            "GfxclkFmaxVmax": lambda: od.GfxclkFmaxVmax,
            "GfxclkFmaxVmaxTemperature": lambda: od.GfxclkFmaxVmaxTemperature,
            "IdlePwrSavingFeaturesCtrl": lambda: od.IdlePwrSavingFeaturesCtrl,
            "RuntimePwrSavingFeaturesCtrl": lambda: od.RuntimePwrSavingFeaturesCtrl,
            "AcousticTargetRpmThreshold": lambda: od.AcousticTargetRpmThreshold,
            "AcousticLimitRpmThreshold": lambda: od.AcousticLimitRpmThreshold,
            "FanZeroRpmEnable": lambda: od.FanZeroRpmEnable,
            "FanZeroRpmStopTemp": lambda: od.FanZeroRpmStopTemp,
            "FanMode": lambda: od.FanMode,
            "AdvancedOdModeEnabled": lambda: od.AdvancedOdModeEnabled,
            "GfxVoltageFullCtrlMode": lambda: od.GfxVoltageFullCtrlMode,
            "SocVoltageFullCtrlMode": lambda: od.SocVoltageFullCtrlMode,
            "GfxclkFullCtrlMode": lambda: od.GfxclkFullCtrlMode,
            "UclkFullCtrlMode": lambda: od.UclkFullCtrlMode,
            "FclkFullCtrlMode": lambda: od.FclkFullCtrlMode,
        }
        for key, getter in od_simple.items():
            if key in w and hasattr(w[key], "setValue"):
                w[key].setValue(getter())
        arr_spec = getattr(self, "_param_od_array_spec", {})
        for key, (attr, idx) in arr_spec.items():
            if key in w and hasattr(w[key], "setValue"):
                arr = getattr(od, attr, None)
                if arr is not None and idx < len(arr):
                    w[key].setValue(arr[idx])
        self._update_detailed_live_columns(None, od, None, None)

    def _update_smu_widgets_from_state(self, smu_state):
        """Update Clock Limits current-value labels and pre-fill QSpinBoxes from SMU state."""
        if not smu_state:
            return
        w = self._detailed_param_widgets

        ppt_widget = w.get("SMU_PptLimit")
        ppt_val = smu_state.get("smu_ppt")
        if ppt_widget is not None and hasattr(ppt_widget, "setValue") and ppt_val is not None:
            ppt_widget.setValue(int(ppt_val))

        from src.engine.smu import _CLK_NAMES
        for clk_name in _CLK_NAMES.values():
            for limit_type, smu_suffix in [("SoftMin", "min"), ("SoftMax", "max"),
                                           ("HardMin", "min"), ("HardMax", "max")]:
                wkey = f"SMU_{clk_name}_{limit_type}"
                spin = w.get(wkey)
                freq_val = smu_state.get(f"smu_freq_{clk_name}_{smu_suffix}")
                if spin is not None and hasattr(spin, "setValue") and freq_val is not None:
                    spin.setValue(int(freq_val))

        for key, (label, smu_key) in getattr(self, "_clock_limits_cv", {}).items():
            val = smu_state.get(smu_key)
            if val is not None:
                unit = " W" if "ppt" in smu_key else " MHz"
                label.setText(f"{val}{unit}")
            else:
                label.setText("—")

    def _update_smu_status_labels(self, smu_state):
        """Update the Status sub-tab QLabels directly from smu_state dict."""
        if not smu_state:
            return
        for smu_key, (label, unit) in getattr(self, "_smu_status_labels", {}).items():
            val = smu_state.get(smu_key)
            if val is None:
                continue
            if smu_key == "smu_features_raw" and isinstance(val, int):
                label.setText(f"0x{val:016X}")
            elif isinstance(val, bool):
                label.setText("ON" if val else "OFF")
            else:
                suffix = f" {unit}" if unit else ""
                label.setText(f"{val}{suffix}")

    def _update_smu_feature_checkboxes(self, smu_state):
        """Sync feature checkboxes and state labels in the Features sub-tab."""
        if not smu_state:
            return
        w = self._detailed_param_widgets
        state_labels = getattr(self, "_smu_feature_state_labels", {})
        for bit in range(64):
            enabled = smu_state.get(f"smu_feature_{bit}")
            if enabled is None:
                continue
            cb = w.get(f"SMU_FEAT_{bit}")
            if cb is not None and hasattr(cb, "setChecked"):
                cb.setChecked(bool(enabled))
            label = state_labels.get(bit)
            if label is not None:
                label.setText("ON" if enabled else "OFF")

    def _on_scan_finished(self, result: ScanResult | None):
        self.scan_result = result
        if result is None:
            self._log("Scan failed (no result).")
            self.scan_status_label.setText("Scan failed.")
            self._set_apply_buttons_enabled(False)
            self._update_memory_placeholder("No addresses")
            self.progress_bar.setValue(100)
            self.rescan_btn.setEnabled(True)
            return
        if result.error:
            self._log(f"Scan failed: {result.error}")
            if getattr(result, "od_table", None) is not None:
                self._update_od_from_scan(result.od_table)
                self.scan_status_label.setText(
                    "PPTable not found — OD/SMU apply available"
                )
                self._set_apply_buttons_enabled(True)
                self._start_detailed_refresh_if_ready()
            else:
                self.scan_status_label.setText(f"Scan failed: {result.error}")
                self._set_apply_buttons_enabled(False)
            self._update_memory_placeholder("No addresses")
            self.progress_bar.setValue(100)
            self.rescan_btn.setEnabled(True)
            return
        if getattr(result, "od_table", None) is not None:
            self._update_od_from_scan(result.od_table)
        self._set_apply_buttons_enabled(True)
        if result.valid_addrs:
            page_offsets = set(a & 0xFFF for a in result.valid_addrs)
            offset_str = ", ".join(f"0x{o:03X}" for o in sorted(page_offsets))
            self._log(
                f"Scan complete: found {len(result.valid_addrs)} valid PPTable(s) at "
                + ", ".join(f"0x{a:012X}" for a in result.valid_addrs)
                + f"  [page offset: {offset_str}]"
            )
            if result.rejected_addrs:
                self._log(f"  Rejected {len(result.rejected_addrs)} ghost address(es)")
            self.scan_status_label.setText(
                f"Ready — {len(result.valid_addrs)} PPTable(s) found"
            )
            self._start_memory_refresh_if_ready()
            self._start_detailed_refresh_if_ready()
        else:
            self._log("Scan complete: no valid PPTable addresses found.")
            self._start_detailed_refresh_if_ready()
            self.scan_status_label.setText(
                "No PPTable found — OD/SMU apply available."
            )
            self._update_memory_placeholder("No addresses")
        self.progress_bar.setValue(100)
        self.rescan_btn.setEnabled(True)

    def _setup_detailed_tabs(self):
        """Set up PP, OD, SMU as separate tabs."""
        vb = self.vbios_values

        # Param definitions: (human_name, table_key, source, unit, vbios_val, ram_key, smu_key)
        # smu_key: "od" = from od_table (use table_key as attr), "gfxclk"/"ppt"/"temp" = from metrics
        self._param_ram_key = {}
        self._param_smu_key = {}
        self._param_unit = {}
        self._detailed_param_widgets = {}
        self._param_current_value_widget = {}  # key -> QLabel for Current value column
        self._detailed_tables = {}

        def _add_pp_row(table, human, key, unit, vb_val, ram_key, smu_key, widget):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem(unit))
            cv_label = QLabel("—")
            table.setCellWidget(row, 3, cv_label)
            table.setCellWidget(row, 4, widget)
            self._param_ram_key[key] = ram_key
            self._param_smu_key[key] = smu_key
            self._param_current_value_widget[key] = cv_label
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget

        def _add_od_row(table, human, key, unit, vb_val, smu_key, widget, row_apply_fn=None,
                       feature_bit=None, limits_key=None):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem(unit))
            allowed_str = "—"
            if od_limits is not None and feature_bit is not None:
                allowed_str = "Yes" if od_limits.is_allowed(feature_bit) else "No"
            table.setItem(row, 3, QTableWidgetItem(allowed_str))
            cv_label = QLabel("—")
            table.setCellWidget(row, 4, cv_label)
            table.setCellWidget(row, 5, widget)
            self._param_smu_key[key] = smu_key
            self._param_current_value_widget[key] = cv_label
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget
            # Apply PPTable limits to spinbox range
            if od_limits is not None:
                lk = limits_key if limits_key is not None else key
                rng = od_limits.get_range(lk)
                if rng is not None:
                    widget.setRange(rng[0], rng[1])
            if row_apply_fn is not None:
                btn = QPushButton("Set")
                btn.setMaximumWidth(50)
                _label = human
                _fn = row_apply_fn
                btn.clicked.connect(lambda checked=False, fn=_fn, lbl=_label:
                                    self._run_with_hardware(f"Set {lbl}", fn, require_scan=False))
                table.setCellWidget(row, 6, btn)

        def _add_smu_row(table, human, key, unit, vb_val, widget, smu_key=None, row_apply_fn=None):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem(unit))
            cv_label = QLabel("—")
            table.setCellWidget(row, 3, cv_label)
            table.setCellWidget(row, 4, widget)
            self._param_current_value_widget[key] = cv_label
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget
            if smu_key:
                self._param_smu_key[key] = smu_key
            if row_apply_fn is not None:
                btn = QPushButton("Set")
                btn.setMaximumWidth(50)
                _label = human
                _fn = row_apply_fn
                btn.clicked.connect(lambda checked=False, fn=_fn, lbl=_label:
                                    self._run_with_hardware(f"Set {lbl}", fn, require_scan=False))
                table.setCellWidget(row, 5, btn)

        # (1) PP Section: expanded decoded PP fields
        pp_grp = QGroupBox("PP — Clocks & MsgLimits")
        pp_table = QTableWidget()
        pp_table.setColumnCount(5)
        pp_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Unit",
            "Current value", "Custom input",
        ])
        pp_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        pp_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["PP"] = pp_table
        self._pp_ram_offset_map = {}
        self._pp_patch_keys = set()
        rom_bytes, _ = read_vbios_decoded(DEFAULT_VBIOS_PATH)
        decoded = decode_pp_table_full(rom_bytes, rom_path=DEFAULT_VBIOS_PATH) if rom_bytes else None
        decoded_tree = decoded.data if decoded else None

        def _pp_leaf(path: tuple):
            data = decoded_tree
            if data is None:
                return None
            for part in path:
                if isinstance(part, int) and isinstance(data, dict) and "entries" in data:
                    data = data.get("entries")
                if isinstance(data, dict):
                    if part not in data:
                        return None
                    data = data[part]
                elif isinstance(data, list):
                    if not isinstance(part, int) or part < 0 or part >= len(data):
                        return None
                    data = data[part]
                else:
                    return None
            if isinstance(data, dict) and "value" in data and "offset" in data:
                return data
            return None

        def _build_spinbox(default_value: int, unit: str, field_type: str):
            w = QSpinBox()
            if field_type in ("I", "L", "i", "l"):
                w.setRange(0, 2_000_000_000)
            elif field_type in ("B", "b"):
                w.setRange(0, 255)
            else:
                w.setRange(0, 65535)
            w.setValue(int(default_value))
            if unit:
                w.setSuffix(f" {unit}")
            return w

        # UPP offsets are absolute from PP table start, but valid_addrs
        # from the scan point to BaseClockAc.  Adjust by subtracting the
        # BaseClockAc PP offset so offsets are relative to the clock block.
        _bc_pp_off = getattr(self.vbios_values, 'baseclock_pp_offset', 0)

        def _add_decoded_pp_row(human, key, unit, path, *, group, smu_key=None):
            leaf = _pp_leaf(path)
            if leaf is None:
                return False
            vb_val = int(leaf.get("value", 0))
            raw_offset = int(leaf.get("offset", -1))
            field_type = str(leaf.get("type", "H"))
            widget = _build_spinbox(vb_val, unit, field_type)
            _add_pp_row(pp_table, human, key, unit, vb_val, key, smu_key, widget)
            if raw_offset >= 0:
                self._pp_ram_offset_map[key] = {
                    "offset": raw_offset - _bc_pp_off,
                    "type": field_type,
                    "group": group,
                }
            self._pp_patch_keys.add(key)
            return True

        # DriverReportedClocks
        _add_decoded_pp_row("Base Clock AC", "BaseClockAc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "BaseClockAc"), group="clocks")
        _add_decoded_pp_row("Game Clock AC", "GameClockAc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "GameClockAc"), group="clocks", smu_key="gfxclk")
        _add_decoded_pp_row("Boost Clock AC", "BoostClockAc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "BoostClockAc"), group="clocks")
        _add_decoded_pp_row("Base Clock DC", "BaseClockDc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "BaseClockDc"), group="clocks")
        _add_decoded_pp_row("Game Clock DC", "GameClockDc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "GameClockDc"), group="clocks")
        _add_decoded_pp_row("Boost Clock DC", "BoostClockDc", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "BoostClockDc"), group="clocks")
        _add_decoded_pp_row("Max Reported Clock", "MaxReportedClock", "MHz",
                            ("smc_pptable", "SkuTable", "DriverReportedClocks", "MaxReportedClock"), group="clocks")

        # MsgLimits
        for pidx in range(4):
            for didx in range(2):
                key = f"MSGLIMIT_POWER_{pidx}_{didx}"
                if pidx == 0 and didx == 0:
                    key = "PPT0_AC"
                elif pidx == 0 and didx == 1:
                    key = "PPT0_DC"
                _add_decoded_pp_row(
                    f"MsgLimits Power[{pidx}][{didx}]",
                    key,
                    "W",
                    ("smc_pptable", "SkuTable", "MsgLimits", "Power", pidx, didx),
                    group="msglimits",
                    smu_key="ppt" if pidx == 0 and didx == 0 else None,
                )
        for tidx in range(2):
            key = "TDC_GFX" if tidx == 0 else "TDC_SOC"
            _add_decoded_pp_row(
                f"MsgLimits Tdc[{tidx}]",
                key,
                "A",
                ("smc_pptable", "SkuTable", "MsgLimits", "Tdc", tidx),
                group="msglimits",
            )
        for tidx in range(12):
            key = f"MSGLIMIT_TEMP_{tidx}"
            if tidx == 0:
                key = "Temp_Edge"
            elif tidx == 1:
                key = "Temp_Hotspot"
            elif tidx == 4:
                key = "Temp_Mem"
            elif tidx == 6:
                key = "Temp_VR_GFX"
            elif tidx == 7:
                key = "Temp_VR_SOC"
            _add_decoded_pp_row(
                f"MsgLimits Temperature[{tidx}]",
                key,
                "°C",
                ("smc_pptable", "SkuTable", "MsgLimits", "Temperature", tidx),
                group="msglimits",
                smu_key="temp" if tidx == 0 else None,
            )

        # CustomSkuTable (power/temps/fan)
        for i in range(4):
            _add_decoded_pp_row(f"SocketPowerLimitAc[{i}]", f"SocketPowerLimitAc_{i}", "W",
                                ("smc_pptable", "CustomSkuTable", "SocketPowerLimitAc", i), group="power")
            _add_decoded_pp_row(f"SocketPowerLimitDc[{i}]", f"SocketPowerLimitDc_{i}", "W",
                                ("smc_pptable", "CustomSkuTable", "SocketPowerLimitDc", i), group="power")
        for i in range(2):
            _add_decoded_pp_row(f"PlatformTdcLimit[{i}]", f"PlatformTdcLimit_{i}", "A",
                                ("smc_pptable", "CustomSkuTable", "PlatformTdcLimit", i), group="power")
        for i in range(12):
            _add_decoded_pp_row(f"TemperatureLimit[{i}]", f"TemperatureLimit_{i}", "°C",
                                ("smc_pptable", "CustomSkuTable", "TemperatureLimit", i), group="temps")
            _add_decoded_pp_row(f"FwCtfLimit[{i}]", f"FwCtfLimit_{i}", "°C",
                                ("smc_pptable", "CustomSkuTable", "FwCtfLimit", i), group="temps")
            _add_decoded_pp_row(f"FanStopTemp[{i}]", f"FanStopTemp_{i}", "°C",
                                ("smc_pptable", "CustomSkuTable", "FanStopTemp", i), group="fan")
            _add_decoded_pp_row(f"FanStartTemp[{i}]", f"FanStartTemp_{i}", "°C",
                                ("smc_pptable", "CustomSkuTable", "FanStartTemp", i), group="fan")
            _add_decoded_pp_row(f"FanGain[{i}]", f"FanGain_{i}", "",
                                ("smc_pptable", "CustomSkuTable", "FanGain", i), group="fan")
            _add_decoded_pp_row(f"FanTargetTemperature[{i}]", f"FanTargetTemperature_{i}", "°C",
                                ("smc_pptable", "CustomSkuTable", "FanTargetTemperature", i), group="fan")
        _add_decoded_pp_row("FanPwmMin", "FanPwmMin", "", ("smc_pptable", "CustomSkuTable", "FanPwmMin"), group="fan")
        _add_decoded_pp_row("AcousticTargetRpmThreshold", "AcousticTargetRpmThreshold", "RPM",
                            ("smc_pptable", "CustomSkuTable", "AcousticTargetRpmThreshold"), group="fan")
        _add_decoded_pp_row("AcousticLimitRpmThreshold", "AcousticLimitRpmThreshold", "RPM",
                            ("smc_pptable", "CustomSkuTable", "AcousticLimitRpmThreshold"), group="fan")
        _add_decoded_pp_row("FanMaximumRpm", "FanMaximumRpm", "RPM",
                            ("smc_pptable", "CustomSkuTable", "FanMaximumRpm"), group="fan")
        _add_decoded_pp_row("FanZeroRpmEnable", "FanZeroRpmEnable", "",
                            ("smc_pptable", "CustomSkuTable", "FanZeroRpmEnable"), group="fan")

        # SkuTable frequency tables
        for i in range(16):
            _add_decoded_pp_row(f"FreqTableGfx[{i}]", f"FreqTableGfx_{i}", "MHz",
                                ("smc_pptable", "SkuTable", "FreqTableGfx", i), group="freq")
        for i in range(6):
            _add_decoded_pp_row(f"FreqTableUclk[{i}]", f"FreqTableUclk_{i}", "MHz",
                                ("smc_pptable", "SkuTable", "FreqTableUclk", i), group="freq")
        for i in range(8):
            _add_decoded_pp_row(f"FreqTableSocclk[{i}]", f"FreqTableSocclk_{i}", "MHz",
                                ("smc_pptable", "SkuTable", "FreqTableSocclk", i), group="freq")
            _add_decoded_pp_row(f"FreqTableFclk[{i}]", f"FreqTableFclk_{i}", "MHz",
                                ("smc_pptable", "SkuTable", "FreqTableFclk", i), group="freq")
        _add_decoded_pp_row("GfxclkAibFmax", "GfxclkAibFmax", "MHz",
                            ("smc_pptable", "SkuTable", "GfxclkAibFmax"), group="freq")
        _add_decoded_pp_row("GfxclkFgfxoffEntry", "GfxclkFgfxoffEntry", "MHz",
                            ("smc_pptable", "SkuTable", "GfxclkFgfxoffEntry"), group="freq")
        _add_decoded_pp_row("GfxclkThrottleClock", "GfxclkThrottleClock", "MHz",
                            ("smc_pptable", "SkuTable", "GfxclkThrottleClock"), group="freq")
        _add_decoded_pp_row("GfxclkFreqGfxUlv", "GfxclkFreqGfxUlv", "MHz",
                            ("smc_pptable", "SkuTable", "GfxclkFreqGfxUlv"), group="freq")

        # Voltage
        for i in range(2):
            _add_decoded_pp_row(f"DefaultMaxVoltage[{i}]", f"DefaultMaxVoltage_{i}", "mV",
                                ("smc_pptable", "SkuTable", "DefaultMaxVoltage", i), group="voltage")
            _add_decoded_pp_row(f"BoostMaxVoltage[{i}]", f"BoostMaxVoltage_{i}", "mV",
                                ("smc_pptable", "SkuTable", "BoostMaxVoltage", i), group="voltage")
            _add_decoded_pp_row(f"UlvVoltageOffset[{i}]", f"UlvVoltageOffset_{i}", "mV",
                                ("smc_pptable", "SkuTable", "UlvVoltageOffset", i), group="voltage")

        # Board
        _add_decoded_pp_row("LoadlineGfx", "LoadlineGfx", "", ("smc_pptable", "BoardTable", "LoadlineGfx"), group="board")
        _add_decoded_pp_row("LoadlineSoc", "LoadlineSoc", "", ("smc_pptable", "BoardTable", "LoadlineSoc"), group="board")
        _add_decoded_pp_row("GfxEdcLimit", "GfxEdcLimit", "", ("smc_pptable", "BoardTable", "GfxEdcLimit"), group="board")
        _add_decoded_pp_row("SocEdcLimit", "SocEdcLimit", "", ("smc_pptable", "BoardTable", "SocEdcLimit"), group="board")
        _add_decoded_pp_row("RestBoardPower", "RestBoardPower", "W", ("smc_pptable", "BoardTable", "RestBoardPower"), group="board")

        # Fallback to legacy subset when full decode isn't available.
        if pp_table.rowCount() == 0:
            det_game_clock = QSpinBox()
            det_game_clock.setRange(500, 5000)
            det_game_clock.setValue(vb.gameclock_ac)
            det_game_clock.setSuffix(" MHz")
            _add_pp_row(pp_table, "Game Clock", "GameClockAc", "MHz", vb.gameclock_ac, "gameclock_ac", "gfxclk", det_game_clock)

            det_boost_clock = QSpinBox()
            det_boost_clock.setRange(500, 5000)
            det_boost_clock.setValue(vb.boostclock_ac)
            det_boost_clock.setSuffix(" MHz")
            _add_pp_row(pp_table, "Boost Clock", "BoostClockAc", "MHz", vb.boostclock_ac, "boostclock_ac", None, det_boost_clock)

            det_power_ac = QSpinBox()
            det_power_ac.setRange(50, 600)
            det_power_ac.setValue(vb.power_ac)
            det_power_ac.setSuffix(" W")
            _add_pp_row(pp_table, "PPT AC", "PPT0_AC", "W", vb.power_ac, "ppt0_ac", "ppt", det_power_ac)

            det_power_dc = QSpinBox()
            det_power_dc.setRange(50, 600)
            det_power_dc.setValue(vb.power_dc)
            det_power_dc.setSuffix(" W")
            _add_pp_row(pp_table, "PPT DC", "PPT0_DC", "W", vb.power_dc, "ppt0_dc", None, det_power_dc)

            det_tdc_gfx = QSpinBox()
            det_tdc_gfx.setRange(20, 500)
            det_tdc_gfx.setValue(vb.tdc_gfx)
            det_tdc_gfx.setSuffix(" A")
            _add_pp_row(pp_table, "TDC GFX", "TDC_GFX", "A", vb.tdc_gfx, "tdc_gfx", None, det_tdc_gfx)

            det_tdc_soc = QSpinBox()
            det_tdc_soc.setRange(0, 200)
            det_tdc_soc.setValue(vb.tdc_soc)
            det_tdc_soc.setSuffix(" A")
            _add_pp_row(pp_table, "TDC SOC", "TDC_SOC", "A", vb.tdc_soc, "tdc_soc", None, det_tdc_soc)

            det_temp_edge = QSpinBox()
            det_temp_edge.setRange(0, 150)
            det_temp_edge.setValue(vb.temp_edge if vb.temp_edge else 100)
            det_temp_edge.setSuffix(" °C")
            _add_pp_row(pp_table, "Temp Edge", "Temp_Edge", "°C", vb.temp_edge or "—", "temp_edge", "temp", det_temp_edge)

            det_temp_hotspot = QSpinBox()
            det_temp_hotspot.setRange(0, 150)
            det_temp_hotspot.setValue(vb.temp_hotspot if vb.temp_hotspot else 110)
            det_temp_hotspot.setSuffix(" °C")
            _add_pp_row(pp_table, "Temp Hotspot", "Temp_Hotspot", "°C", vb.temp_hotspot or "—", "temp_hotspot", None, det_temp_hotspot)

            det_temp_mem = QSpinBox()
            det_temp_mem.setRange(0, 150)
            det_temp_mem.setValue(vb.temp_mem if vb.temp_mem else 100)
            det_temp_mem.setSuffix(" °C")
            _add_pp_row(pp_table, "Temp Mem", "Temp_Mem", "°C", vb.temp_mem or "—", "temp_mem", None, det_temp_mem)

            det_temp_vr_gfx = QSpinBox()
            det_temp_vr_gfx.setRange(0, 200)
            det_temp_vr_gfx.setValue(vb.temp_vr_gfx if vb.temp_vr_gfx else 115)
            det_temp_vr_gfx.setSuffix(" °C")
            _add_pp_row(pp_table, "Temp VR GFX", "Temp_VR_GFX", "°C", vb.temp_vr_gfx or "—", "temp_vr_gfx", None, det_temp_vr_gfx)

            det_temp_vr_soc = QSpinBox()
            det_temp_vr_soc.setRange(0, 200)
            det_temp_vr_soc.setValue(vb.temp_vr_soc if vb.temp_vr_soc else 115)
            det_temp_vr_soc.setSuffix(" °C")
            _add_pp_row(pp_table, "Temp VR SOC", "Temp_VR_SOC", "°C", vb.temp_vr_soc or "—", "temp_vr_soc", None, det_temp_vr_soc)

        pp_layout = QVBoxLayout(pp_grp)
        pp_layout.addWidget(pp_table)
        pp_btn_row = QHBoxLayout()
        self.pp_refresh_btn = QPushButton("Refresh")
        self.pp_refresh_btn.setToolTip("Read live values from RAM and SMU")
        self.pp_refresh_btn.clicked.connect(self._on_detailed_refresh_click)
        self.pp_refresh_btn.setEnabled(True)
        pp_btn_row.addWidget(self.pp_refresh_btn)
        self.clocks_apply_btn = QPushButton("Apply PP")
        self.clocks_apply_btn.setToolTip("Patches clocks and MsgLimits in RAM, sends SetSoftMin/Max and SetPptLimit to SMU")
        self.clocks_apply_btn.clicked.connect(self._on_apply_pp)
        self.msglimits_apply_btn = self.clocks_apply_btn
        pp_btn_row.addWidget(self.clocks_apply_btn)
        pp_layout.addLayout(pp_btn_row)

        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setWidget(pp_grp)
        pp_tab_layout = QVBoxLayout(self.pp_tab)
        pp_tab_layout.addWidget(pp_scroll)

        # (2) OD Section — OverDrive Table with per-row Set buttons
        od_grp = QGroupBox("OD — OverDrive Table")
        od_table = QTableWidget()
        od_table.setColumnCount(7)
        od_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Unit", "Allowed",
            "Current value", "Custom input", "Set",
        ])
        od_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        od_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["OD"] = od_table
        self._param_od_array_spec = {}  # key -> (attr, index) for array fields
        od_limits = extract_od_limits_from_decoded(decoded)

        def _mk_od_apply(od_attr, feature_bit, spin, label):
            def fn(hw):
                def modify(od):
                    setattr(od, od_attr, spin.value())
                    od.FeatureCtrlMask |= (1 << feature_bit)
                ok, err = apply_od_single_field(hw["smu"], hw["virt"], modify)
                if ok:
                    self._log(f"OD: Set {label} = {spin.value()} OK")
                    return None
                msg = f"OD: Set {label} = {spin.value()} failed: {err}"
                self._log(msg)
                return (False, msg)

            return fn

        def _mk_od_apply_array(attr, idx, feature_bit, spin, label):
            def fn(hw):
                def modify(od):
                    arr = getattr(od, attr)
                    arr[idx] = spin.value()
                    od.FeatureCtrlMask |= (1 << feature_bit)
                ok, err = apply_od_single_field(hw["smu"], hw["virt"], modify)
                if ok:
                    self._log(f"OD: Set {label} = {spin.value()} OK")
                    return None
                msg = f"OD: Set {label} = {spin.value()} failed: {err}"
                self._log(msg)
                return (False, msg)

            return fn

        det_gfx_offset = QSpinBox()
        det_gfx_offset.setRange(0, 2000)
        det_gfx_offset.setValue(200)
        det_gfx_offset.setSuffix(" MHz")
        _add_od_row(od_table, "Gfxclk Offset", "GfxclkFoffset", "MHz", None, "od", det_gfx_offset,
                    row_apply_fn=_mk_od_apply("GfxclkFoffset", PP_OD_FEATURE_GFXCLK_BIT, det_gfx_offset, "Gfxclk Offset"),
                    feature_bit=PP_OD_FEATURE_GFXCLK_BIT)

        det_od_ppt = QSpinBox()
        det_od_ppt.setRange(-50, 100)
        det_od_ppt.setValue(10)
        det_od_ppt.setSuffix("%")
        _add_od_row(od_table, "PPT %", "Ppt", "%", None, "od", det_od_ppt,
                    row_apply_fn=_mk_od_apply("Ppt", PP_OD_FEATURE_PPT_BIT, det_od_ppt, "PPT %"),
                    feature_bit=PP_OD_FEATURE_PPT_BIT)

        det_od_tdc = QSpinBox()
        det_od_tdc.setRange(-50, 100)
        det_od_tdc.setValue(0)
        det_od_tdc.setSuffix("%")
        _add_od_row(od_table, "TDC %", "Tdc", "%", None, "od", det_od_tdc,
                    row_apply_fn=_mk_od_apply("Tdc", PP_OD_FEATURE_TDC_BIT, det_od_tdc, "TDC %"),
                    feature_bit=PP_OD_FEATURE_TDC_BIT)

        det_uclk_min = QSpinBox()
        det_uclk_min.setRange(0, 3000)
        det_uclk_min.setValue(0)
        det_uclk_min.setSpecialValueText("no change")
        det_uclk_min.setSuffix(" MHz")
        _add_od_row(od_table, "UCLK min", "UclkFmin", "MHz", None, "od", det_uclk_min,
                    row_apply_fn=_mk_od_apply("UclkFmin", PP_OD_FEATURE_UCLK_BIT, det_uclk_min, "UCLK min"),
                    feature_bit=PP_OD_FEATURE_UCLK_BIT)

        det_uclk_max = QSpinBox()
        det_uclk_max.setRange(0, 3000)
        det_uclk_max.setValue(0)
        det_uclk_max.setSpecialValueText("no change")
        det_uclk_max.setSuffix(" MHz")
        _add_od_row(od_table, "UCLK max", "UclkFmax", "MHz", None, "od", det_uclk_max,
                    row_apply_fn=_mk_od_apply("UclkFmax", PP_OD_FEATURE_UCLK_BIT, det_uclk_max, "UCLK max"),
                    feature_bit=PP_OD_FEATURE_UCLK_BIT)

        det_fclk_min = QSpinBox()
        det_fclk_min.setRange(0, 3000)
        det_fclk_min.setValue(0)
        det_fclk_min.setSpecialValueText("no change")
        det_fclk_min.setSuffix(" MHz")
        _add_od_row(od_table, "FCLK min", "FclkFmin", "MHz", None, "od", det_fclk_min,
                    row_apply_fn=_mk_od_apply("FclkFmin", PP_OD_FEATURE_FCLK_BIT, det_fclk_min, "FCLK min"),
                    feature_bit=PP_OD_FEATURE_FCLK_BIT)

        det_fclk_max = QSpinBox()
        det_fclk_max.setRange(0, 3000)
        det_fclk_max.setValue(0)
        det_fclk_max.setSpecialValueText("no change")
        det_fclk_max.setSuffix(" MHz")
        _add_od_row(od_table, "FCLK max", "FclkFmax", "MHz", None, "od", det_fclk_max,
                    row_apply_fn=_mk_od_apply("FclkFmax", PP_OD_FEATURE_FCLK_BIT, det_fclk_max, "FCLK max"),
                    feature_bit=PP_OD_FEATURE_FCLK_BIT)

        # Voltage offsets per V/F zone (6 points)
        for i in range(PP_NUM_OD_VF_CURVE_POINTS):
            key = f"VoltageOffsetZone{i}"
            self._param_od_array_spec[key] = ("VoltageOffsetPerZoneBoundary", i)
            spin = QSpinBox()
            spin.setRange(-500, 500)
            spin.setValue(0)
            spin.setSuffix(" mV")
            _add_od_row(od_table, f"V/F Zone {i}", key, "mV", None, "od", spin,
                        row_apply_fn=_mk_od_apply_array(
                            "VoltageOffsetPerZoneBoundary", i, PP_OD_FEATURE_GFX_VF_CURVE_BIT, spin, f"V/F Zone {i}"),
                        feature_bit=PP_OD_FEATURE_GFX_VF_CURVE_BIT, limits_key="VoltageOffsetPerZoneBoundary")

        # VddGfxVmax, VddSocVmax
        det_vdd_gfx = QSpinBox()
        det_vdd_gfx.setRange(0, 2000)
        det_vdd_gfx.setValue(0)
        det_vdd_gfx.setSpecialValueText("no change")
        det_vdd_gfx.setSuffix(" mV")
        _add_od_row(od_table, "VddGfx Vmax", "VddGfxVmax", "mV", None, "od", det_vdd_gfx,
                    row_apply_fn=_mk_od_apply("VddGfxVmax", PP_OD_FEATURE_GFX_VMAX_BIT, det_vdd_gfx, "VddGfx Vmax"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_vdd_soc = QSpinBox()
        det_vdd_soc.setRange(0, 2000)
        det_vdd_soc.setValue(0)
        det_vdd_soc.setSpecialValueText("no change")
        det_vdd_soc.setSuffix(" mV")
        _add_od_row(od_table, "VddSoc Vmax", "VddSocVmax", "mV", None, "od", det_vdd_soc,
                    row_apply_fn=_mk_od_apply("VddSocVmax", PP_OD_FEATURE_SOC_VMAX_BIT, det_vdd_soc, "VddSoc Vmax"),
                    feature_bit=PP_OD_FEATURE_SOC_VMAX_BIT)

        # Fan controls
        det_fan_target_temp = QSpinBox()
        det_fan_target_temp.setRange(0, 120)
        det_fan_target_temp.setValue(0)
        det_fan_target_temp.setSpecialValueText("no change")
        det_fan_target_temp.setSuffix(" °C")
        _add_od_row(od_table, "Fan Target Temp", "FanTargetTemperature", "°C", None, "od", det_fan_target_temp,
                    row_apply_fn=_mk_od_apply("FanTargetTemperature", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_target_temp, "Fan Target Temp"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_fan_min_pwm = QSpinBox()
        det_fan_min_pwm.setRange(0, 255)
        det_fan_min_pwm.setValue(0)
        det_fan_min_pwm.setSpecialValueText("no change")
        _add_od_row(od_table, "Fan Min PWM", "FanMinimumPwm", "", None, "od", det_fan_min_pwm,
                    row_apply_fn=_mk_od_apply("FanMinimumPwm", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_min_pwm, "Fan Min PWM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_max_op_temp = QSpinBox()
        det_max_op_temp.setRange(0, 127)
        det_max_op_temp.setValue(0)
        det_max_op_temp.setSpecialValueText("no change")
        det_max_op_temp.setSuffix(" °C")
        _add_od_row(od_table, "Max Op Temp", "MaxOpTemp", "°C", None, "od", det_max_op_temp,
                    row_apply_fn=_mk_od_apply("MaxOpTemp", PP_OD_FEATURE_TEMPERATURE_BIT, det_max_op_temp, "Max Op Temp"),
                    feature_bit=PP_OD_FEATURE_TEMPERATURE_BIT)

        # EDC / PCC
        det_gfx_edc = QSpinBox()
        det_gfx_edc.setRange(-32768, 32767)
        det_gfx_edc.setValue(0)
        det_gfx_edc.setSpecialValueText("no change")
        _add_od_row(od_table, "Gfx EDC", "GfxEdc", "", None, "od", det_gfx_edc,
                    row_apply_fn=_mk_od_apply("GfxEdc", PP_OD_FEATURE_EDC_BIT, det_gfx_edc, "Gfx EDC"),
                    feature_bit=PP_OD_FEATURE_EDC_BIT)

        det_gfx_pcc = QSpinBox()
        det_gfx_pcc.setRange(-32768, 32767)
        det_gfx_pcc.setValue(0)
        det_gfx_pcc.setSpecialValueText("no change")
        _add_od_row(od_table, "Gfx PCC Limit", "GfxPccLimitControl", "", None, "od", det_gfx_pcc,
                    row_apply_fn=_mk_od_apply("GfxPccLimitControl", PP_OD_FEATURE_EDC_BIT, det_gfx_pcc, "Gfx PCC Limit"),
                    feature_bit=PP_OD_FEATURE_EDC_BIT)

        # GfxclkFmaxVmax
        det_gfx_fmax_vmax = QSpinBox()
        det_gfx_fmax_vmax.setRange(0, 5000)
        det_gfx_fmax_vmax.setValue(0)
        det_gfx_fmax_vmax.setSpecialValueText("no change")
        det_gfx_fmax_vmax.setSuffix(" MHz")
        _add_od_row(od_table, "Gfxclk Fmax@Vmax", "GfxclkFmaxVmax", "MHz", None, "od", det_gfx_fmax_vmax,
                    row_apply_fn=_mk_od_apply("GfxclkFmaxVmax", PP_OD_FEATURE_GFX_VMAX_BIT, det_gfx_fmax_vmax, "Gfxclk Fmax@Vmax"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_gfx_fmax_vmax_temp = QSpinBox()
        det_gfx_fmax_vmax_temp.setRange(0, 127)
        det_gfx_fmax_vmax_temp.setValue(0)
        det_gfx_fmax_vmax_temp.setSpecialValueText("no change")
        det_gfx_fmax_vmax_temp.setSuffix(" °C")
        _add_od_row(od_table, "Gfxclk Fmax@Vmax Temp", "GfxclkFmaxVmaxTemperature", "°C", None, "od", det_gfx_fmax_vmax_temp,
                    row_apply_fn=_mk_od_apply("GfxclkFmaxVmaxTemperature", PP_OD_FEATURE_GFX_VMAX_BIT, det_gfx_fmax_vmax_temp, "Gfxclk Fmax@Vmax Temp"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_idle_pwr = QSpinBox()
        det_idle_pwr.setRange(0, 255)
        det_idle_pwr.setValue(0)
        det_idle_pwr.setSpecialValueText("no change")
        _add_od_row(od_table, "Idle Pwr Saving Ctrl", "IdlePwrSavingFeaturesCtrl", "", None, "od", det_idle_pwr,
                    row_apply_fn=_mk_od_apply("IdlePwrSavingFeaturesCtrl", PP_OD_FEATURE_FULL_CTRL_BIT, det_idle_pwr, "Idle Pwr Saving Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_runtime_pwr = QSpinBox()
        det_runtime_pwr.setRange(0, 255)
        det_runtime_pwr.setValue(0)
        det_runtime_pwr.setSpecialValueText("no change")
        _add_od_row(od_table, "Runtime Pwr Saving Ctrl", "RuntimePwrSavingFeaturesCtrl", "", None, "od", det_runtime_pwr,
                    row_apply_fn=_mk_od_apply("RuntimePwrSavingFeaturesCtrl", PP_OD_FEATURE_FULL_CTRL_BIT, det_runtime_pwr, "Runtime Pwr Saving Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        for i in range(NUM_OD_FAN_MAX_POINTS):
            key_pwm = f"FanLinearPwm{i}"
            self._param_od_array_spec[key_pwm] = ("FanLinearPwmPoints", i)
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(0)
            spin.setSpecialValueText("no change")
            _add_od_row(od_table, f"Fan PWM pt {i}", key_pwm, "%", None, "od", spin,
                        row_apply_fn=_mk_od_apply_array("FanLinearPwmPoints", i, PP_OD_FEATURE_FAN_CURVE_BIT, spin, f"Fan PWM pt {i}"),
                        feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT, limits_key="FanLinearPwmPoints")
            key_temp = f"FanLinearTemp{i}"
            self._param_od_array_spec[key_temp] = ("FanLinearTempPoints", i)
            spin_temp = QSpinBox()
            spin_temp.setRange(0, 127)
            spin_temp.setValue(0)
            spin_temp.setSpecialValueText("no change")
            spin_temp.setSuffix(" °C")
            _add_od_row(od_table, f"Fan Temp pt {i}", key_temp, "°C", None, "od", spin_temp,
                        row_apply_fn=_mk_od_apply_array("FanLinearTempPoints", i, PP_OD_FEATURE_FAN_CURVE_BIT, spin_temp, f"Fan Temp pt {i}"),
                        feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT, limits_key="FanLinearTempPoints")

        det_acoustic_target = QSpinBox()
        det_acoustic_target.setRange(0, 65535)
        det_acoustic_target.setValue(0)
        det_acoustic_target.setSpecialValueText("no change")
        det_acoustic_target.setSuffix(" RPM")
        _add_od_row(od_table, "Acoustic Target RPM", "AcousticTargetRpmThreshold", " RPM", None, "od", det_acoustic_target,
                    row_apply_fn=_mk_od_apply("AcousticTargetRpmThreshold", PP_OD_FEATURE_FAN_CURVE_BIT, det_acoustic_target, "Acoustic Target RPM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_acoustic_limit = QSpinBox()
        det_acoustic_limit.setRange(0, 65535)
        det_acoustic_limit.setValue(0)
        det_acoustic_limit.setSpecialValueText("no change")
        det_acoustic_limit.setSuffix(" RPM")
        _add_od_row(od_table, "Acoustic Limit RPM", "AcousticLimitRpmThreshold", " RPM", None, "od", det_acoustic_limit,
                    row_apply_fn=_mk_od_apply("AcousticLimitRpmThreshold", PP_OD_FEATURE_FAN_CURVE_BIT, det_acoustic_limit, "Acoustic Limit RPM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_fan_zero_rpm = QSpinBox()
        det_fan_zero_rpm.setRange(0, 1)
        det_fan_zero_rpm.setValue(0)
        det_fan_zero_rpm.setSpecialValueText("no change")
        _add_od_row(od_table, "Fan Zero RPM Enable", "FanZeroRpmEnable", "", None, "od", det_fan_zero_rpm,
                    row_apply_fn=_mk_od_apply("FanZeroRpmEnable", PP_OD_FEATURE_ZERO_FAN_BIT, det_fan_zero_rpm, "Fan Zero RPM Enable"),
                    feature_bit=PP_OD_FEATURE_ZERO_FAN_BIT)

        det_fan_zero_stop = QSpinBox()
        det_fan_zero_stop.setRange(0, 127)
        det_fan_zero_stop.setValue(0)
        det_fan_zero_stop.setSpecialValueText("no change")
        det_fan_zero_stop.setSuffix(" °C")
        _add_od_row(od_table, "Fan Zero RPM Stop Temp", "FanZeroRpmStopTemp", "°C", None, "od", det_fan_zero_stop,
                    row_apply_fn=_mk_od_apply("FanZeroRpmStopTemp", PP_OD_FEATURE_ZERO_FAN_BIT, det_fan_zero_stop, "Fan Zero RPM Stop Temp"),
                    feature_bit=PP_OD_FEATURE_ZERO_FAN_BIT)

        det_fan_mode = QSpinBox()
        det_fan_mode.setRange(0, 255)
        det_fan_mode.setValue(0)
        det_fan_mode.setSpecialValueText("no change")
        _add_od_row(od_table, "Fan Mode", "FanMode", "", None, "od", det_fan_mode,
                    row_apply_fn=_mk_od_apply("FanMode", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_mode, "Fan Mode"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_advanced_od = QSpinBox()
        det_advanced_od.setRange(0, 1)
        det_advanced_od.setValue(0)
        det_advanced_od.setSpecialValueText("no change")
        _add_od_row(od_table, "Advanced OD Mode", "AdvancedOdModeEnabled", "", None, "od", det_advanced_od,
                    row_apply_fn=_mk_od_apply("AdvancedOdModeEnabled", PP_OD_FEATURE_FULL_CTRL_BIT, det_advanced_od, "Advanced OD Mode"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_gfx_volt_full = QSpinBox()
        det_gfx_volt_full.setRange(0, 65535)
        det_gfx_volt_full.setValue(0)
        det_gfx_volt_full.setSpecialValueText("no change")
        det_gfx_volt_full.setSuffix(" mV")
        _add_od_row(od_table, "Gfx Voltage Full Ctrl", "GfxVoltageFullCtrlMode", "mV", None, "od", det_gfx_volt_full,
                    row_apply_fn=_mk_od_apply("GfxVoltageFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_gfx_volt_full, "Gfx Voltage Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_soc_volt_full = QSpinBox()
        det_soc_volt_full.setRange(0, 65535)
        det_soc_volt_full.setValue(0)
        det_soc_volt_full.setSpecialValueText("no change")
        det_soc_volt_full.setSuffix(" mV")
        _add_od_row(od_table, "Soc Voltage Full Ctrl", "SocVoltageFullCtrlMode", "mV", None, "od", det_soc_volt_full,
                    row_apply_fn=_mk_od_apply("SocVoltageFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_soc_volt_full, "Soc Voltage Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_gfxclk_full = QSpinBox()
        det_gfxclk_full.setRange(0, 5000)
        det_gfxclk_full.setValue(0)
        det_gfxclk_full.setSpecialValueText("no change")
        det_gfxclk_full.setSuffix(" MHz")
        _add_od_row(od_table, "Gfxclk Full Ctrl", "GfxclkFullCtrlMode", "MHz", None, "od", det_gfxclk_full,
                    row_apply_fn=_mk_od_apply("GfxclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_gfxclk_full, "Gfxclk Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_uclk_full = QSpinBox()
        det_uclk_full.setRange(0, 3000)
        det_uclk_full.setValue(0)
        det_uclk_full.setSpecialValueText("no change")
        det_uclk_full.setSuffix(" MHz")
        _add_od_row(od_table, "UCLK Full Ctrl", "UclkFullCtrlMode", "MHz", None, "od", det_uclk_full,
                    row_apply_fn=_mk_od_apply("UclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_uclk_full, "UCLK Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_fclk_full = QSpinBox()
        det_fclk_full.setRange(0, 3000)
        det_fclk_full.setValue(0)
        det_fclk_full.setSpecialValueText("no change")
        det_fclk_full.setSuffix(" MHz")
        _add_od_row(od_table, "FCLK Full Ctrl", "FclkFullCtrlMode", "MHz", None, "od", det_fclk_full,
                    row_apply_fn=_mk_od_apply("FclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_fclk_full, "FCLK Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        od_layout = QVBoxLayout(od_grp)
        od_layout.addWidget(od_table)
        od_btn_row = QHBoxLayout()
        self.od_refresh_btn = QPushButton("Refresh")
        self.od_refresh_btn.setToolTip("Read live values from RAM and SMU")
        self.od_refresh_btn.clicked.connect(self._on_detailed_refresh_click)
        self.od_refresh_btn.setEnabled(True)
        od_btn_row.addWidget(self.od_refresh_btn)
        self.od_apply_btn = QPushButton("Apply OD")
        self.od_apply_btn.setToolTip("Sends OD table (offset, PPT%, TDC%, UCLK/FCLK) to SMU via table transfer")
        self.od_apply_btn.clicked.connect(self._on_apply_od)
        od_btn_row.addWidget(self.od_apply_btn)
        od_layout.addLayout(od_btn_row)

        od_scroll = QScrollArea()
        od_scroll.setWidgetResizable(True)
        od_scroll.setWidget(od_grp)
        od_tab_layout = QVBoxLayout(self.od_tab)
        od_tab_layout.addWidget(od_scroll)

        self._detailed_worker = None

    def _show_smu_cheatsheet(self, title: str, html: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Cheatsheet — {title}")
        dlg.resize(620, 480)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(html)
        lay.addWidget(browser)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def _setup_smu_tab(self):
        """Set up the SMU tab with a nested QTabWidget containing 5 sub-tabs:
        Status, Clock Limits, Controls, Features, Tables.
        """
        from src.engine.smu import _CLK_NAMES as _ALL_CLK_NAMES
        from src.engine.overclock_engine import read_smu_metrics_full

        outer_layout = QVBoxLayout(self.smu_tab)
        self._smu_inner_tabs = QTabWidget()
        outer_layout.addWidget(self._smu_inner_tabs)
        self._smu_refresh_buttons = []

        def _make_smu_table(with_set_col=True):
            t = QTableWidget()
            cols = 6 if with_set_col else 5
            t.setColumnCount(cols)
            headers = ["Human name", "Table key", "Unit", "Current value", "Custom input"]
            if with_set_col:
                headers.append("Set")
            t.setHorizontalHeaderLabels(headers)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.horizontalHeader().setStretchLastSection(True)
            return t

        def _add_smu_row(table, human, key, unit, vb_val, widget, smu_key=None, row_apply_fn=None):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem(unit))
            cv_label = QLabel("—")
            table.setCellWidget(row, 3, cv_label)
            table.setCellWidget(row, 4, widget)
            self._param_current_value_widget[key] = cv_label
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget
            if smu_key:
                self._param_smu_key[key] = smu_key
            if row_apply_fn is not None:
                btn = QPushButton("Set")
                btn.setMaximumWidth(50)
                _label = human
                _fn = row_apply_fn
                btn.clicked.connect(lambda checked=False, fn=_fn, lbl=_label:
                                    self._run_with_hardware(f"Set {lbl}", fn, require_scan=False))
                table.setCellWidget(row, 5, btn)

        def _add_refresh_btn(layout_target):
            btn = QPushButton("Refresh")
            btn.setToolTip("Read all SMU state: DPM freq ranges, PPT, voltage, features")
            btn.clicked.connect(self._on_detailed_refresh_click)
            self._smu_refresh_buttons.append(btn)
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addStretch()
            layout_target.addLayout(row)

        def _add_cheatsheet_btn(layout_target, tab_title, html_content):
            btn = QToolButton()
            btn.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxQuestion))
            btn.setIconSize(QSize(18, 18))
            btn.setToolTip(f"Open cheatsheet for {tab_title}")
            btn.setStyleSheet(
                "QToolButton { border: none; background: transparent; }")
            btn.setCursor(Qt.CursorShape.WhatsThisCursor)
            btn.clicked.connect(
                lambda checked=False, t=tab_title, h=html_content:
                    self._show_smu_cheatsheet(t, h))
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addWidget(QLabel(f"<b>{tab_title}</b>"))
            row.addStretch()
            layout_target.addLayout(row)

        # ==================================================================
        # Sub-tab 1: Status (read-only, 2-column Name | Value)
        # ==================================================================
        _STATUS_CHEATSHEET = """
<h3>Status — Read-only SMU dashboard</h3>
<p>This tab shows the <b>live state</b> reported by the GPU's System Management Unit (SMU).
Nothing here is writable — it's a monitoring-only view. Hit <b>Refresh</b> to re-read
all values from hardware.</p>

<h4>What each row means</h4>
<ul>
  <li><b>SMU Firmware Version</b> — Version string of the firmware running on the SMU
      micro-controller inside the GPU die.</li>
  <li><b>Driver IF Version</b> — The interface contract version the SMU firmware expects
      from the host-side driver. Mismatched versions may cause commands to be rejected.</li>
  <li><b>GFX Voltage (SVI3)</b> — Real-time GPU core voltage (millivolts) as reported by
      the SVI3 serial voltage-identification bus between the SMU and the voltage
      regulator.</li>
  <li><b>Running Features (hex)</b> — Raw 64-bit bitmask of all currently enabled SMU
      features. Each bit corresponds to a feature listed in the <i>Features</i> sub-tab.</li>
  <li><b>Current PPT Limit</b> — Package Power Tracking limit in watts. This is the maximum
      total board power the SMU will allow before it starts throttling.</li>
  <li><b>Per-clock Min / Max</b> — The current DPM frequency boundaries for each of the 11
      clock domains (GFXCLK, SOCCLK, UCLK, etc.). "Min" is the floor, "Max" is the
      ceiling the SMU is enforcing right now.</li>
  <li><b>DC Max</b> — Maximum allowed frequency for each clock when the system is running
      on battery (DC) power rather than wall power (AC).</li>
</ul>

<h4>All clock domains shown</h4>
<p>Three rows appear for each of the 11 PPCLK domains (Min / Max / DC Max):</p>
<ul>
  <li><b>GFXCLK</b> — Graphics/shader engine core clock.</li>
  <li><b>SOCCLK</b> — System-on-chip infrastructure clock.</li>
  <li><b>UCLK</b> — Unified memory controller clock (VRAM bandwidth).</li>
  <li><b>FCLK</b> — Data-fabric clock (GFX &harr; memory &harr; IO interconnect).</li>
  <li><b>DCLK0</b> — Video decoder engine clock.</li>
  <li><b>VCLK0</b> — Video encoder engine clock.</li>
  <li><b>DISPCLK</b> — Display pipe clock.</li>
  <li><b>DPPCLK</b> — Display pixel-processing clock.</li>
  <li><b>DPREFCLK</b> — Display reference clock (DisplayPort symbol rate base).</li>
  <li><b>DCFCLK</b> — Display controller fabric clock.</li>
  <li><b>DTBCLK</b> — Display timing-base clock (pixel timing generator).</li>
</ul>

<h4>UI controls on this tab</h4>
<ul>
  <li><b>Refresh</b> button — Sends a query to the SMU and updates every row. This is the
      only way to update values on this tab; nothing is streamed automatically.</li>
  <li>All cells are <b>read-only</b>. You can select and copy text from the Value column.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Values only update when you click <b>Refresh</b> — they are not live-streamed.</li>
  <li>GFX Voltage may read 0 mV when the GPU is deeply idle (GfxOff).</li>
  <li>Min/Max frequencies show the SMU's <i>enforced</i> boundaries, which may differ from
      what you requested if the SMU clamped or rejected the value.</li>
  <li>DC Max values are only meaningful on laptops with battery. On desktop GPUs they
      typically mirror the AC max or report 0.</li>
  <li>The Running Features hex value may be hard to read. Cross-reference individual bits
      in the <i>Features</i> sub-tab for a human-readable breakdown.</li>
</ul>
"""
        self._smu_status_labels = {}

        status_w = QWidget()
        status_lay = QVBoxLayout(status_w)
        _add_cheatsheet_btn(status_lay, "Status", _STATUS_CHEATSHEET)
        status_tbl = QTableWidget()
        status_tbl.setColumnCount(2)
        status_tbl.setHorizontalHeaderLabels(["Name", "Value"])
        status_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        status_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        status_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        status_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        status_tbl.verticalHeader().setVisible(False)
        self._detailed_tables["SMU_Status"] = status_tbl

        def _add_status_row(human_name, smu_key, unit=""):
            row = status_tbl.rowCount()
            status_tbl.insertRow(row)
            name_item = QTableWidgetItem(human_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            status_tbl.setItem(row, 0, name_item)
            val_label = QLabel("—")
            val_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            status_tbl.setCellWidget(row, 1, val_label)
            self._smu_status_labels[smu_key] = (val_label, unit)

        _add_status_row("SMU Firmware Version", "smu_version")
        _add_status_row("Driver IF Version", "smu_drv_if")
        _add_status_row("GFX Voltage (SVI3)", "smu_voltage", "mV")
        _add_status_row("Running Features (hex)", "smu_features_raw")
        _add_status_row("Current PPT Limit", "smu_ppt", "W")

        for clk_id, clk_name in sorted(_ALL_CLK_NAMES.items()):
            _add_status_row(f"{clk_name} Min", f"smu_freq_{clk_name}_min", "MHz")
            _add_status_row(f"{clk_name} Max", f"smu_freq_{clk_name}_max", "MHz")
            _add_status_row(f"{clk_name} DC Max", f"smu_dcmax_{clk_name}", "MHz")

        status_lay.addWidget(status_tbl)
        _add_refresh_btn(status_lay)
        status_scroll = QScrollArea()
        status_scroll.setWidgetResizable(True)
        status_scroll.setWidget(status_w)
        self._smu_inner_tabs.addTab(status_scroll, "Status")

        # ==================================================================
        # Sub-tab 2: Clock Limits  (grid: 11 clocks × 4 limit types)
        # ==================================================================
        _CLOCK_CHEATSHEET = """
<h3>Clock Limits — DPM frequency boundaries</h3>
<p>This tab lets you override the Dynamic Power Management (DPM) frequency range for
each of the GPU's 11 clock domains. You can also set the Package Power Tracking (PPT)
limit.</p>

<h4>Limit types</h4>
<ul>
  <li><b>SoftMin / SoftMax</b> — <i>Advisory</i> limits. The SMU will try to keep the clock
      within this range but may briefly step outside it during transient workloads or
      thermal events. Think of these as "preferred operating range".</li>
  <li><b>HardMin / HardMax</b> — <i>Absolute</i> floor and ceiling. The SMU will not clock
      below HardMin or above HardMax under any circumstances (short of emergency
      thermal shutdown). Use these when you need a hard guarantee.</li>
</ul>

<h4>Clock domains</h4>
<ul>
  <li><b>GFXCLK</b> — Graphics/shader engine core clock.</li>
  <li><b>SOCCLK</b> — System-on-chip infrastructure clock (command processor, display
      controller fabric, etc.).</li>
  <li><b>UCLK</b> — Unified memory controller clock (directly determines VRAM bandwidth).</li>
  <li><b>FCLK</b> — Data fabric clock (interconnect between GFX, memory, and IO).</li>
  <li><b>DCLK0 / VCLK0</b> — Video decode and encode engine clocks.</li>
  <li><b>DISPCLK / DPPCLK</b> — Display pipe and pixel-processing clocks.</li>
  <li><b>DPREFCLK / DCFCLK / DTBCLK</b> — Display reference, display controller fabric, and
      display timing base clocks.</li>
</ul>

<h4>PPT Limit</h4>
<p>Total board power cap in watts. When total power draw reaches this limit the SMU
throttles clocks to stay within budget. Shown at the bottom of the tab in its own
group box with its own <b>Current</b> label, spinbox (0–600 W), and <b>Set</b> button.</p>

<h4>UI elements in each cell</h4>
<ul>
  <li><b>Current value label</b> (top of each cell) — The frequency the SMU currently reports
      for that limit direction (min or max). Updated on Refresh.</li>
  <li><b>QSpinBox</b> (bottom-left) — Enter the desired frequency in MHz. The value 0
      (displayed as "—") means "skip, don't send this command".</li>
  <li><b>Set button</b> (bottom-right of each cell) — Sends the
      <code>Set{Soft,Hard}{Min,Max}ByFreq</code> SMU message for that specific clock and
      limit type only. Other cells are not affected.</li>
  <li><b>Refresh button</b> (bottom of tab) — Re-reads all SMU state and updates every
      current value label.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Setting SoftMin &gt; SoftMax or HardMin &gt; HardMax may be silently rejected or cause
      instability.</li>
  <li>The "current value" for SoftMin and HardMin both show the same reported minimum
      frequency — the SMU reports only one min and one max per clock regardless of which
      limit type originally set it. Same applies to SoftMax/HardMax.</li>
  <li>Setting a very high HardMax does not guarantee the GPU will reach it. Thermal and
      power limits still apply.</li>
  <li>GFXCLK changes take effect immediately. Some display clocks may only change on the
      next mode switch.</li>
  <li>Enter 0 (shown as "—") to skip a row without sending anything.</li>
  <li>Display-related clocks (DISPCLK, DPPCLK, DPREFCLK, DCFCLK, DTBCLK) affect
      monitor output. Setting them incorrectly can cause a black screen until the next
      mode switch or driver reload.</li>
  <li>UCLK and FCLK are tightly coupled on most RDNA4 GPUs. Changing one without the
      other may put the memory subsystem into a sub-optimal ratio.</li>
</ul>
"""
        clock_w = QWidget()
        clock_lay = QVBoxLayout(clock_w)
        _add_cheatsheet_btn(clock_lay, "Clock Limits", _CLOCK_CHEATSHEET)

        _LIMIT_TYPES = ["SoftMin", "SoftMax", "HardMin", "HardMax"]
        _FREQ_MSG = {
            "SoftMin": PPSMC.SetSoftMinByFreq,
            "SoftMax": PPSMC.SetSoftMaxByFreq,
            "HardMin": PPSMC.SetHardMinByFreq,
            "HardMax": PPSMC.SetHardMaxByFreq,
        }

        def _mk_freq_apply(clk_id, msg_id, spin, cn, lt):
            def fn(hw):
                v = spin.value()
                if v > 0:
                    hw["smu"].send_msg(msg_id, ((clk_id & 0xFFFF) << 16) | (v & 0xFFFF))
                    self._log(f"SMU: {cn} {lt} = {v} MHz")
                else:
                    self._log(f"SMU: {cn} {lt} skipped (0)")
            return fn

        _SMU_CLK_DOMAINS = [
            (PPCLK.GFXCLK,   "GFXCLK",   5000),
            (PPCLK.SOCCLK,   "SOCCLK",   3000),
            (PPCLK.UCLK,     "UCLK",     3000),
            (PPCLK.FCLK,     "FCLK",     3000),
            (PPCLK.DCLK0,    "DCLK0",    3000),
            (PPCLK.VCLK0,    "VCLK0",    3000),
            (PPCLK.DISPCLK,  "DISPCLK",  3000),
            (PPCLK.DPPCLK,   "DPPCLK",   3000),
            (PPCLK.DPREFCLK, "DPREFCLK", 3000),
            (PPCLK.DCFCLK,   "DCFCLK",   3000),
            (PPCLK.DTBCLK,   "DTBCLK",   3000),
        ]

        self._clock_limits_cv = {}

        clock_tbl = QTableWidget()
        clock_tbl.setColumnCount(1 + len(_LIMIT_TYPES))
        clock_tbl.setHorizontalHeaderLabels(["Clock"] + _LIMIT_TYPES)
        clock_tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        for ci in range(1, 1 + len(_LIMIT_TYPES)):
            clock_tbl.horizontalHeader().setSectionResizeMode(
                ci, QHeaderView.ResizeMode.Stretch)
        clock_tbl.verticalHeader().setVisible(False)
        clock_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        clock_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        for _clk_id, clk_name, max_mhz in _SMU_CLK_DOMAINS:
            row = clock_tbl.rowCount()
            clock_tbl.insertRow(row)
            name_item = QTableWidgetItem(clk_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            clock_tbl.setItem(row, 0, name_item)

            for col_idx, lt in enumerate(_LIMIT_TYPES, start=1):
                key = f"SMU_{clk_name}_{lt}"
                smu_suffix = "min" if lt.endswith("Min") else "max"
                smu_key = f"smu_freq_{clk_name}_{smu_suffix}"

                cell = QWidget()
                cell_lay = QVBoxLayout(cell)
                cell_lay.setContentsMargins(4, 2, 4, 2)
                cell_lay.setSpacing(2)

                cv_label = QLabel("—")
                cv_label.setToolTip(f"Live value from {smu_key}")
                cell_lay.addWidget(cv_label)

                input_row = QHBoxLayout()
                input_row.setSpacing(2)
                spin = QSpinBox()
                spin.setRange(0, max_mhz)
                spin.setValue(0)
                spin.setSpecialValueText("—")
                spin.setSuffix(" MHz")
                input_row.addWidget(spin)

                set_btn = QPushButton("Set")
                set_btn.setMaximumWidth(40)
                _fn = _mk_freq_apply(_clk_id, _FREQ_MSG[lt], spin, clk_name, lt)
                set_btn.clicked.connect(
                    lambda checked=False, fn=_fn, lbl=f"{clk_name} {lt}":
                        self._run_with_hardware(f"Set {lbl}", fn, require_scan=False))
                input_row.addWidget(set_btn)
                cell_lay.addLayout(input_row)

                clock_tbl.setCellWidget(row, col_idx, cell)
                self._clock_limits_cv[key] = (cv_label, smu_key)
                self._detailed_param_widgets[key] = spin

        clock_tbl.resizeRowsToContents()
        clock_lay.addWidget(clock_tbl)

        # PPT Limit (single value, not per-limit-type)
        ppt_box = QGroupBox("PPT Limit")
        ppt_lay = QHBoxLayout(ppt_box)
        ppt_cv_label = QLabel("—")
        ppt_lay.addWidget(QLabel("Current:"))
        ppt_lay.addWidget(ppt_cv_label)
        smu_ppt_spin = QSpinBox()
        smu_ppt_spin.setRange(0, 600)
        smu_ppt_spin.setValue(0)
        smu_ppt_spin.setSpecialValueText("—")
        smu_ppt_spin.setSuffix(" W")
        ppt_lay.addWidget(smu_ppt_spin)

        def _mk_ppt_apply(spin):
            def fn(hw):
                v = spin.value()
                if v > 0:
                    hw["smu"].set_ppt_limit(v)
                    self._log(f"SMU: PPT Limit = {v} W")
            return fn

        ppt_set_btn = QPushButton("Set")
        ppt_set_btn.setMaximumWidth(50)
        ppt_set_btn.clicked.connect(
            lambda checked=False, fn=_mk_ppt_apply(smu_ppt_spin):
                self._run_with_hardware("Set PPT Limit", fn, require_scan=False))
        ppt_lay.addWidget(ppt_set_btn)
        ppt_lay.addStretch()
        clock_lay.addWidget(ppt_box)

        self._clock_limits_cv["SMU_PptLimit"] = (ppt_cv_label, "smu_ppt")
        self._detailed_param_widgets["SMU_PptLimit"] = smu_ppt_spin

        _add_refresh_btn(clock_lay)
        clock_scroll = QScrollArea()
        clock_scroll.setWidgetResizable(True)
        clock_scroll.setWidget(clock_w)
        self._smu_inner_tabs.addTab(clock_scroll, "Clock Limits")

        # ==================================================================
        # Sub-tab 3: Controls
        # ==================================================================
        _CONTROLS_CHEATSHEET = """
<h3>Controls — Individual SMU commands</h3>
<p>Each row sends one specific command to the SMU when you click its <b>Set</b> button.
Nothing is sent until you explicitly click — there is no "Apply All".</p>

<h4>What each control does</h4>
<ul>
  <li><b>GFX Off</b> — When checked, sends <code>DisallowGfxOff</code> which prevents the GPU
      from entering deep idle power-gating (GfxOff). Useful during benchmarking to avoid
      wake-up latency spikes. Unchecked sends <code>AllowGfxOff</code> (default behavior).</li>
  <li><b>GFX DCS</b> — Dynamic Clock Spreading. When checked, allows the SMU to
      slightly spread clock transitions to reduce electromagnetic interference (EMI).
      Unchecked disables it.</li>
  <li><b>Workload Profile</b> — Tells the SMU which DPM heuristic to use:
    <ul>
      <li><i>Default</i> — balanced general-purpose behavior.</li>
      <li><i>3D Fullscreen</i> — prefers higher clocks, lower latency.</li>
      <li><i>PowerSave</i> — aggressively drops clocks when idle.</li>
      <li><i>Video</i> — optimized for media decode power efficiency.</li>
      <li><i>VR</i> — low-latency, high sustained clocks.</li>
      <li><i>Compute</i> — holds clocks high, disables some display power saving.</li>
      <li><i>Custom / Window3D</i> — vendor-specific variants.</li>
    </ul></li>
  <li><b>Throttler Mask</b> — A hex bitmask controlling which throttling sources are active.
      Each bit corresponds to a different throttler (thermal, power, current, etc.).
      Setting this to <code>0x0000</code> disables all throttlers.</li>
  <li><b>Temp Input Select</b> — Selects which temperature sensor (0–15) the SMU uses as
      its primary input for thermal throttling decisions (0 = Edge, 1 = Hotspot,
      higher values = VRAM, VR SoC, etc.).</li>
  <li><b>FW D-states Mask</b> — Controls which firmware power states (D-states) are allowed.
      Each bit enables a different low-power idle sub-state.</li>
  <li><b>DCS Architecture</b> — Selects the Dynamic Clock Spreading implementation:
      0 = Disabled, 1 = Async, 2 = Sync.</li>
</ul>

<h4>Table columns</h4>
<ul>
  <li><b>Human name</b> — Friendly label for the control.</li>
  <li><b>Table key</b> — Internal identifier used by the engine (e.g.
      <code>SMU_DisallowGfxOff</code>). Useful for cross-referencing logs.</li>
  <li><b>Unit</b> — Measurement unit if applicable (usually blank for toggles).</li>
  <li><b>Current value</b> — Last-known value read from hardware (where readable).
      Shows "—" for write-only commands.</li>
  <li><b>Custom input</b> — The widget you interact with (checkbox, combo box, or
      hex spinbox depending on the row).</li>
  <li><b>Set</b> — Per-row apply button. Sends only this one command to the SMU.</li>
</ul>

<h4>SMU messages sent (reference)</h4>
<ul>
  <li>GFX Off &rarr; <code>AllowGfxOff</code> / <code>DisallowGfxOff</code></li>
  <li>GFX DCS &rarr; <code>AllowGfxDcs</code> / <code>DisallowGfxDcs</code></li>
  <li>Workload Profile &rarr; <code>SetWorkloadMask</code></li>
  <li>Throttler Mask &rarr; <code>SetThrottlerMask</code></li>
  <li>Temp Input Select &rarr; <code>SetTemperatureInputSelect</code></li>
  <li>FW D-states Mask &rarr; <code>SetFwDstatesMask</code></li>
  <li>DCS Architecture &rarr; <code>SetDcsArch</code></li>
</ul>

<h4>Caveats</h4>
<ul>
  <li><span style="color: #c00;"><b>Setting Throttler Mask to 0x0000 disables ALL thermal and
      power protection.</b></span> The GPU can physically damage itself under load without
      these safeguards. Use with extreme caution.</li>
  <li>Workload profile changes are advisory — the SMU may still throttle if thermal limits
      are hit regardless of profile.</li>
  <li>GfxOff disallow does not persist across driver reloads or system sleep.</li>
  <li>Setting "no change" (-1) in a spinbox skips that command entirely.</li>
  <li>DCS Architecture and GFX DCS are related but separate: DCS toggles the feature on/off,
      while DCS Architecture selects which spreading algorithm is used when DCS is on.</li>
  <li>Temp Input Select values beyond the number of physical sensors on your GPU are
      invalid and will be silently ignored.</li>
</ul>
"""
        ctrl_w = QWidget()
        ctrl_lay = QVBoxLayout(ctrl_w)
        _add_cheatsheet_btn(ctrl_lay, "Controls", _CONTROLS_CHEATSHEET)
        ctrl_tbl = _make_smu_table(with_set_col=True)
        self._detailed_tables["SMU_Controls"] = ctrl_tbl

        def _mk_gfxoff_apply(cb):
            def fn(hw):
                checked = cb.isChecked()
                _log_to_file(f"_mk_gfxoff_apply: checked={checked}")
                if checked:
                    hw["smu"].disallow_gfx_off()
                    self._log("SMU: DisallowGfxOff")
                else:
                    hw["smu"].allow_gfx_off()
                    self._log("SMU: AllowGfxOff")
            return fn

        det_gfxoff = QCheckBox()
        det_gfxoff.setChecked(True)
        det_gfxoff.setToolTip("Checked = DisallowGfxOff (prevents idle power gate)")
        _add_smu_row(ctrl_tbl, "GFX Off", "SMU_DisallowGfxOff", "", None, det_gfxoff,
                     row_apply_fn=_mk_gfxoff_apply(det_gfxoff))

        def _mk_dcs_apply(cb):
            def fn(hw):
                if cb.isChecked():
                    hw["smu"].send_msg(PPSMC.AllowGfxDcs)
                    self._log("SMU: AllowGfxDcs")
                else:
                    hw["smu"].send_msg(PPSMC.DisallowGfxDcs)
                    self._log("SMU: DisallowGfxDcs")
            return fn

        det_dcs = QCheckBox()
        det_dcs.setChecked(False)
        det_dcs.setToolTip("Checked = AllowGfxDcs")
        _add_smu_row(ctrl_tbl, "GFX DCS", "SMU_GfxDcs", "", None, det_dcs,
                     row_apply_fn=_mk_dcs_apply(det_dcs))

        def _mk_workload_apply(combo):
            def fn(hw):
                v = combo.currentData()
                if v is not None and v >= 0:
                    hw["smu"].set_workload_mask(v)
                    self._log(f"SMU: Workload profile = {v} ({combo.currentText()})")
            return fn

        smu_workload = QComboBox()
        _WORKLOAD_PROFILES = [
            "Default", "3D Fullscreen", "PowerSave", "Video",
            "VR", "Compute", "Custom", "Window3D",
        ]
        for i, name in enumerate(_WORKLOAD_PROFILES):
            smu_workload.addItem(f"{i} \u2013 {name}", i)
        smu_workload.setCurrentIndex(0)
        _add_smu_row(ctrl_tbl, "Workload Profile", "SMU_Workload", "", None, smu_workload,
                     row_apply_fn=_mk_workload_apply(smu_workload))

        def _mk_throttler_apply(spin):
            def fn(hw):
                v = spin.value()
                if v >= 0:
                    hw["smu"].send_msg(PPSMC.SetThrottlerMask, v)
                    self._log(f"SMU: Throttler mask = 0x{v:04X}")
            return fn

        smu_throttler = QSpinBox()
        smu_throttler.setRange(-1, 0xFFFF)
        smu_throttler.setValue(-1)
        smu_throttler.setSpecialValueText("no change")
        smu_throttler.setDisplayIntegerBase(16)
        smu_throttler.setPrefix("0x")
        _add_smu_row(ctrl_tbl, "Throttler Mask", "SMU_ThrottlerMask", "", None, smu_throttler,
                     row_apply_fn=_mk_throttler_apply(smu_throttler))

        def _mk_tempinput_apply(spin):
            def fn(hw):
                v = spin.value()
                if v >= 0:
                    hw["smu"].send_msg(PPSMC.SetTemperatureInputSelect, v)
                    self._log(f"SMU: Temp input select = {v}")
            return fn

        smu_temp_input = QSpinBox()
        smu_temp_input.setRange(-1, 15)
        smu_temp_input.setValue(-1)
        smu_temp_input.setSpecialValueText("no change")
        _add_smu_row(ctrl_tbl, "Temp Input Select", "SMU_TempInputSelect", "", None, smu_temp_input,
                     row_apply_fn=_mk_tempinput_apply(smu_temp_input))

        def _mk_fwdstates_apply(spin):
            def fn(hw):
                v = spin.value()
                if v >= 0:
                    hw["smu"].send_msg(PPSMC.SetFwDstatesMask, v)
                    self._log(f"SMU: FW Dstates mask = 0x{v:04X}")
            return fn

        smu_fwdstates = QSpinBox()
        smu_fwdstates.setRange(-1, 0xFFFF)
        smu_fwdstates.setValue(-1)
        smu_fwdstates.setSpecialValueText("no change")
        smu_fwdstates.setDisplayIntegerBase(16)
        smu_fwdstates.setPrefix("0x")
        _add_smu_row(ctrl_tbl, "FW D-states Mask", "SMU_FwDstatesMask", "", None, smu_fwdstates,
                     row_apply_fn=_mk_fwdstates_apply(smu_fwdstates))

        def _mk_dcsarch_apply(combo):
            def fn(hw):
                v = combo.currentData()
                if v is not None and v >= 0:
                    hw["smu"].send_msg(PPSMC.SetDcsArch, v)
                    self._log(f"SMU: DCS Arch = {v} ({combo.currentText()})")
            return fn

        smu_dcsarch = QComboBox()
        for val, name in [(0, "Disabled"), (1, "Async"), (2, "Sync")]:
            smu_dcsarch.addItem(f"{val} \u2013 {name}", val)
        smu_dcsarch.setCurrentIndex(0)
        _add_smu_row(ctrl_tbl, "DCS Architecture", "SMU_DcsArch", "", None, smu_dcsarch,
                     row_apply_fn=_mk_dcsarch_apply(smu_dcsarch))

        ctrl_lay.addWidget(ctrl_tbl)
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setWidget(ctrl_w)
        self._smu_inner_tabs.addTab(ctrl_scroll, "Controls")

        # ==================================================================
        # Sub-tab 4: Features (per-bit toggle with individual Set)
        # ==================================================================
        _FEATURES_CHEATSHEET = """
<h3>Features — Per-bit SMU feature toggles</h3>
<p>The SMU firmware maintains a 64-bit register where each bit enables or disables one
hardware feature. This tab gives you granular control over every individual bit.</p>

<h4>How it works</h4>
<ul>
  <li><b>Current state</b> — Shows whether the SMU currently has this feature ON or OFF
      (read from hardware on Refresh).</li>
  <li><b>Toggle checkbox</b> — Set the desired state. Nothing is sent until you click the
      row's <b>Set</b> button.</li>
  <li>Each <b>Set</b> button only toggles that one bit. There is no "Apply All" to prevent
      accidentally flipping features you didn't intend to change.</li>
</ul>

<h4>Complete feature reference</h4>
<p><b>Low word (bits 0–31):</b></p>
<ul>
  <li><b>0 — FW_DATA_READ</b> — Allows SMU firmware to read data tables from DRAM. Required
      for metrics, PPTable reads, and most telemetry. Do not disable.</li>
  <li><b>1 — DPM_GFXCLK</b> — Dynamic Power Management for the graphics core clock. When ON
      the GPU scales GFXCLK up/down with load. OFF locks it at its current level.</li>
  <li><b>2 — DPM_GFX_POWER_OPTIMIZER</b> — Additional power-aware optimizer that fine-tunes
      GFX voltage/frequency operating points beyond basic DPM stepping.</li>
  <li><b>3 — DPM_UCLK</b> — DPM for memory controller clock. OFF locks VRAM speed.</li>
  <li><b>4 — DPM_FCLK</b> — DPM for data-fabric clock. OFF locks the interconnect speed.</li>
  <li><b>5 — DPM_SOCCLK</b> — DPM for SoC infrastructure clock.</li>
  <li><b>6 — DPM_LINK</b> — DPM for PCIe link speed/width. OFF prevents link-speed downshift
      to save power at idle.</li>
  <li><b>7 — DPM_DCN</b> — DPM for the display controller (DCN). OFF locks display clocks.</li>
  <li><b>8 — VMEMP_SCALING</b> — Memory power-supply voltage scaling. Allows the SMU to
      reduce VRAM voltage at low clock speeds.</li>
  <li><b>9 — VDDIO_MEM_SCALING</b> — Memory I/O voltage scaling. Same concept for the
      VDDIO rail feeding the memory PHY.</li>
  <li><b>10 — DS_GFXCLK</b> — Deep-sleep for GFXCLK. Allows the clock to gate entirely
      when idle (lowest power). OFF keeps it ticking.</li>
  <li><b>11 — DS_SOCCLK</b> — Deep-sleep for SOCCLK.</li>
  <li><b>12 — DS_FCLK</b> — Deep-sleep for FCLK.</li>
  <li><b>13 — DS_LCLK</b> — Deep-sleep for LCLK (PCIe reference clock domain).</li>
  <li><b>14 — DS_DCFCLK</b> — Deep-sleep for display controller fabric clock.</li>
  <li><b>15 — DS_UCLK</b> — Deep-sleep for memory controller clock.</li>
  <li><b>16 — GFX_ULV</b> — Ultra-Low Voltage mode for GFX. Allows the GPU to drop to a
      very low voltage/frequency point at near-zero load.</li>
  <li><b>17 — FW_DSTATE</b> — Firmware D-state management. Allows the SMU to enter
      low-power firmware states between workloads.</li>
  <li><b>18 — GFXOFF</b> — GfxOff power gating. Allows the entire graphics engine to power
      down when idle. Saves significant power but adds wake-up latency.</li>
  <li><b>19 — BACO</b> — Bus Active, Chip Off. A deep idle state where most of the GPU
      is powered down but PCIe stays alive.</li>
  <li><b>20 — MM_DPM</b> — DPM for multimedia engines (VCN/JPEG). OFF locks video
      encode/decode clocks.</li>
  <li><b>21 — SOC_MPCLK_DS</b> — Deep-sleep for the SoC MP (management processor) clock.</li>
  <li><b>22 — BACO_MPCLK_DS</b> — Deep-sleep for the MP clock specifically during BACO.</li>
  <li><b>23 — THROTTLERS</b> — Master switch for all throttling logic. OFF disables every
      throttler (thermal, power, current). <span style="color:#c00;">Extremely
      dangerous.</span></li>
  <li><b>24 — SMARTSHIFT</b> — AMD SmartShift (laptop dynamic power sharing between CPU
      and GPU). Irrelevant on desktops.</li>
  <li><b>25 — GTHR</b> — GPU Thermal Headroom reporting. Exposes how close the GPU is
      to its thermal limit.</li>
  <li><b>26 — ACDC</b> — AC/DC power-source detection. Allows the SMU to switch profiles
      between wall power and battery.</li>
  <li><b>27 — VR0HOT</b> — Voltage-regulator over-temperature protection. When the VRM
      reports overheating, the SMU throttles to reduce current draw.</li>
  <li><b>28 — FW_CTF</b> — <span style="color: #c00;"><b>Critical Thermal Fault handler.
      Permanently locked ON.</b></span> This is the GPU's last-resort emergency thermal
      shutdown. Cannot be disabled from this UI.</li>
  <li><b>29 — FAN_CONTROL</b> — SMU-managed fan curve. OFF hands fan control to the host
      driver or leaves it at the last-set duty cycle.</li>
  <li><b>30 — GFX_DCS</b> — Dynamic Clock Spreading for GFX. Reduces EMI by slightly
      modulating the clock edge timing.</li>
  <li><b>31 — GFX_READ_MARGIN</b> — Read-margin adjustment for GFX SRAM. Internal
      reliability feature, leave ON.</li>
</ul>

<p><b>High word (bits 32–56):</b></p>
<ul>
  <li><b>32 — LED_DISPLAY</b> — Controls the GPU's onboard LED/RGB lighting via SMU.</li>
  <li><b>33 — GFXCLK_SPREAD_SPECTRUM</b> — Spread-spectrum clocking for GFXCLK to reduce
      EMI. Turning OFF gives a slightly cleaner clock but may violate EMI compliance.</li>
  <li><b>34 — OUT_OF_BAND_MONITOR</b> — Out-of-band telemetry monitoring (BMC/IPMI
      sideband reporting on server GPUs).</li>
  <li><b>35 — OPTIMIZED_VMIN</b> — Per-part optimized minimum-voltage calibration. Allows
      the SMU to run lower voltage than the generic table for parts that pass testing.</li>
  <li><b>36 — GFX_IMU</b> — GFX Integrated Management Unit. Internal co-processor that
      handles fast voltage/frequency transitions.</li>
  <li><b>37 — BOOT_TIME_CAL</b> — Boot-time silicon calibration. Runs once at power-on to
      characterize the specific die. Disabling skips calibration (not recommended).</li>
  <li><b>38 — GFX_PCC_DFLL</b> — GFX Precision Clock Controller with Digital Frequency-Locked
      Loop. Enables fine-grained clock regulation.</li>
  <li><b>39 — SOC_CG</b> — SoC clock gating. Allows unused SoC blocks to gate their clocks
      for power savings.</li>
  <li><b>40 — DF_CSTATE</b> — Data Fabric C-state. Allows the interconnect fabric to enter
      low-power idle states.</li>
  <li><b>41 — GFX_EDC</b> — Electrical Design Current protection for GFX. Throttles when
      instantaneous current spikes approach the VRM design limit.</li>
  <li><b>42 — BOOT_POWER_OPT</b> — Boot-time power optimization. Reduces power during
      driver initialization.</li>
  <li><b>43 — CLOCK_POWER_DOWN_BYPASS</b> — Bypasses clock power-down sequencing. Internal
      debug feature.</li>
  <li><b>44 — DS_VCN</b> — Deep-sleep for the Video Core Next (VCN) encode/decode engine.</li>
  <li><b>45 — BACO_CG</b> — Clock gating specifically during BACO state.</li>
  <li><b>46 — MEM_TEMP_READ</b> — Enables the SMU to read VRAM temperature sensors.</li>
  <li><b>47 — ATHUB_MMHUB_PG</b> — Power gating for the Address Translation Hub and
      Multimedia Hub. Saves idle power but adds latency on first access after gate.</li>
  <li><b>48 — SOC_PCC</b> — SoC Precision Clock Controller. Fine clock regulation for
      SoC domain.</li>
  <li><b>49 — EDC_PWRBRK</b> — EDC Power-Break. An emergency current-limiting mechanism that
      aggressively throttles when instantaneous power spikes are detected.</li>
  <li><b>50 — SOC_EDC_XVMIN</b> — SoC EDC with cross-voltage-minimum awareness.</li>
  <li><b>51 — GFX_PSM_DIDT</b> — GFX Power State Machine di/dt (current-slew-rate)
      protection. Limits how fast current can ramp to protect the power delivery.</li>
  <li><b>52 — APT_ALL_ENABLE</b> — Adaptive Power Tuning master enable.</li>
  <li><b>53 — APT_SQ_THROTTLE</b> — APT Shader Queue throttle — dynamically reduces shader
      workload to stay within power/thermal budgets.</li>
  <li><b>54 — APT_PF_DCS</b> — APT Power-Filtered Dynamic Clock Spreading.</li>
  <li><b>55 — GFX_EDC_XVMIN</b> — GFX EDC with cross-voltage-minimum awareness.</li>
  <li><b>56 — GFX_DIDT_XVMIN</b> — GFX di/dt with cross-voltage-minimum awareness.</li>
</ul>
<p>Bits 57–63 are SPARE (reserved/unused) and are not shown in the table.</p>

<h4>Table columns</h4>
<ul>
  <li><b>Bit</b> — Bit position in the 64-bit feature register (0–63).</li>
  <li><b>Name</b> — Human-readable feature name from the SMU firmware header.</li>
  <li><b>Current state</b> — ON or OFF as last read from hardware.</li>
  <li><b>Toggle</b> — Checkbox to set your desired state. Greyed out for FW_CTF.</li>
  <li><b>Set</b> — Sends <code>EnableSmuFeaturesLow/High</code> or
      <code>DisableSmuFeaturesLow/High</code> for this one bit only.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Disabling DPM features locks clocks at their current level. If you disable DPM_GFXCLK
      while the GPU is idle, you may be stuck at a low clock until you re-enable it.</li>
  <li>Disabling thermal/protection features (THROTTLERS, VR0HOT, GFX_EDC, EDC_PWRBRK)
      removes hardware protection. The GPU can overheat or overdraw current.</li>
  <li>Some features have hidden dependencies — disabling a parent feature may silently
      force-disable its children.</li>
  <li>Changes take effect immediately but <b>do not persist</b> across driver reloads or
      system reboots.</li>
  <li>Enabling a feature the SMU firmware doesn't support on your specific GPU may be
      silently ignored or return an error code.</li>
  <li>The ACDC and SMARTSHIFT features are laptop-oriented and have no effect on desktop
      GPUs.</li>
  <li>Disabling FW_DATA_READ (bit 0) will break metrics, table reads, and most telemetry.
      Re-enabling it may require a driver reload.</li>
</ul>
"""
        feat_w = QWidget()
        feat_lay = QVBoxLayout(feat_w)
        _add_cheatsheet_btn(feat_lay, "Features", _FEATURES_CHEATSHEET)

        feat_tbl = QTableWidget()
        feat_tbl.setColumnCount(5)
        feat_tbl.setHorizontalHeaderLabels(["Bit", "Name", "Current state", "Toggle", "Set"])
        feat_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        feat_tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        feat_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        feat_tbl.verticalHeader().setVisible(False)

        self._smu_feature_state_labels = {}

        def _mk_feature_apply(bit, cb):
            fname = _FEATURE_NAMES.get(bit, f"BIT_{bit}")
            def fn(hw):
                smu = hw["smu"]
                if bit < 32:
                    mask = 1 << bit
                    if cb.isChecked():
                        smu.enable_features_low(mask)
                    else:
                        smu.disable_features_low(mask)
                else:
                    mask = 1 << (bit - 32)
                    if cb.isChecked():
                        smu.enable_features_high(mask)
                    else:
                        smu.disable_features_high(mask)
                action = "Enabled" if cb.isChecked() else "Disabled"
                self._log(f"SMU: {action} feature {fname} (bit {bit})")
            return fn

        for bit in range(64):
            fname = _FEATURE_NAMES.get(bit, None)
            if fname is None or fname.startswith("SPARE"):
                continue

            row = feat_tbl.rowCount()
            feat_tbl.insertRow(row)

            bit_item = QTableWidgetItem(str(bit))
            bit_item.setFlags(bit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            feat_tbl.setItem(row, 0, bit_item)

            name_item = QTableWidgetItem(fname)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            feat_tbl.setItem(row, 1, name_item)

            state_label = QLabel("—")
            state_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            feat_tbl.setCellWidget(row, 2, state_label)
            self._smu_feature_state_labels[bit] = state_label

            cb = QCheckBox()
            cb.setChecked(False)
            if bit == SMU_FEATURE.FW_CTF:
                cb.setEnabled(False)
                cb.setToolTip("Critical thermal fault handler — cannot be disabled")
            else:
                cb.setToolTip(f"Bit {bit}: enable/disable {fname}")
            feat_tbl.setCellWidget(row, 3, cb)
            self._detailed_param_widgets[f"SMU_FEAT_{bit}"] = cb

            set_btn = QPushButton("Set")
            set_btn.setMaximumWidth(50)
            _fn = _mk_feature_apply(bit, cb)
            set_btn.clicked.connect(
                lambda checked=False, fn=_fn, lbl=fname:
                    self._run_with_hardware(f"Set {lbl}", fn, require_scan=False))
            feat_tbl.setCellWidget(row, 4, set_btn)

        feat_tbl.resizeRowsToContents()
        feat_lay.addWidget(feat_tbl)
        _add_refresh_btn(feat_lay)
        feat_scroll = QScrollArea()
        feat_scroll.setWidgetResizable(True)
        feat_scroll.setWidget(feat_w)
        self._smu_inner_tabs.addTab(feat_scroll, "Features")

        # ==================================================================
        # Sub-tab 5: Tables (live metrics + other SMU tables)
        # ==================================================================
        _TABLES_CHEATSHEET = """
<h3>Tables — Live metrics &amp; raw SMU table dumps</h3>
<p>This tab reads raw data tables from the SMU's DMA buffer. The top section shows
parsed live performance metrics; the bottom section lets you dump raw tables as hex.</p>

<h4>All metrics sections</h4>
<p>The metrics table is organized into sections. Every field from the
<code>SmuMetrics_t</code> struct is displayed:</p>

<p><b>Current Clocks (MHz)</b></p>
<ul>
  <li><b>CurrClock_GFXCLK / SOCCLK / UCLK / FCLK / DCLK0 / VCLK0 / DISPCLK / DPPCLK /
      DPREFCLK / DCFCLK / DTBCLK</b> — Real-time frequency of each clock domain. This is
      what the hardware is actually running at right now, not the requested target.</li>
</ul>

<p><b>Power</b></p>
<ul>
  <li><b>AverageSocketPower</b> — Average power draw from the GPU die only (watts).</li>
  <li><b>AverageTotalBoardPower</b> — Average total board power including VRMs, memory, and
      aux circuits (watts).</li>
  <li><b>dGPU_W_MAX</b> — Maximum instantaneous power draw seen during the sampling window.</li>
  <li><b>EnergyAccumulator</b> — Cumulative energy counter (increments over time). Useful for
      computing average power over custom intervals.</li>
</ul>

<p><b>Voltage (mV)</b></p>
<ul>
  <li><b>AvgVoltage_VDD_GFX</b> — Graphics core voltage rail.</li>
  <li><b>AvgVoltage_VDD_SOC</b> — SoC logic voltage rail.</li>
  <li><b>AvgVoltage_VDDCI_MEM</b> — Memory controller I/O voltage.</li>
  <li><b>AvgVoltage_VDDIO_MEM</b> — Memory PHY I/O voltage.</li>
</ul>

<p><b>Current (mA)</b></p>
<ul>
  <li><b>AvgCurrent_VDD_GFX / VDD_SOC / VDDCI_MEM / VDDIO_MEM</b> — Averaged current draw
      for each voltage rail. Multiply with voltage to get per-rail power.</li>
</ul>

<p><b>Activity (%)</b></p>
<ul>
  <li><b>AverageGfxActivity</b> — GPU shader/compute utilization (0–100%).</li>
  <li><b>AverageUclkActivity</b> — Memory controller utilization.</li>
  <li><b>AverageVcn0ActivityPercentage</b> — VCN0 (video encode/decode engine 0) utilization.</li>
  <li><b>Vcn1ActivityPercentage</b> — VCN1 (second video engine, if present) utilization.</li>
</ul>

<p><b>Fan</b></p>
<ul>
  <li><b>AvgFanPwm</b> — Fan duty cycle as a percentage (0–100).</li>
  <li><b>AvgFanRpm</b> — Fan speed in RPM. 0 when fan-stop is active.</li>
</ul>

<p><b>Temperature</b></p>
<ul>
  <li><b>AvgTemperature_Edge</b> — Die-edge temperature sensor (&deg;C).</li>
  <li><b>AvgTemperature_Hotspot / Hotspot_GFX / Hotspot_SOC</b> — Peak junction temps.
      Hotspot is the overall max; GFX and SOC break it down by domain.</li>
  <li><b>AvgTemperature_Mem</b> — VRAM temperature.</li>
  <li><b>AvgTemperature_VR_GFX / VR_SOC / VR_Mem0 / VR_Mem1</b> — Voltage regulator
      temperatures for each power rail.</li>
  <li><b>AvgTemperature_Liquid0 / Liquid1</b> — Liquid cooling loop sensors (if equipped).</li>
  <li><b>AvgTemperature_PLX</b> — PLX/PCIe switch temperature (multi-GPU boards).</li>
  <li><b>AvgTemperatureFanIntake</b> — Ambient air temperature at the fan intake.</li>
</ul>

<p><b>PCIe</b></p>
<ul>
  <li><b>PcieRate</b> — Current PCIe generation (1 = Gen1, 2 = Gen2, … 5 = Gen5).</li>
  <li><b>PcieWidth</b> — Current PCIe lane width (1, 2, 4, 8, 16).</li>
</ul>

<p><b>Throttling (%)</b></p>
<ul>
  <li><b>ThrottlingPercentage_0 through _20</b> — Per-throttler activity. Each index maps to
      a specific throttler (thermal, power, current, etc.). 0 = not throttling, 100 = fully
      throttled. The 21 indices correspond to the SMU's internal throttler array.</li>
  <li><b>VmaxThrottlingPercentage</b> — How much the maximum-voltage limiter is restricting
      clocks.</li>
</ul>

<p><b>Average Frequencies (MHz)</b></p>
<ul>
  <li><b>AverageGfxclkFrequencyTarget</b> — The clock the SMU is targeting for GFX.</li>
  <li><b>AverageGfxclkFrequencyPreDs / PostDs</b> — GFX clock before and after deep-sleep
      gating is applied. PreDs &ge; PostDs; the difference shows how much time is spent
      in deep-sleep.</li>
  <li><b>AverageFclkFrequencyPreDs / PostDs</b> — Same for FCLK.</li>
  <li><b>AverageMemclkFrequencyPreDs / PostDs</b> — Same for memory clock.</li>
  <li><b>AverageVclk0Frequency / AverageDclk0Frequency</b> — Average video encode/decode
      clock 0.</li>
  <li><b>AverageVclk1Frequency / AverageDclk1Frequency</b> — Same for engine 1.</li>
  <li><b>AveragePCIeBusy</b> — Average PCIe bus utilization.</li>
</ul>

<p><b>Moving Averages</b></p>
<ul>
  <li>Same metrics as Average Frequencies plus activity and power, but using a longer
      exponential-moving-average window. Useful for smoothing out transient spikes.</li>
  <li>Includes: <b>MovingAverageGfxclkFrequencyTarget</b>, <b>PreDs/PostDs</b> for GFXCLK,
      FCLK, MEMCLK; <b>MovingAverageVclk0/Dclk0Frequency</b>;
      <b>MovingAverageGfxActivity / UclkActivity / Vcn0Activity / PCIeBusy</b>;
      <b>MovingAverageUclkActivity_MAX</b>; <b>MovingAverageSocketPower</b>.</li>
</ul>

<p><b>D3Hot Counters</b></p>
<ul>
  <li>Entry/exit counters for each D3Hot sequence. Tracks how many times the GPU has entered
      and exited each low-power mode:</li>
  <li><b>BACO</b> — Bus Active Chip Off.</li>
  <li><b>MSR</b> — Modern Standby Resume.</li>
  <li><b>BAMACO</b> — Bus Active Memory Active Chip Off.</li>
  <li><b>ULPS</b> — Ultra Low Power State (display-related).</li>
  <li><b>ArmMsgReceived_*</b> — Counts of "arm" messages received per mode (internal
      handshake between host driver and SMU for D3 transitions).</li>
</ul>

<p><b>Misc</b></p>
<ul>
  <li><b>MetricsCounter</b> — How many times the SMU firmware has updated the metrics struct.
      Incrementing confirms the SMU is alive and sampling.</li>
  <li><b>ApuSTAPMSmartShiftLimit / ApuSTAPMLimit</b> — SmartShift/STAPM power limits
      (laptop-only, irrelevant on desktop).</li>
  <li><b>AvgApuSocketPower</b> — APU socket power (laptop-only).</li>
  <li><b>AverageUclkActivity_MAX</b> — Peak memory utilization seen in the window.</li>
  <li><b>PublicSerialNumberLower / Upper</b> — GPU die serial number (two 32-bit halves).</li>
</ul>

<h4>Other Tables (on demand)</h4>
<ul>
  <li><b>Read PPTable</b> (table id 0) — Raw hex dump of the power-play table stored in SMU
      SRAM. Contains DPM frequency/voltage curves, thermal limits, fan curves, and feature
      enable masks. The layout is GPU-generation-specific.</li>
  <li><b>Read Driver Info</b> (table id 10) — DPM frequency tables and driver state. Shows
      all DPM levels the SMU has configured for each clock domain.</li>
  <li><b>Read ECC Info</b> (table id 11) — Error-correction counters. Same data as the
      dedicated ECC tab. Shows correctable/uncorrectable error counts per memory
      partition.</li>
</ul>

<h4>UI controls</h4>
<ul>
  <li><b>Refresh Now</b> — One-shot read of the full SmuMetrics_t struct.</li>
  <li><b>Auto-refresh checkbox</b> — Enables a periodic timer.</li>
  <li><b>Interval spinbox (1–30 s)</b> — Sets the auto-refresh period. Adjustable while
      auto-refresh is running.</li>
  <li><b>Status label</b> — Shows last update timestamp and value count, or error text.</li>
  <li><b>Hex view</b> (bottom) — Read-only text area displaying raw hex dumps from the
      "Other Tables" buttons.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Metrics are sampled by the SMU firmware at its own internal rate, not at your refresh
      interval. Polling faster than ~2 s adds MMIO overhead with diminishing returns.</li>
  <li>Some metric values are <i>averaged</i> over the SMU's sampling window, not
      instantaneous snapshots. "Pre/PostDs" frequency differences reveal deep-sleep
      duty cycle, not instantaneous jitter.</li>
  <li>Raw table hex dumps require knowledge of the struct layout to interpret. The PPTable
      format is GPU-generation-specific.</li>
  <li>Reading tables while the GPU is under heavy load may briefly stall the SMU command
      interface.</li>
  <li>D3Hot counters and APU/STAPM fields are typically zero on desktop GPUs.</li>
  <li>ThrottlingPercentage indices are not named in the metrics struct; their mapping to
      specific throttlers (thermal, power, current, etc.) depends on SMU firmware version.</li>
  <li>PublicSerialNumber fields may be zero if the GPU vendor has not programmed a serial.</li>
</ul>
"""
        tables_w = QWidget()
        tables_lay = QVBoxLayout(tables_w)
        _add_cheatsheet_btn(tables_lay, "Tables", _TABLES_CHEATSHEET)

        # --- Live Metrics section ---
        metrics_header = QLabel("Live Metrics (SmuMetrics_t)")
        metrics_header.setStyleSheet("font-weight: bold; font-size: 10pt;")
        tables_lay.addWidget(metrics_header)

        metrics_ctrl_row = QHBoxLayout()
        self._smu_metrics_refresh_btn = QPushButton("Refresh Now")
        self._smu_metrics_refresh_btn.setToolTip(
            "Read full SmuMetrics_t from SMU DMA buffer")
        self._smu_metrics_refresh_btn.clicked.connect(self._on_smu_metrics_refresh)
        metrics_ctrl_row.addWidget(self._smu_metrics_refresh_btn)

        self._smu_metrics_auto_cb = QCheckBox("Auto-refresh")
        self._smu_metrics_auto_cb.setToolTip("Periodically read metrics from the SMU")
        self._smu_metrics_auto_cb.toggled.connect(self._on_smu_metrics_auto_toggle)
        metrics_ctrl_row.addWidget(self._smu_metrics_auto_cb)

        self._smu_metrics_interval_spin = QSpinBox()
        self._smu_metrics_interval_spin.setRange(1, 30)
        self._smu_metrics_interval_spin.setValue(2)
        self._smu_metrics_interval_spin.setSuffix(" s")
        self._smu_metrics_interval_spin.setToolTip("Auto-refresh interval in seconds")
        self._smu_metrics_interval_spin.valueChanged.connect(
            self._on_smu_metrics_interval_changed)
        metrics_ctrl_row.addWidget(self._smu_metrics_interval_spin)

        self._smu_metrics_status_label = QLabel("")
        self._smu_metrics_status_label.setStyleSheet("color: #888;")
        metrics_ctrl_row.addWidget(self._smu_metrics_status_label)

        metrics_ctrl_row.addStretch()
        tables_lay.addLayout(metrics_ctrl_row)

        self._smu_metrics_table = QTableWidget()
        self._smu_metrics_table.setColumnCount(2)
        self._smu_metrics_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self._smu_metrics_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._smu_metrics_table.horizontalHeader().setStretchLastSection(True)
        self._smu_metrics_table.verticalHeader().setVisible(False)
        self._smu_metrics_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        tables_lay.addWidget(self._smu_metrics_table)

        self._metrics_value_items: dict[str, QTableWidgetItem] = {}
        self._metrics_worker = None

        self._metrics_auto_timer = QTimer(self)
        self._metrics_auto_timer.timeout.connect(self._on_smu_metrics_timer_tick)

        self._init_metrics_table_rows()

        # --- Other Tables section ---
        other_header = QLabel("Other SMU Tables (on demand)")
        other_header.setStyleSheet(
            "font-weight: bold; font-size: 10pt; margin-top: 12px;")
        tables_lay.addWidget(other_header)

        other_btn_row = QHBoxLayout()
        self._smu_read_pptable_btn = QPushButton("Read PPTable")
        self._smu_read_pptable_btn.setToolTip(
            "TABLE_PPTABLE (id=0) — raw hex dump of the power-play table")
        self._smu_read_pptable_btn.clicked.connect(
            lambda: self._on_smu_read_other_table("PPTable", TABLE_PPTABLE))
        other_btn_row.addWidget(self._smu_read_pptable_btn)

        self._smu_read_driver_info_btn = QPushButton("Read Driver Info")
        self._smu_read_driver_info_btn.setToolTip(
            "TABLE_DRIVER_INFO (id=10) — DPM freq tables and driver state")
        self._smu_read_driver_info_btn.clicked.connect(
            lambda: self._on_smu_read_other_table("DriverInfo", TABLE_DRIVER_INFO))
        other_btn_row.addWidget(self._smu_read_driver_info_btn)

        self._smu_read_ecc_btn = QPushButton("Read ECC Info")
        self._smu_read_ecc_btn.setToolTip(
            "TABLE_ECCINFO (id=11) — same data as the ECC tab")
        self._smu_read_ecc_btn.clicked.connect(
            lambda: self._on_smu_read_other_table("EccInfo", TABLE_ECCINFO))
        other_btn_row.addWidget(self._smu_read_ecc_btn)

        other_btn_row.addStretch()
        tables_lay.addLayout(other_btn_row)

        self._smu_table_hex_view = QPlainTextEdit()
        self._smu_table_hex_view.setReadOnly(True)
        self._smu_table_hex_view.setStyleSheet(
            "background: #1a1a2a; color: #9d9; padding: 6px; "
            "font-family: Consolas, monospace; font-size: 8pt;")
        self._smu_table_hex_view.setMaximumHeight(300)
        self._smu_table_hex_view.setPlaceholderText(
            "Click a button above to read a table...")
        tables_lay.addWidget(self._smu_table_hex_view)

        self._smu_table_worker = None

        tables_scroll = QScrollArea()
        tables_scroll.setWidgetResizable(True)
        tables_scroll.setWidget(tables_w)
        self._smu_inner_tabs.addTab(tables_scroll, "Tables")

    # ------------------------------------------------------------------
    # Tables sub-tab: metrics helpers
    # ------------------------------------------------------------------

    def _init_metrics_table_rows(self):
        """Pre-populate the metrics QTableWidget with section headers and
        value rows so that refreshes only update cell text, not row structure."""
        tbl = self._smu_metrics_table
        tbl.setRowCount(0)
        self._metrics_value_items.clear()
        for section_name, keys in _METRICS_DISPLAY_SECTIONS:
            row = tbl.rowCount()
            tbl.insertRow(row)
            hdr = QTableWidgetItem(section_name)
            hdr.setBackground(Qt.GlobalColor.darkGray)
            hdr.setForeground(Qt.GlobalColor.white)
            font = hdr.font()
            font.setBold(True)
            hdr.setFont(font)
            tbl.setItem(row, 0, hdr)
            spacer = QTableWidgetItem("")
            spacer.setBackground(Qt.GlobalColor.darkGray)
            tbl.setItem(row, 1, spacer)
            tbl.setSpan(row, 0, 1, 2)

            for key in keys:
                row = tbl.rowCount()
                tbl.insertRow(row)
                name_item = QTableWidgetItem("  " + key)
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, 0, name_item)
                val_item = QTableWidgetItem("—")
                val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, 1, val_item)
                self._metrics_value_items[key] = val_item

    def _populate_metrics_values(self, d: dict):
        """Update existing value cells from a metrics dict (fast path)."""
        for key, item in self._metrics_value_items.items():
            val = d.get(key)
            item.setText(str(val) if val is not None else "—")

    # ------------------------------------------------------------------
    # Tables sub-tab: live metrics refresh
    # ------------------------------------------------------------------

    def _on_smu_metrics_refresh(self):
        """Manual one-shot read of full SmuMetrics_t."""
        if self._metrics_worker is not None and self._metrics_worker.isRunning():
            return
        self._smu_metrics_refresh_btn.setEnabled(False)
        self._smu_metrics_status_label.setText("Reading...")
        self._metrics_worker = MetricsRefreshWorker()
        self._metrics_worker.results_signal.connect(self._on_smu_metrics_results)
        self._metrics_worker.finished.connect(self._on_metrics_worker_done)
        self._metrics_worker.start()

    def _on_smu_metrics_auto_toggle(self, checked: bool):
        """Start/stop the auto-refresh timer."""
        if checked:
            interval_ms = self._smu_metrics_interval_spin.value() * 1000
            self._metrics_auto_timer.start(interval_ms)
            self._smu_metrics_status_label.setText("Auto-refresh ON")
            self._on_smu_metrics_timer_tick()
        else:
            self._metrics_auto_timer.stop()
            self._smu_metrics_status_label.setText("Auto-refresh OFF")

    def _on_smu_metrics_interval_changed(self, val: int):
        """Update timer interval if auto-refresh is active."""
        if self._metrics_auto_timer.isActive():
            self._metrics_auto_timer.setInterval(val * 1000)

    def _on_smu_metrics_timer_tick(self):
        """Timer-driven refresh — skips if a read is already in flight."""
        if self._metrics_worker is not None and self._metrics_worker.isRunning():
            return
        self._metrics_worker = MetricsRefreshWorker()
        self._metrics_worker.results_signal.connect(self._on_smu_metrics_results)
        self._metrics_worker.finished.connect(self._on_metrics_worker_done)
        self._metrics_worker.start()

    def _on_smu_metrics_results(self, result):
        """Handle metrics data from MetricsRefreshWorker."""
        if isinstance(result, dict) and "error" in result:
            self._smu_metrics_status_label.setText(f"Error: {result['error']}")
            self._log(f"SMU Metrics: {result['error']}")
            return
        self._populate_metrics_values(result)
        ts = time.strftime("%H:%M:%S")
        count = sum(1 for v in result.values() if v is not None)
        self._smu_metrics_status_label.setText(f"Updated {ts} ({count} values)")

    def _on_metrics_worker_done(self):
        self._metrics_worker = None
        self._smu_metrics_refresh_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Tables sub-tab: other SMU tables (on demand)
    # ------------------------------------------------------------------

    def _on_smu_read_other_table(self, table_name: str, table_id: int):
        """Read a raw SMU table and display as hex dump."""
        if self._smu_table_worker is not None and self._smu_table_worker.isRunning():
            self._log("Table read already in progress")
            return
        self._smu_read_pptable_btn.setEnabled(False)
        self._smu_read_driver_info_btn.setEnabled(False)
        self._smu_read_ecc_btn.setEnabled(False)
        self._smu_table_hex_view.setPlainText(f"Reading {table_name}...")
        self._log(f"Tables: reading {table_name} (id={table_id})...")

        self._smu_table_worker = SmuTableReadWorker(table_name, table_id)
        self._smu_table_worker.results_signal.connect(self._on_smu_table_read_results)
        self._smu_table_worker.finished.connect(self._on_smu_table_worker_done)
        self._smu_table_worker.start()

    def _on_smu_table_read_results(self, table_name: str, result):
        """Display raw table data as hex dump."""
        if isinstance(result, dict) and "error" in result:
            self._smu_table_hex_view.setPlainText(
                f"{table_name}: Error — {result['error']}")
            self._log(f"Tables: {table_name} failed: {result['error']}")
            return

        raw = result
        lines = [f"{table_name} — {len(raw)} bytes\n"]
        for off in range(0, len(raw), 16):
            chunk = raw[off:off + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            ascii_part = "".join(
                chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            lines.append(f"  {off:04X}: {hex_part:<48s}  {ascii_part}")
        self._smu_table_hex_view.setPlainText("\n".join(lines))
        self._log(f"Tables: {table_name} loaded ({len(raw)} bytes)")

    def _on_smu_table_worker_done(self):
        self._smu_table_worker = None
        self._smu_read_pptable_btn.setEnabled(True)
        self._smu_read_driver_info_btn.setEnabled(True)
        self._smu_read_ecc_btn.setEnabled(True)

    def _setup_smu_tricks_tab(self):
        """SMU Tricks tab: granular individual control over GFXCLK SoftMin/Max, HardMin/Max."""
        layout = QVBoxLayout(self.smu_tricks_tab)

        header = QLabel("SMU Tricks — Granular GFXCLK Frequency Control")
        header.setStyleSheet("font-weight: bold; font-size: 11pt;")
        layout.addWidget(header)

        desc = QLabel(
            "Control each GFXCLK frequency limit individually via direct SMU commands. "
            "Press Refresh to read current values from the SMU before making changes."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; padding: 4px 0;")
        layout.addWidget(desc)

        refresh_row = QHBoxLayout()
        self.smu_tricks_refresh_btn = QPushButton("Refresh")
        self.smu_tricks_refresh_btn.setToolTip("Read current DPM frequency limits from SMU")
        self.smu_tricks_refresh_btn.clicked.connect(self._on_smu_tricks_refresh)
        refresh_row.addWidget(self.smu_tricks_refresh_btn)
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        self.smu_tricks_table = QTableWidget()
        self.smu_tricks_table.setColumnCount(4)
        self.smu_tricks_table.setHorizontalHeaderLabels(["Name", "Current", "Input", "Apply"])
        self.smu_tricks_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.smu_tricks_table.horizontalHeader().setStretchLastSection(True)
        self.smu_tricks_table.verticalHeader().setVisible(False)

        self._smu_tricks_params = [
            ("GFXCLK SoftMin", "SoftMin", PPSMC.SetSoftMinByFreq, "min"),
            ("GFXCLK SoftMax", "SoftMax", PPSMC.SetSoftMaxByFreq, "max"),
            ("GFXCLK HardMin", "HardMin", PPSMC.SetHardMinByFreq, "min"),
            ("GFXCLK HardMax", "HardMax", PPSMC.SetHardMaxByFreq, "max"),
        ]
        self._smu_tricks_spins: dict[str, QSpinBox] = {}
        self._smu_tricks_current_items: dict[str, QTableWidgetItem] = {}

        for i, (name, key, msg_id, _dpm_field) in enumerate(self._smu_tricks_params):
            self.smu_tricks_table.insertRow(i)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.smu_tricks_table.setItem(i, 0, name_item)

            current_item = QTableWidgetItem("—")
            current_item.setFlags(current_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.smu_tricks_table.setItem(i, 1, current_item)
            self._smu_tricks_current_items[key] = current_item

            spin = QSpinBox()
            spin.setRange(0, 5000)
            spin.setValue(0)
            spin.setSpecialValueText("—")
            spin.setSuffix(" MHz")
            self.smu_tricks_table.setCellWidget(i, 2, spin)
            self._smu_tricks_spins[key] = spin

            btn = QPushButton("Apply")
            btn.setMaximumWidth(70)
            _key = key
            _msg_id = msg_id
            _name = name
            btn.clicked.connect(
                lambda checked=False, k=_key, m=_msg_id, n=_name:
                    self._on_smu_tricks_apply(k, m, n)
            )
            self.smu_tricks_table.setCellWidget(i, 3, btn)

        self.smu_tricks_table.setMaximumHeight(180)
        layout.addWidget(self.smu_tricks_table)

        algo_label = QLabel("Algorithm Reference")
        algo_label.setStyleSheet("font-weight: bold; margin-top: 12px;")
        layout.addWidget(algo_label)

        algo_text = QLabel(
            "When you hit Apply on a single row, the tool performs this sequence:\n"
            "  1. Send the selected SMU set-frequency command:\n"
            "       SetSoftMinByFreq(GFXCLK, value)  — soft minimum frequency floor\n"
            "       SetSoftMaxByFreq(GFXCLK, value)  — soft maximum frequency ceiling\n"
            "       SetHardMinByFreq(GFXCLK, value)  — hard minimum (absolute floor)\n"
            "       SetHardMaxByFreq(GFXCLK, value)  — hard maximum (absolute ceiling)\n"
            "  2. SetWorkloadMask(PowerSave), wait 300 ms  — trigger DPM refresh\n"
            "  3. SetWorkloadMask(3D Fullscreen), wait 300 ms — settle on gaming profile\n"
            "\n"
            "Steps 2-3 cycle the workload mask to force the SMU to re-evaluate DPM\n"
            "limits with the new value. Without this cycle, the SMU acknowledges the\n"
            "command but the effective limits remain unchanged.\n"
            "\n"
            "For comparison, the Simple tab runs the full sequence in order:\n"
            "  1. Patch GameClockAc & BoostClockAc in all RAM PPTable copies\n"
            "  2. SetSoftMaxByFreq(GFXCLK, clock + offset)          — raise ceiling first\n"
            "  3. SetHardMaxByFreq(GFXCLK, clock + offset)          — then hard ceiling\n"
            "  4. SetSoftMinByFreq(GFXCLK, clock)                   — raise floor\n"
            "  5. SetHardMinByFreq(GFXCLK, clock)                   — then hard floor\n"
            "  6. DisallowGfxOff                                     — prevent idle power gate\n"
            "  7. DisableSmuFeatures(DS_GFXCLK | GFX_ULV | GFXOFF)  — lock features (if enabled)\n"
            "  8. SetWorkloadMask(PowerSave), wait 300 ms            — trigger DPM refresh\n"
            "  9. SetWorkloadMask(3D Fullscreen), wait 300 ms        — settle on gaming profile\n"
            "\n"
            "Note: The SMU only reports effective DPM min/max — soft and hard limits\n"
            "cannot be queried separately. Current column shows DPM Min for the *Min\n"
            "rows and DPM Max for the *Max rows."
        )
        algo_text.setWordWrap(True)
        algo_text.setStyleSheet(
            "color: #aaa; font-size: 9pt; padding: 10px; "
            "background: #1a1a2a; border-radius: 4px; font-family: Consolas, monospace;"
        )
        layout.addWidget(algo_text)

        layout.addStretch()
        self._smu_tricks_worker = None

    def _setup_ecc_tab(self):
        """ECC tab: read ECC counters from SMU via table transfer."""
        layout = QVBoxLayout(self.ecc_tab)

        header = QLabel("ECC Counters — SMU TABLE_ECCINFO Transfer")
        header.setStyleSheet("font-weight: bold; font-size: 11pt;")
        layout.addWidget(header)

        desc = QLabel(
            "Read correctable error (CE) counters from the SMU. "
            "Counters are read-and-clear: the SMU resets them after each transfer, "
            "so we accumulate across reads (like the Linux amdgpu driver)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; padding: 4px 0;")
        layout.addWidget(desc)

        refresh_row = QHBoxLayout()
        self.ecc_refresh_btn = QPushButton("Refresh")
        self.ecc_refresh_btn.setToolTip("Transfer ECC table from SMU and read counters")
        self.ecc_refresh_btn.clicked.connect(self._on_ecc_refresh)
        refresh_row.addWidget(self.ecc_refresh_btn)
        self.ecc_reset_btn = QPushButton("Reset Totals")
        self.ecc_reset_btn.setToolTip("Clear the accumulated counters (SMU counters are unaffected)")
        self.ecc_reset_btn.clicked.connect(self._on_ecc_reset)
        refresh_row.addWidget(self.ecc_reset_btn)
        refresh_row.addStretch()
        layout.addLayout(refresh_row)

        self.ecc_summary_label = QLabel("—")
        self.ecc_summary_label.setWordWrap(True)
        self.ecc_summary_label.setStyleSheet(
            "background: #2a2a2a; color: #ddd; padding: 8px; border-radius: 4px; "
            "font-family: Consolas, monospace; font-size: 9pt;"
        )
        layout.addWidget(self.ecc_summary_label)

        self.ecc_table_widget = QTableWidget()
        self.ecc_table_widget.setColumnCount(5)
        self.ecc_table_widget.setHorizontalHeaderLabels([
            "Channel", "This Read", "Accumulated", "Status", "MCA Addr",
        ])
        self.ecc_table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.ecc_table_widget.horizontalHeader().setStretchLastSection(True)
        self.ecc_table_widget.setMaximumHeight(280)
        layout.addWidget(self.ecc_table_widget)

        self.ecc_raw_label = QLabel("")
        self.ecc_raw_label.setWordWrap(True)
        self.ecc_raw_label.setStyleSheet(
            "background: #1a1a2a; color: #8a8; padding: 6px; border-radius: 4px; "
            "font-family: Consolas, monospace; font-size: 8pt;"
        )
        self.ecc_raw_label.setMaximumHeight(160)
        self.ecc_raw_label.hide()
        layout.addWidget(self.ecc_raw_label)

        layout.addStretch()
        self._ecc_worker = None
        self._ecc_accum = EccAccumulator()

    def _on_ecc_refresh(self):
        """Read ECC counters from SMU."""
        if self._ecc_worker is not None and self._ecc_worker.isRunning():
            return
        self.ecc_refresh_btn.setEnabled(False)
        self.ecc_summary_label.setText("Reading...")
        self.ecc_table_widget.setRowCount(0)
        self._ecc_worker = EccRefreshWorker(self)
        self._ecc_worker.results_signal.connect(self._on_ecc_refresh_results)
        self._ecc_worker.finished.connect(self._enable_ecc_refresh)
        self._ecc_worker.start()
        self._log("ECC: reading counters from SMU...")

    def _enable_ecc_refresh(self):
        self._ecc_worker = None
        self.ecc_refresh_btn.setEnabled(True)

    def _on_ecc_reset(self):
        """Reset accumulated ECC counters (SMU-side counters are unaffected)."""
        self._ecc_accum = EccAccumulator()
        self.ecc_table_widget.setRowCount(0)
        self.ecc_summary_label.setText("Accumulated totals reset.")
        self.ecc_raw_label.hide()
        self._log("ECC: accumulated totals reset")

    def _on_ecc_refresh_results(self, result):
        """Update ECC tab with table data, accumulating across reads."""
        self.ecc_table_widget.setRowCount(0)

        if isinstance(result, dict) and "error" in result:
            self.ecc_summary_label.setText(f"Error: {result['error']}")
            self._log(f"ECC refresh failed: {result['error']}")
            return

        if isinstance(result, tuple) and len(result) == 2:
            ecc_table, raw_bytes = result
        else:
            ecc_table, raw_bytes = result, None

        if ecc_table is None:
            self.ecc_summary_label.setText(
                "ECC table transfer failed or TABLE_ECCINFO not supported on this firmware."
            )
            self._log("ECC: not supported or transfer failed")
            return

        # Check for firmware stub / test pattern
        test_msg = detect_test_pattern(raw_bytes)
        if test_msg:
            self.ecc_summary_label.setText(test_msg)
            self._log(f"ECC: test pattern detected — {test_msg}")
            if raw_bytes:
                hex_dump = format_raw_hex(raw_bytes[:128])
                self.ecc_raw_label.setText(f"Raw ECC table (first 128 bytes):\n{hex_dump}")
                self.ecc_raw_label.show()
            return

        this_read = total_ce_count(ecc_table)
        self._ecc_accum.add(ecc_table)
        grand = self._ecc_accum.grand_total()
        reads = self._ecc_accum.reads

        self.ecc_summary_label.setText(
            f"This read: {this_read} CE  |  Accumulated total: {grand} CE  "
            f"({reads} read{'s' if reads != 1 else ''})"
        )

        this_summary = format_ecc_summary(ecc_table)
        accum_summary = self._ecc_accum.per_channel_summary()

        all_channels = sorted(set(
            [ch for ch, _, _ in this_summary] +
            [ch for ch, _, _ in accum_summary]
        ))
        this_map = {ch: ce for ch, ce, _ in this_summary}
        accum_map = {ch: (ce, st) for ch, ce, st in accum_summary}

        for ch in all_channels:
            row = self.ecc_table_widget.rowCount()
            self.ecc_table_widget.insertRow(row)
            self.ecc_table_widget.setItem(row, 0, QTableWidgetItem(str(ch)))
            self.ecc_table_widget.setItem(row, 1, QTableWidgetItem(
                str(this_map.get(ch, 0))))
            acc_ce, acc_st = accum_map.get(ch, (0, "—"))
            self.ecc_table_widget.setItem(row, 2, QTableWidgetItem(str(acc_ce)))
            self.ecc_table_widget.setItem(row, 3, QTableWidgetItem(acc_st))
            mca_addr = self._ecc_accum.last_mca_addr[ch]
            mca = f"0x{mca_addr:X}" if mca_addr else "—"
            self.ecc_table_widget.setItem(row, 4, QTableWidgetItem(mca))

        if raw_bytes:
            hex_dump = format_raw_hex(raw_bytes[:128])
            self.ecc_raw_label.setText(f"Raw ECC table (first 128 bytes):\n{hex_dump}")
            self.ecc_raw_label.show()

        self._log(f"ECC: this_read={this_read} CE, accumulated={grand} CE (reads={reads})")

    def _on_smu_tricks_refresh(self):
        """Read current DPM frequency limits from SMU for the SMU Tricks table."""
        if self._smu_tricks_worker is not None and self._smu_tricks_worker.isRunning():
            return
        self.smu_tricks_refresh_btn.setEnabled(False)
        self._smu_tricks_worker = SmuTricksRefreshWorker(self)
        self._smu_tricks_worker.results_signal.connect(self._on_smu_tricks_refresh_results)
        self._smu_tricks_worker.finished.connect(self._enable_smu_tricks_refresh)
        self._smu_tricks_worker.start()
        self._log("SMU Tricks: reading DPM frequency limits...")

    def _enable_smu_tricks_refresh(self):
        self._smu_tricks_worker = None
        self.smu_tricks_refresh_btn.setEnabled(True)

    def _on_smu_tricks_refresh_results(self, result):
        """Update SMU Tricks table with current DPM min/max from SMU."""
        if "error" in result:
            self._log(f"SMU Tricks refresh failed: {result['error']}")
            return

        dpm_min = result["min"]
        dpm_max = result["max"]

        for key, item in self._smu_tricks_current_items.items():
            if key in ("SoftMin", "HardMin"):
                item.setText(f"{dpm_min} MHz")
            else:
                item.setText(f"{dpm_max} MHz")

        for key, spin in self._smu_tricks_spins.items():
            if key in ("SoftMin", "HardMin"):
                spin.setValue(dpm_min)
            else:
                spin.setValue(dpm_max)

        self._log(f"SMU Tricks: DPM min={dpm_min} MHz, max={dpm_max} MHz")

    def _on_smu_tricks_apply(self, key, msg_id, name):
        """Apply a single GFXCLK frequency limit via SMU, then cycle workload mask to force DPM refresh."""
        spin = self._smu_tricks_spins[key]
        val = spin.value()
        if val <= 0:
            self._log(f"SMU Tricks: {name} skipped (no value set)")
            return

        def do_apply(hw):
            smu = hw["smu"]
            param = ((PPCLK.GFXCLK & 0xFFFF) << 16) | (val & 0xFFFF)
            resp, _ = smu.send_msg(msg_id, param)
            if resp == 1:
                self._log(f"SMU Tricks: {name} = {val} MHz (OK)")
            else:
                self._log(f"SMU Tricks: {name} = {val} MHz (resp=0x{resp:02X})")
                return
            smu.send_msg(PPSMC.SetWorkloadMask, 1 << 2)   # PowerSave
            time.sleep(0.3)
            smu.send_msg(PPSMC.SetWorkloadMask, 1 << 1)   # 3D Fullscreen
            time.sleep(0.3)
            self._log(f"SMU Tricks: DPM refresh cycle done")

        self._run_with_hardware(f"SMU Tricks: {name}", do_apply, require_scan=False)

    def _setup_memory_tab(self):
        """Memory tab: view of PPTable copies in RAM, manual refresh."""
        layout = QVBoxLayout(self.memory_tab)

        memory_tooltip = (
            "First row: VBIOS (reference) = original values from bios/vbios.rom. "
            "Other rows: PPTable data in RAM (may be patched). "
            "The driver may move or unmap; if reads fail, entries show 'Unavailable'."
        )
        header_row = QHBoxLayout()
        header = QLabel("Memory — PPTable copies at scanned addresses")
        header.setStyleSheet("font-weight: bold;")
        header.setToolTip(memory_tooltip)
        header_row.addWidget(header)
        help_btn = QToolButton()
        help_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
        )
        help_btn.setIconSize(QSize(16, 16))
        help_btn.setToolTip(memory_tooltip)
        help_btn.setStyleSheet("QToolButton { border: none; background: transparent; }")
        help_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
        header_row.addWidget(help_btn)
        header_row.addStretch()
        self.memory_refresh_btn = QPushButton("Refresh")
        self.memory_refresh_btn.setToolTip("Read PPTable data from all scanned addresses")
        self.memory_refresh_btn.clicked.connect(self._on_memory_refresh_click)
        self.memory_refresh_btn.setEnabled(False)
        header_row.addWidget(self.memory_refresh_btn)
        layout.addLayout(header_row)

        self.memory_banner = QLabel()
        self.memory_banner.setWordWrap(True)
        self.memory_banner.setStyleSheet(
            "background: #4a3020; color: #faa; padding: 8px; border-radius: 4px;"
        )
        self.memory_banner.hide()
        layout.addWidget(self.memory_banner)

        self.memory_table = QTableWidget()
        self.memory_table.setColumnCount(14)
        self.memory_table.setHorizontalHeaderLabels([
            "Address", "Status", "BaseClock", "GameClock", "BoostClock",
            "PPT AC", "PPT DC", "TDC GFX", "TDC SOC",
            "Temp Edge", "Temp Hotspot", "Temp Mem", "Temp VR GFX", "Temp VR SOC",
        ])
        self.memory_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.memory_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.memory_table)

        self._memory_worker = None

        self._update_memory_placeholder("Scanning...")

    _REG_CHEATSHEET_HTML = """
<h3>Registry Patch &mdash; AMD GPU Driver Settings</h3>
<p>This tab reads and writes DWORD values in the AMD GPU driver's registry key
under <code>HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{display-GUID}\\XXXX</code>.
Changes are <b>persistent across reboots</b> (unlike SMU commands or RAM patches).</p>

<h4>How it works</h4>
<ul>
  <li><b>Current</b> column &mdash; The value currently stored in the registry (read-only).</li>
  <li><b>Custom</b> column &mdash; The value you want to apply. Checkboxes toggle 0/1;
      spinboxes accept numeric values.</li>
  <li><b>Select recommended</b> &mdash; Fills the Custom column with the recommended
      anti-gating values (disables power-saving features). Settings that have no specific
      recommendation are left at their current registry value.</li>
  <li><b>Apply</b> &mdash; Writes all Custom column values to the registry. A backup of
      original values is saved on first apply.</li>
  <li><b>Return to stock</b> &mdash; Restores the original values from the backup file.</li>
</ul>

<h4>Anti-Clock-Gating Patches</h4>
<p>These disable various power-saving features that cause clock gating, power gating,
and idle downclocking. Disabling them keeps clocks high and reduces latency spikes
at the cost of higher idle power consumption.</p>
<ul>
  <li><b>EnableUlps</b> (&rarr;0) &mdash; Ultra Low Power State. Aggressively power-gates the
      GPU at idle. Causes wake-up latency, CrossFire/multi-monitor instability.</li>
  <li><b>PP_GPUPowerDownEnabled</b> (&rarr;0) &mdash; GPU power-down gating. Allows the driver
      to fully gate the GPU when idle.</li>
  <li><b>EnableUvdClockGating</b> (&rarr;0) &mdash; UVD (Unified Video Decoder) clock gating.
      Legacy; replaced by VCN on RDNA. Harmless to set.</li>
  <li><b>EnableVceSwClockGating</b> (&rarr;0) &mdash; VCE (Video Coding Engine) software clock
      gating. Legacy; replaced by VCN on RDNA.</li>
  <li><b>KMD_EnableContextBasedPowerManagement</b> (&rarr;0) &mdash; Context-based power
      management. Driver switches power states based on application context.</li>
  <li><b>EnableAspmL0s / EnableAspmL1</b> (&rarr;0) &mdash; PCIe Active State Power Management.
      L0s and L1 add link-level power saving with latency penalties.</li>
  <li><b>PP_ULPSDelayIntervalInMilliSeconds</b> (&rarr;0) &mdash; Delay before entering ULPS.
      Setting to 0 effectively disables the ULPS timer.</li>
  <li><b>DisableVCEPowerGating</b> (&rarr;1) &mdash; VCE power gating (1=disabled). Legacy.</li>
  <li><b>PP_DisablePowerContainment</b> (&rarr;1) &mdash; Power containment / boost capping.
      When disabled, the GPU does not cap boost clocks based on power draw. Useful for
      benchmarking.</li>
  <li><b>PP_DisableClockStretcher</b> (&rarr;1) &mdash; Clock stretcher. When disabled, prevents
      mid-load clock drops used to manage transient power spikes.</li>
  <li><b>PP_MCLKDeepSleepDisable</b> (&rarr;1) &mdash; Memory clock deep sleep. When disabled,
      VRAM stays at operating frequency instead of dropping to deep-sleep.</li>
  <li><b>KMD_EnableGFXLowPowerState</b> (&rarr;0) &mdash; GFX low-power state. When disabled,
      the graphics engine does not enter its low-power idle mode.</li>
  <li><b>DisableDrmdmaPowerGating</b> (&rarr;1) &mdash; DRM/DMA power gating (1=disabled).</li>
</ul>

<h4>Verification Values</h4>
<p>These are typically already set correctly by modern drivers. We verify them
but they rarely need changing:</p>
<ul>
  <li><b>PP_SclkDeepSleepDisable</b> (=1) &mdash; SCLK deep sleep disabled. Prevents HDMI
      audio dropouts.</li>
  <li><b>PP_DisableVoltageIsland</b> (=1) &mdash; Voltage island power gating disabled.</li>
  <li><b>DisableSAMUPowerGating</b> (=1) &mdash; SAMU power gating disabled (pre-RDNA).</li>
  <li><b>GCOOPTION_DisableGPIOPowerSaveMode</b> (=1) &mdash; GPIO power-save mode disabled.</li>
</ul>

<h4>Performance Tuning</h4>
<p>These control DPM behavior, stutter mode, and other tunable parameters. They are
<b>not</b> included in &ldquo;Select recommended&rdquo; &mdash; the Custom column defaults to the
current registry value so you can tweak them manually.</p>
<ul>
  <li><b>PP_ThermalAutoThrottlingEnable</b> &mdash; Master thermal throttle switch in
      PowerPlay. Default 1 (enabled). Setting to 0 disables thermal throttling &mdash;
      <span style="color:#c00;">dangerous, GPU can overheat.</span></li>
  <li><b>StutterMode</b> &mdash; Memory clock stutter mode. 0=OFF, 1=on, 2=Vega default.
      Setting to 0 eliminates MCLK stuttering (keeps VRAM clock constant).</li>
  <li><b>PP_MCLKStutterModeThreshold</b> &mdash; Threshold before the driver engages
      stutter mode. Default ~81920 (0x14000). Lower values or 0 reduce/disable stutter.</li>
  <li><b>PP_ActivityTarget</b> &mdash; DPM activity threshold (%). Default 30. Lower values
      make the GPU ramp up to higher clocks sooner under light load.</li>
  <li><b>PP_AllGraphicLevel_UpHyst</b> &mdash; Delay in ms before clocking up. Default ~50ms.
      Setting to 0 gives immediate clock ramp-up.</li>
  <li><b>PP_AllGraphicLevel_DownHyst</b> &mdash; Delay in ms before clocking down. Default
      ~20ms. Higher values keep clocks elevated longer after load drops.</li>
  <li><b>KMD_FRTEnabled</b> &mdash; Frame Rate Target Control. 0=off, 1=on. When on, the
      driver caps FPS via KMD_MaxFrameRateRequested.</li>
  <li><b>DisableFBCSupport</b> &mdash; Frame Buffer Compression. 1=disabled. Disabling can
      fix rendering artifacts on some setups.</li>
  <li><b>DMMEnableDDCPolling</b> &mdash; DDC (Display Data Channel) polling. 0=disabled.
      Disabling reduces periodic overhead from monitor detection.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>All changes require a <b>reboot</b> to take full effect (the driver reads these
      values at load time).</li>
  <li>UVD, VCE, and SAMU settings are GCN/Vega-era legacy. On RDNA GPUs they are
      harmless but functionally no-ops (VCN replaced UVD+VCE).</li>
  <li>Disabling thermal throttling or power containment removes hardware protection.
      Monitor temperatures carefully.</li>
  <li>A backup of original values is saved on first Apply. Use &ldquo;Return to stock&rdquo; to
      restore.</li>
  <li>Administrator privileges are required for both reading and writing.</li>
</ul>
"""

    def _setup_registry_tab(self):
        """Registry Patch tab: table with Name, Current, Custom (checkboxes + spinboxes)."""
        layout = QVBoxLayout(self.registry_tab)
        self._reg_worker = None
        self._reg_widgets = {}

        if RegistryPatch is None:
            msg = QLabel(
                "Registry patch is not available (Windows only). "
                "The winreg module is required for AMD GPU registry anti-clock-gating patches."
            )
            msg.setWordWrap(True)
            msg.setStyleSheet("color: #888; padding: 16px;")
            layout.addWidget(msg)
            return

        self._reg_patch: RegistryPatch | None = None
        self._reg_report: dict | None = None

        # Adapter info
        try:
            self._reg_patch = RegistryPatch()
            self._reg_report = self._reg_patch.read_current()
        except Exception as e:
            err_label = QLabel(f"Cannot access registry: {e}\nRun as Administrator if needed.")
            err_label.setWordWrap(True)
            err_label.setStyleSheet("color: #c00; padding: 16px;")
            layout.addWidget(err_label)
            return

        info = self._reg_patch.info
        info_text = f"Adapter: {info.get('DriverDesc', '?')}  |  {info.get('MatchingDeviceId', '?')}"
        info_label = QLabel(info_text)
        info_label.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(info_label)

        # Cheatsheet button
        hint_btn = QToolButton()
        hint_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxQuestion))
        hint_btn.setIconSize(QSize(18, 18))
        hint_btn.setToolTip("Open cheatsheet for Registry Patch")
        hint_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; }")
        hint_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
        hint_btn.clicked.connect(
            lambda: self._show_smu_cheatsheet(
                "Registry Patch", self._REG_CHEATSHEET_HTML))
        hint_row = QHBoxLayout()
        hint_row.addWidget(hint_btn)
        hint_row.addStretch()
        layout.addLayout(hint_row)

        # Table: Name, Current, Custom
        self.reg_table = QTableWidget()
        self.reg_table.setColumnCount(3)
        self.reg_table.setHorizontalHeaderLabels(["Name", "Current", "Custom"])
        self.reg_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.reg_table.horizontalHeader().setStretchLastSection(True)

        self._populate_reg_table(self._reg_report)

        layout.addWidget(self.reg_table)

        # Buttons
        btn_row = QHBoxLayout()
        self.reg_select_recommended_btn = QPushButton("Select recommended")
        self.reg_select_recommended_btn.setToolTip(
            "Fill Custom column with recommended anti-gating values; "
            "performance tuning settings stay at current")
        self.reg_select_recommended_btn.clicked.connect(self._on_reg_select_recommended)
        btn_row.addWidget(self.reg_select_recommended_btn)

        self.reg_refresh_btn = QPushButton("Refresh")
        self.reg_refresh_btn.clicked.connect(self._on_reg_refresh)
        btn_row.addWidget(self.reg_refresh_btn)

        self.reg_apply_btn = QPushButton("Apply")
        self.reg_apply_btn.setToolTip("Apply Custom column values to registry (saves original state on first apply)")
        self.reg_apply_btn.clicked.connect(self._on_reg_apply)
        btn_row.addWidget(self.reg_apply_btn)

        self.reg_restore_btn = QPushButton("Return to stock")
        self.reg_restore_btn.setToolTip("Restore original values from backup")
        self.reg_restore_btn.clicked.connect(self._on_reg_restore)
        self.reg_restore_btn.setEnabled(os.path.isfile(BACKUP_FILE) if BACKUP_FILE else False)
        btn_row.addWidget(self.reg_restore_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _on_reg_refresh(self):
        """Re-read registry and update table."""
        if self._reg_patch is None:
            return
        if hasattr(self, "_reg_worker") and self._reg_worker is not None and self._reg_worker.isRunning():
            return
        self._reg_worker = RegistryPatchWorker("read", self._reg_patch.read_current, self)
        self._reg_worker.finished_signal.connect(self._on_reg_worker_finished)
        self._reg_worker.finished.connect(lambda: setattr(self, "_reg_worker", None))
        self._reg_worker.start()
        self.reg_refresh_btn.setEnabled(False)
        self.reg_apply_btn.setEnabled(False)
        self.reg_restore_btn.setEnabled(False)

    def _on_reg_apply(self):
        """Apply Custom column values to registry."""
        if self._reg_patch is None:
            return
        if hasattr(self, "_reg_worker") and self._reg_worker is not None and self._reg_worker.isRunning():
            return

        values = {}
        for n, w in self._reg_widgets.items():
            if isinstance(w, QCheckBox):
                values[n] = 1 if w.isChecked() else 0
            elif isinstance(w, QSpinBox):
                values[n] = w.value()

        def do_apply():
            return self._reg_patch.apply(values=values)

        self._reg_worker = RegistryPatchWorker("apply", do_apply, self)
        self._reg_worker.finished_signal.connect(self._on_reg_worker_finished)
        self._reg_worker.finished.connect(lambda: setattr(self, "_reg_worker", None))
        self._reg_worker.start()
        self.reg_refresh_btn.setEnabled(False)
        self.reg_apply_btn.setEnabled(False)
        self.reg_restore_btn.setEnabled(False)
        self._log(f"Registry: Applying {len(values)} value(s)...")

    def _on_reg_restore(self):
        """Restore original values from backup."""
        if self._reg_patch is None:
            return
        if hasattr(self, "_reg_worker") and self._reg_worker is not None and self._reg_worker.isRunning():
            return

        def do_restore():
            return self._reg_patch.restore()

        self._reg_worker = RegistryPatchWorker("restore", do_restore, self)
        self._reg_worker.finished_signal.connect(self._on_reg_worker_finished)
        self._reg_worker.finished.connect(lambda: setattr(self, "_reg_worker", None))
        self._reg_worker.start()
        self.reg_refresh_btn.setEnabled(False)
        self.reg_apply_btn.setEnabled(False)
        self.reg_restore_btn.setEnabled(False)
        self._log("Registry: Restoring original values...")

    def _on_reg_worker_finished(self, action: str, result, is_error: bool):
        """Handle registry worker completion."""
        self.reg_refresh_btn.setEnabled(True)
        self.reg_apply_btn.setEnabled(True)
        if BACKUP_FILE and os.path.isfile(BACKUP_FILE):
            self.reg_restore_btn.setEnabled(True)

        if is_error:
            self._log(f"Registry {action}: failed — {result}")
            return

        if action == "read":
            self._reg_report = result
            self._update_reg_table(result)
        elif action == "apply":
            changes = result
            if changes:
                self._log(f"Registry: Applied {len(changes)} change(s). Reboot for full effect.")
                self._reg_report = self._reg_patch.read_current()
                self._update_reg_table(self._reg_report)
            else:
                self._log("Registry: No changes needed.")
        elif action == "restore":
            restored = result
            self._log(f"Registry: Restored {len(restored)} value(s). Reboot for full effect.")
            self._reg_report = self._reg_patch.read_current()
            self._update_reg_table(self._reg_report)

    def _make_reg_name_cell(self, display_name: str, original_name: str) -> QWidget:
        """Create Name column cell: display name + ? icon with tooltip showing original key."""
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        label = QLabel(display_name)
        layout.addWidget(label)
        hint_btn = QToolButton()
        hint_btn.setText("?")
        hint_btn.setToolTip(original_name)
        hint_btn.setFixedSize(18, 18)
        hint_btn.setStyleSheet("font-size: 10pt; font-weight: bold;")
        layout.addWidget(hint_btn)
        layout.addStretch()
        return cell

    def _populate_reg_table(self, report: dict):
        """Populate or refresh registry table from report (patch + verify + extra)."""
        if not hasattr(self, "reg_table") or self.reg_table is None:
            return
        self.reg_table.setRowCount(0)
        self._reg_widgets.clear()
        patch_data = report.get("patch", {})
        verify_data = report.get("verify", {})
        extra_data = report.get("extra", {})

        def _add_bool_row(name, entry):
            current = entry.get("current")
            current_is_one = current == 1 if current is not None else False
            row = self.reg_table.rowCount()
            self.reg_table.insertRow(row)
            display_name = REG_NAME_TO_DISPLAY.get(name, name) if REG_NAME_TO_DISPLAY else name
            self.reg_table.setCellWidget(row, 0, self._make_reg_name_cell(display_name, name))
            current_cb = QCheckBox()
            current_cb.setChecked(current_is_one)
            current_cb.setEnabled(False)
            current_cb.setToolTip("Current registry value (read-only)")
            self.reg_table.setCellWidget(row, 1, current_cb)
            custom_cb = QCheckBox()
            custom_cb.setChecked(current_is_one)
            custom_cb.setToolTip("Value to apply: checked=1, unchecked=0")
            self._reg_widgets[name] = custom_cb
            self.reg_table.setCellWidget(row, 2, custom_cb)

        def _add_spin_row(name, entry, min_val, max_val):
            current = entry.get("current")
            row = self.reg_table.rowCount()
            self.reg_table.insertRow(row)
            display_name = REG_NAME_TO_DISPLAY.get(name, name) if REG_NAME_TO_DISPLAY else name
            self.reg_table.setCellWidget(row, 0, self._make_reg_name_cell(display_name, name))
            cur_label = QLabel(str(current) if current is not None else "(missing)")
            cur_label.setEnabled(False)
            cur_label.setToolTip("Current registry value (read-only)")
            self.reg_table.setCellWidget(row, 1, cur_label)
            spin = QSpinBox()
            spin.setRange(min_val, max_val)
            spin.setValue(current if current is not None else 0)
            spin.setToolTip(f"Value to apply ({min_val}–{max_val})")
            self._reg_widgets[name] = spin
            self.reg_table.setCellWidget(row, 2, spin)

        for name, entry in patch_data.items():
            _add_bool_row(name, entry)
        for name, entry in verify_data.items():
            _add_bool_row(name, entry)
        for name, entry in extra_data.items():
            if entry.get("is_bool", True):
                _add_bool_row(name, entry)
            else:
                _add_spin_row(name, entry, entry.get("min", 0), entry.get("max", 0xFFFF))

    def _on_reg_select_recommended(self):
        """Set Custom widgets to recommended values where defined; leave others at current."""
        if not RECOMMENDED_VALUES:
            return
        for name, w in self._reg_widgets.items():
            recommended = RECOMMENDED_VALUES.get(name)
            if recommended is None:
                if self._reg_report:
                    for section in ("patch", "verify", "extra"):
                        if name in self._reg_report.get(section, {}):
                            cur = self._reg_report[section][name].get("current")
                            if cur is not None:
                                if isinstance(w, QCheckBox):
                                    w.setChecked(cur == 1)
                                elif isinstance(w, QSpinBox):
                                    w.setValue(cur)
                            break
                continue
            if isinstance(w, QCheckBox):
                w.setChecked(recommended == 1)
            elif isinstance(w, QSpinBox):
                w.setValue(recommended)

    def _update_reg_table(self, report: dict):
        """Update registry table from report."""
        self._populate_reg_table(report)

    def _on_memory_refresh_click(self):
        """Manual refresh: read PPTable data from all scanned addresses."""
        if self._memory_worker is not None and self._memory_worker.isRunning():
            return
        if self.scan_result is None:
            self._update_memory_placeholder("Scanning...")
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        if not addrs:
            self._update_memory_placeholder("No addresses")
            return
        self.memory_refresh_btn.setEnabled(False)
        self._memory_worker = MemoryRefreshWorker(addrs, self)
        self._memory_worker.results_signal.connect(self._on_memory_refresh_results)
        self._memory_worker.finished.connect(lambda: self._enable_memory_refresh())
        self._memory_worker.start()

    def _enable_memory_refresh(self):
        self._memory_worker = None
        self.memory_refresh_btn.setEnabled(True)

    def _update_memory_placeholder(self, text: str):
        """Show placeholder text when no table data (scanning, no addrs, etc.)."""
        self.memory_table.setRowCount(0)
        self.memory_banner.setText(text)
        self.memory_banner.setVisible(bool(text))

    def _on_memory_refresh_results(self, results: list):
        """Update memory table from worker results. First row is VBIOS reference (decode on demand)."""
        vb = _get_vbios_values()
        if vb is None:
            vb = self.vbios_values
        vb_data = {
            "baseclock_ac": vb.baseclock_ac,
            "gameclock_ac": vb.gameclock_ac,
            "boostclock_ac": vb.boostclock_ac,
            "ppt0_ac": vb.power_ac,
            "ppt0_dc": vb.power_dc,
            "tdc_gfx": vb.tdc_gfx,
            "tdc_soc": vb.tdc_soc,
            "temp_edge": vb.temp_edge or 0,
            "temp_hotspot": vb.temp_hotspot or 0,
            "temp_mem": vb.temp_mem or 0,
            "temp_vr_gfx": vb.temp_vr_gfx or 0,
            "temp_vr_soc": vb.temp_vr_soc or 0,
        }
        # Prepend VBIOS reference row so it's always visible for comparison
        rows = [("VBIOS (reference)", "—", vb_data)] + [
            (f"0x{addr:012X}", status, data) for addr, status, data in results
        ]
        self.memory_banner.hide()
        self.memory_table.setRowCount(len(rows))

        failed_count = sum(1 for _, status, _ in results if status != "OK")
        if failed_count == len(results) and results:
            self.memory_banner.setText(
                "All PPTable copies unavailable. Driver may have moved tables. Try re-scanning."
            )
            self.memory_banner.show()

        def _fmt(val, suffix=""):
            if val is not None:
                return f"{val}{suffix}"
            return "—"

        for row, (addr_str, status, data) in enumerate(rows):
            self.memory_table.setItem(row, 0, QTableWidgetItem(addr_str))
            self.memory_table.setItem(row, 1, QTableWidgetItem(status))
            d = data if data else {}
            self.memory_table.setItem(row, 2, QTableWidgetItem(_fmt(d.get("baseclock_ac"), " MHz")))
            self.memory_table.setItem(row, 3, QTableWidgetItem(_fmt(d.get("gameclock_ac"), " MHz")))
            self.memory_table.setItem(row, 4, QTableWidgetItem(_fmt(d.get("boostclock_ac"), " MHz")))
            self.memory_table.setItem(row, 5, QTableWidgetItem(_fmt(d.get("ppt0_ac"), " W")))
            self.memory_table.setItem(row, 6, QTableWidgetItem(_fmt(d.get("ppt0_dc"), " W")))
            self.memory_table.setItem(row, 7, QTableWidgetItem(_fmt(d.get("tdc_gfx"), " A")))
            self.memory_table.setItem(row, 8, QTableWidgetItem(_fmt(d.get("tdc_soc"), " A")))
            self.memory_table.setItem(row, 9, QTableWidgetItem(_fmt(d.get("temp_edge"), " °C")))
            self.memory_table.setItem(row, 10, QTableWidgetItem(_fmt(d.get("temp_hotspot"), " °C")))
            self.memory_table.setItem(row, 11, QTableWidgetItem(_fmt(d.get("temp_mem"), " °C")))
            self.memory_table.setItem(row, 12, QTableWidgetItem(_fmt(d.get("temp_vr_gfx"), " °C")))
            self.memory_table.setItem(row, 13, QTableWidgetItem(_fmt(d.get("temp_vr_soc"), " °C")))

    def _start_memory_refresh_if_ready(self):
        """Do initial memory read after scan completes."""
        if self.scan_result and getattr(self.scan_result, "valid_addrs", None):
            self.memory_refresh_btn.setEnabled(True)
            self._on_memory_refresh_click()

    def _on_detailed_refresh_click(self):
        """Manual refresh: read live RAM/SMU values for PP/OD/SMU tabs."""
        if self._detailed_worker is not None and self._detailed_worker.isRunning():
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) if self.scan_result else []
        self._set_detailed_refresh_enabled(False)
        self._detailed_worker = DetailedRefreshWorker(
            addrs,
            pp_ram_offset_map=self._pp_ram_offset_map,
            parent=self,
        )
        self._detailed_worker.results_signal.connect(self._on_detailed_refresh_results)
        self._detailed_worker.finished.connect(lambda: self._enable_detailed_refresh())
        self._detailed_worker.start()

    def _set_detailed_refresh_enabled(self, enabled: bool):
        self.pp_refresh_btn.setEnabled(enabled)
        self.od_refresh_btn.setEnabled(enabled)
        for btn in getattr(self, "_smu_refresh_buttons", []):
            btn.setEnabled(enabled)

    def _enable_detailed_refresh(self):
        self._detailed_worker = None
        self._set_detailed_refresh_enabled(True)

    def _on_detailed_refresh_results(self, ram_data, od_table, metrics, smu_state=None):
        """Update Detailed tab Live columns and Custom inputs from worker results."""
        _log_to_file(f"_on_detailed_refresh_results: ram={ram_data is not None}, "
                     f"od={od_table is not None}, metrics={metrics is not None}, "
                     f"smu={smu_state is not None and len(smu_state) if smu_state else None}")
        self._update_detailed_live_columns(ram_data, od_table, metrics, smu_state)
        if od_table:
            self._update_od_from_scan(od_table)
            if self.scan_result is None:
                self.scan_result = ScanResult(
                    [], [], [], [], False, [], od_table=od_table
                )
            else:
                self.scan_result.od_table = od_table
            self._set_apply_buttons_enabled(self._can_apply())
        if smu_state:
            self._update_smu_status_labels(smu_state)
            self._update_smu_widgets_from_state(smu_state)
            self._update_smu_feature_checkboxes(smu_state)
            ver = smu_state.get("smu_version", "?")
            n_freq = sum(1 for k in smu_state if k.startswith("smu_freq_"))
            self._log(f"SMU refresh: version={ver}, {n_freq} freq values, "
                      f"ppt={smu_state.get('smu_ppt', '?')}, "
                      f"voltage={smu_state.get('smu_voltage', '?')}\n")
        elif smu_state is None:
            self._log("SMU refresh: FAILED — could not read SMU state (hardware init error?)\n")
        for _tbl in self._detailed_tables.values():
            _tbl.viewport().update()

    def _start_detailed_refresh_if_ready(self):
        """Do initial detailed read after scan completes (RAM data when addrs exist)."""
        if not self.scan_result:
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        od = getattr(self.scan_result, "od_table", None)
        if addrs or od:
            self._on_detailed_refresh_click()

    def _log(self, msg: str):
        """Thread-safe: file log from any thread; GUI update via signal from worker threads."""
        _log_to_file(msg)
        app = QApplication.instance()
        if app and QThread.currentThread() is app.thread():
            self._log_gui(msg)
        else:
            self.log_request_signal.emit(msg)

    def _log_gui(self, msg: str):
        """Update log widget. Must be called from main thread only."""
        self.log_output.appendPlainText(msg)
        sb = self.log_output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _can_apply(self) -> bool:
        """True if any apply is allowed (scan finished, have hw or od_table)."""
        if self.scan_result is None:
            return False
        if (
            self.scan_result.error
            and not self.scan_result.valid_addrs
            and not getattr(self.scan_result, "od_table", None)
        ):
            return False
        return True

    def _set_apply_buttons_enabled(self, enabled: bool):
        """Enable or disable scan-dependent Apply buttons (not SMU, which works without scan)."""
        self.simple_apply_btn.setEnabled(enabled)
        self.clocks_apply_btn.setEnabled(enabled)
        self.msglimits_apply_btn.setEnabled(enabled)
        self.od_apply_btn.setEnabled(enabled)

    def _run_with_hardware(self, action_name: str, apply_fn, require_scan=True):
        """Run apply_fn(hw) in background thread. Prevents UI freeze (PP apply does scan_memory)."""
        _log_to_file(f"_run_with_hardware: {action_name} (require_scan={require_scan})")
        if require_scan and not self._can_apply():
            self._log(f"{action_name}: scan not ready.")
            return
        if self._apply_worker is not None and self._apply_worker.isRunning():
            self._log(f"{action_name}: apply already in progress.")
            return
        self._apply_worker = ApplyWorker(action_name, apply_fn, self)
        self._apply_worker.finished_signal.connect(
            self._on_apply_finished, Qt.ConnectionType.QueuedConnection
        )
        self._apply_worker.finished.connect(lambda: setattr(self, "_apply_worker", None))
        self._set_apply_buttons_enabled(False)
        self.scan_status_label.setText(f"{action_name}...")
        self._log(f"{action_name}: running...")
        self._apply_worker.start()

    def _on_apply_finished(self, action_name: str, err: str | None):
        """Handle apply worker completion."""
        if self._can_apply():
            self._set_apply_buttons_enabled(True)
        if err:
            self._log(f"{action_name} failed: {err}")
            self.scan_status_label.setText(f"{action_name} failed.")
        else:
            self._log(f"{action_name} done.")
            addrs = getattr(self.scan_result, "valid_addrs", []) or []
            od = getattr(self.scan_result, "od_table", None)
            if addrs:
                self.scan_status_label.setText(f"Ready — {len(addrs)} PPTable(s) found")
            elif od:
                self.scan_status_label.setText("OD/SMU apply available")
            else:
                self.scan_status_label.setText("Ready")

    def get_simple_settings(self) -> OverclockSettings:
        """Return OverclockSettings from Simple tab (clock + offset only)."""
        return OverclockSettings(
            clock=self.clock_spin.value(),
            offset=self.offset_spin.value(),
            od_ppt=0,
            od_tdc=0,
        )

    def get_detailed_settings(self) -> OverclockSettings:
        """Return OverclockSettings from Detailed tab (all patchable fields)."""
        w = self._detailed_param_widgets
        def _val(key, default=0):
            if key not in w:
                return default
            widget = w[key]
            if hasattr(widget, "value"):
                return widget.value()
            if hasattr(widget, "isChecked"):
                return widget.isChecked()
            return default
        return OverclockSettings(
            game_clock=_val("GameClockAc", self.vbios_values.gameclock_ac),
            boost_clock=_val("BoostClockAc", self.vbios_values.boostclock_ac),
            power_ac=_val("PPT0_AC", self.vbios_values.power_ac),
            power_dc=_val("PPT0_DC", self.vbios_values.power_dc),
            tdc_gfx=_val("TDC_GFX", self.vbios_values.tdc_gfx),
            tdc_soc=_val("TDC_SOC", self.vbios_values.tdc_soc),
            temp_edge=_val("Temp_Edge", 100),
            temp_hotspot=_val("Temp_Hotspot", 110),
            temp_mem=_val("Temp_Mem", 100),
            temp_vr_gfx=_val("Temp_VR_GFX", 115),
            temp_vr_soc=_val("Temp_VR_SOC", 115),
            offset=_val("GfxclkFoffset", 200),
            od_ppt=_val("Ppt", 10),
            od_tdc=_val("Tdc", 0),
            uclk_min=_val("UclkFmin", 0),
            uclk_max=_val("UclkFmax", 0),
            fclk_min=_val("FclkFmin", 0),
            fclk_max=_val("FclkFmax", 0),
            min_clock=_val("MinClock", 0),
            lock_features=_val("LockFeatures", False),
        )

    def get_detailed_pp_patch_values(self) -> dict[str, int]:
        """Return user values for expanded PP patch fields."""
        values: dict[str, int] = {}
        for key in self._pp_patch_keys:
            widget = self._detailed_param_widgets.get(key)
            if widget is None or not hasattr(widget, "value"):
                continue
            values[key] = int(widget.value())
        return values

    def _on_apply_simple(self):
        """Apply Simple tab: clock + offset (patches PPTable clocks via RAM + SMU)."""
        settings = self.get_simple_settings()
        self._log(f"Simple Apply: clock={settings.clock} MHz, offset={settings.offset} MHz")

        def do_apply(hw):
            vb = _get_vbios_values()
            if vb is None:
                vb = self.vbios_values
            inpout, smu, virt = hw["inpout"], hw["smu"], hw["virt"]
            if self.scan_result and self.scan_result.valid_addrs:
                results = apply_clocks_only(
                    inpout, smu, self.scan_result, settings,
                    vbios_values=vb,
                    progress_callback=lambda pct, msg: self._log(msg),
                )
                self._log(f"Clocks: {results['patched_count']} patched, "
                          f"{results['skipped_count']} skipped.")

        self._run_with_hardware("Simple Apply", do_apply)

    def _on_apply_pp(self):
        """Apply PP section: clocks + MsgLimits (patch RAM, send SMU commands)."""
        settings = self.get_detailed_settings()
        pp_values = self.get_detailed_pp_patch_values()
        self._log(f"Apply PP: Game={settings._game_clock()} Boost={settings._boost_clock()} MHz, PPT={settings._power_ac()}W")

        def do_apply(hw):
            vb = _get_vbios_values()
            if vb is None:
                vb = self.vbios_values
            log_cb = lambda pct, msg: self._log(msg)
            if self.scan_result and self.scan_result.valid_addrs:
                clk_res = apply_clocks_only(
                    hw["inpout"], hw["smu"], self.scan_result, settings,
                    vbios_values=vb, progress_callback=log_cb,
                )
                ml_res = apply_msglimits_only(
                    hw["inpout"], hw["smu"], self.scan_result, settings, ScanOptions(),
                    vbios_values=vb, progress_callback=log_cb,
                )
                custom_res = apply_pp_custom_fields(
                    hw["inpout"],
                    self.scan_result,
                    pp_values,
                    self._pp_ram_offset_map,
                    progress_callback=log_cb,
                )
                self._log(f"PP: clocks {clk_res['patched_count']} patched / "
                          f"{clk_res['skipped_count']} skipped, "
                          f"MsgLimits {ml_res['patched_count']} patched / "
                          f"{ml_res['skipped_count']} skipped, "
                          f"Custom {custom_res['field_writes']} writes.")
            else:
                self._log("PP: no valid addresses to patch.")

        self._run_with_hardware("Apply PP", do_apply)

    def _on_apply_msglimits(self):
        """Legacy: Apply PP handles both; kept for _set_apply_buttons_enabled compatibility."""
        self._on_apply_pp()

    def _on_apply_od(self):
        settings = self.get_detailed_settings()
        self._log(f"OD Apply: offset={settings.offset} MHz, PPT={settings.od_ppt}%, TDC={settings.od_tdc}%")

        def do_apply(hw):
            apply_od_table_only(hw["smu"], hw["virt"], settings)
            self._log("OD table applied.")

        self._run_with_hardware("OD Apply", do_apply)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RDNA4 Overclock")
        self.setMinimumSize(520, 480)
        # Start at 1000x1000, clamped to screen dimensions
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w = min(1000, geom.width())
            h = min(1000, geom.height())
            self.resize(w, h)
        else:
            self.resize(1000, 1000)

        self.stacked = QStackedWidget()
        self.setCentralWidget(self.stacked)

        self.gate = VbiosGateWidget()
        self.gate.set_on_vbios_ready(self._on_vbios_ready)
        self.stacked.addWidget(self.gate)

        # Try to load existing VBIOS on startup
        self._try_load_vbios()

    def _try_load_vbios(self):
        """If bios/vbios.rom exists, decode and parse. Show main UI."""
        if not os.path.isfile(DEFAULT_VBIOS_PATH):
            self.stacked.setCurrentWidget(self.gate)
            return

        rom_bytes, was_encoded = read_vbios_decoded(DEFAULT_VBIOS_PATH)
        if rom_bytes is None:
            self.stacked.setCurrentWidget(self.gate)
            return

        if not was_encoded:
            write_vbios_encoded(DEFAULT_VBIOS_PATH, rom_bytes)

        diag: list[str] = []
        vals = parse_vbios_from_bytes(rom_bytes, rom_path=DEFAULT_VBIOS_PATH, diagnostic_out=diag)
        used_defaults = vals is None
        if used_defaults:
            vals = parse_vbios_or_defaults(DEFAULT_VBIOS_PATH)

        self._show_main_ui(
            vals, used_defaults=used_defaults,
            diagnostic_lines=diag if used_defaults else None,
        )

    def _on_vbios_ready(self, vbios_values: VbiosValues):
        self._show_main_ui(vbios_values, used_defaults=False)

    def _show_main_ui(self, vbios_values: VbiosValues, *, used_defaults: bool = False, diagnostic_lines: list[str] | None = None):
        if self.stacked.count() < 2:
            main_ui = MainOverclockWidget(vbios_values, used_defaults=used_defaults, diagnostic_lines=diagnostic_lines)
            self.stacked.addWidget(main_ui)
        else:
            main_ui = self.stacked.widget(1)
            main_ui.vbios_values = vbios_values
            main_ui.used_defaults = used_defaults
            main_ui.diagnostic_lines = diagnostic_lines
        self.stacked.setCurrentWidget(main_ui)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    _log_to_file("main(): starting application")
    if getattr(sys, "frozen", False):
        ensure_driver_files_copied()
    app = QApplication(sys.argv)
    app.setApplicationName("RDNA4 Overclock")
    win = MainWindow()
    win.show()
    _log_to_file("main(): window shown, entering event loop")
    ret = app.exec()
    _log_to_file(f"main(): event loop exited with code {ret}")
    return ret


if __name__ == "__main__":
    sys.exit(main())
