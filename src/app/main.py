"""
Adrenalift -- PySide6 Main Window
==========================================

Main application window with:
  - VBIOS gate screen: file picker + copy to bios/ when no VBIOS present
  - Main overclock UI: Simple Settings, PP, SMU (with OD sub-tab), Memory, Registry Patch, log panel, Apply button
"""

from __future__ import annotations

# CRITICAL: On Windows + PyInstaller, multiprocessing child processes re-execute
# this entry-point script.  Call freeze_support() BEFORE any module-level side
# effects (logging, atexit, Qt) so child workers exit immediately instead of
# spawning duplicate GUI instances (fork bomb).
import multiprocessing as _mp
if __name__ == "__main__":
    _mp.freeze_support()

import atexit
import faulthandler
import json
import logging
import os
import re
import sys
import threading
import time
import traceback

from src.app.settings import settings
from src.app.startup_task import (
    is_startup_enabled,
    enable_startup,
    disable_startup,
    ensure_startup_points_to_current,
)
from src.app.help_texts import (
    SIMPLE_HOW_IT_WORKS_HTML,
    PP_HELP_HTML,
    OD_HELP_HTML,
    ESCAPE_OD_HELP_HTML,
    STATUS_CHEATSHEET,
    CLOCK_CHEATSHEET,
    CONTROLS_CHEATSHEET,
    FEATURES_CHEATSHEET,
    THROTTLERS_CHEATSHEET,
    METRICS_CHEATSHEET,
    TABLES_CHEATSHEET,
    REG_CHEATSHEET_HTML,
)
from src.app.ui_helpers import (
    make_spinbox,
    make_cheatsheet_button,
    make_set_button,
    make_current_value_label,
    add_param_row,
)
from src.app.workers import (
    ApplyWorker,
    EscapeWorker,
    RegistryPatchWorker,
    MemoryRefreshWorker,
    MetricsRefreshWorker,
    SmuTableReadWorker,
    DetailedRefreshWorker,
    ScanThread,
    VramDmaScanWorker,
    PfeWorker,
)

from PySide6.QtCore import Qt, QSize, QThread, QTimer, Signal, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
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
    QTreeWidget,
    QTreeWidgetItem,
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
# Version info -- loaded from version.json (bundled in _MEIPASS or project root)
# ---------------------------------------------------------------------------
_VERSION_CANDIDATES = [
    os.path.join(getattr(sys, "_MEIPASS", ""), "version.json"),
    os.path.join(_script_dir, "version.json"),
]
APP_VERSION = "?"
APP_BUILD   = "?"
for _vp in _VERSION_CANDIDATES:
    if os.path.isfile(_vp):
        try:
            with open(_vp, "r", encoding="utf-8") as _vf:
                _version_data = json.load(_vf)
            APP_VERSION = str(_version_data.get("version", "?"))
            APP_BUILD   = str(_version_data.get("build", "?"))
        except Exception:
            pass
        break

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

# Enable faulthandler so native crashes (SIGSEGV, SIGABRT, etc.) dump a
# traceback to the log file instead of vanishing silently.
try:
    _fault_fh = open(_LOG_FILE, "a", encoding="utf-8")
    faulthandler.enable(file=_fault_fh, all_threads=True)
except Exception:
    faulthandler.enable(all_threads=True)


def _log_to_file(msg: str):
    """Write a single log line to the persistent log file."""
    try:
        _file_logger.info(msg)
        _fh.flush()
    except Exception:
        pass


def _log_exception_to_file(context: str = ""):
    """Log the current exception traceback to the persistent log file."""
    try:
        tb = traceback.format_exc()
        _file_logger.error(f"EXCEPTION ({context}):\n{tb}")
        _fh.flush()
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


def _install_threading_exception_hook():
    """Catch unhandled exceptions on non-main threads (Python 3.8+)."""
    def _thread_hook(args):
        try:
            tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            _file_logger.critical(
                f"UNHANDLED THREAD EXCEPTION (thread={args.thread}):\n{tb_text}"
            )
            _fh.flush()
        except Exception:
            pass
    threading.excepthook = _thread_hook

_install_threading_exception_hook()


def _install_qt_message_handler():
    """Redirect Qt internal warnings/errors to the log file."""
    _msg_type_names = {
        QtMsgType.QtDebugMsg: "QtDebug",
        QtMsgType.QtInfoMsg: "QtInfo",
        QtMsgType.QtWarningMsg: "QtWarning",
        QtMsgType.QtCriticalMsg: "QtCritical",
        QtMsgType.QtFatalMsg: "QtFatal",
    }
    def _handler(msg_type, context, message):
        label = _msg_type_names.get(msg_type, f"Qt({msg_type})")
        loc = ""
        if context.file:
            loc = f" [{context.file}:{context.line}]"
        try:
            _file_logger.warning(f"{label}{loc}: {message}")
            if msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                _fh.flush()
        except Exception:
            pass
    qInstallMessageHandler(_handler)

_install_qt_message_handler()


_atexit_clean = False

def _atexit_handler():
    if _atexit_clean:
        _file_logger.info("Session ended (clean exit)")
    else:
        _file_logger.critical("Session ended (atexit without clean flag — possible crash or kill)")
    _fh.flush()

atexit.register(_atexit_handler)


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
    ScanResult,
    cleanup_hardware,
    init_hardware,
    apply_clocks_only,
    _apply_pp_field_groups,
    apply_od_table_only,
    apply_od_single_field,
    apply_smu_features_only,
    patch_pp_single_field,
    read_od,
    read_metrics,
)
from src.engine.smu import PPSMC, PPCLK, SMU_FEATURE, _CLK_NAMES, _FEATURE_NAMES, _FEATURE_NAMES_LOW
from src.engine.smu_metrics import (
    PPCLK_NAMES, SVI_PLANE_NAMES, TEMP_NAMES, THROTTLER_COUNT,
    THROTTLER_NAMES, D3HOT_SEQUENCE_NAMES,
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
from src.io.escape_structures import Od8Setting, OD8_RDNA4_FIELD_MAP, OdFail

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
    ("Throttling (%)", [f"Throttle_{THROTTLER_NAMES[i]}" for i in range(THROTTLER_COUNT)]
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

        title = QLabel("Adrenalift")
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
# Main Overclock UI
# ---------------------------------------------------------------------------


class MainOverclockWidget(QWidget):
    """Main UI with Simple/Detailed tabs, log panel, progress bar, and Apply button."""

    log_request_signal = Signal(str)
    feature_result_signal = Signal(int, bool, bool, str)  # (bit, verified_ok, actual_enabled, message)
    allowed_mask_signal = Signal(object)  # dict with probe results

    def __init__(self, vbios_values: VbiosValues, *, used_defaults: bool = False, diagnostic_lines: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.log_request_signal.connect(self._log_gui, Qt.ConnectionType.QueuedConnection)
        self.feature_result_signal.connect(self._on_feature_result, Qt.ConnectionType.QueuedConnection)
        self.allowed_mask_signal.connect(self._on_allowed_mask_result, Qt.ConnectionType.QueuedConnection)
        self.vbios_values = vbios_values
        self.used_defaults = used_defaults
        self.diagnostic_lines = diagnostic_lines or []
        self.scan_result: ScanResult | None = None
        self._pp_ram_offset_map: dict[str, dict] = {}
        self._pp_patch_keys: set[str] = set()
        layout = QVBoxLayout(self)

        # Info banner (VBIOS clocks left, version right)
        if used_defaults:
            info_text = (
                "VBIOS structure not recognized — using fallback defaults. "
                "Delete bios/vbios.rom and restart to select a different ROM.\n"
                f"{vbios_values.summary()}"
            )
        else:
            info_text = f"VBIOS: {vbios_values.summary()}"

        banner_row = QHBoxLayout()
        banner_row.setContentsMargins(0, 0, 0, 0)
        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet("background: #2a2a2a; color: #ddd; padding: 8px; border-radius: 4px;")
        banner_row.addWidget(info, stretch=1)

        ver_label = QLabel(f"Version: {APP_VERSION}\nBuild: {APP_BUILD}")
        ver_label.setStyleSheet(
            "background: #2a2a2a; color: #888; padding: 8px; border-radius: 4px; font-size: 9pt;"
        )
        ver_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        banner_row.addWidget(ver_label)
        layout.addLayout(banner_row)

        # Tabs
        self.tabs = QTabWidget()
        self.simple_tab = QWidget()
        self.pp_tab = QWidget()
        self.smu_tab = QWidget()
        self.memory_tab = QWidget()
        self.registry_tab = QWidget()
        self.escape_tab = QWidget()
        self._setup_simple_tab()
        self._setup_detailed_tabs()
        self._setup_smu_tab()
        self._setup_memory_tab()
        self._setup_registry_tab()
        self._setup_escape_tab()
        self.tabs.addTab(self.simple_tab, "Simple Settings")
        self.tabs.addTab(self.pp_tab, "PP")
        self.tabs.addTab(self.smu_tab, "SMU")
        self.tabs.addTab(self.memory_tab, "Memory")
        self.tabs.addTab(self.registry_tab, "Registry Patch")
        self.tabs.addTab(self.escape_tab, "Escape OD")
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
        scan_workers_label = QLabel("Workers:")
        scan_workers_label.setStyleSheet("font-size: 9pt;")
        scan_row.addWidget(scan_workers_label)
        self.scan_workers_spin = QSpinBox()
        self.scan_workers_spin.setRange(1, 32)
        saved_workers = settings.get("defaults.scan_workers")
        self.scan_workers_spin.setValue(
            saved_workers if isinstance(saved_workers, int) else min(4, os.cpu_count() or 4)
        )
        self.scan_workers_spin.setToolTip(
            "Number of scan worker threads. More workers = faster scan but heavier system load."
        )
        self.scan_workers_spin.valueChanged.connect(
            lambda v: settings.set("defaults.scan_workers", v)
        )
        scan_row.addWidget(self.scan_workers_spin)
        self.rescan_btn = QPushButton("Scan")
        self.rescan_btn.setToolTip("Scan memory for PPTable addresses")
        self.rescan_btn.clicked.connect(self._on_rescan)
        self.rescan_btn.setEnabled(True)
        scan_row.addWidget(self.rescan_btn)
        layout.addLayout(scan_row)

        self.vram_progress_bar = QProgressBar()
        self.vram_progress_bar.setRange(0, 100)
        self.vram_progress_bar.setValue(0)
        self.vram_progress_bar.setTextVisible(True)
        layout.addWidget(self.vram_progress_bar)

        vram_scan_row = QHBoxLayout()
        self.vram_status_label = QLabel(
            "Ready — press VRAM Scan to find DMA buffer"
        )
        self.vram_status_label.setStyleSheet("color: #888; font-size: 9pt;")
        vram_scan_row.addWidget(self.vram_status_label)
        vram_scan_row.addStretch()
        self.vram_scan_btn = QPushButton("VRAM Scan")
        self.vram_scan_btn.setToolTip("Scan GPU VRAM for DMA buffer offset")
        self.vram_scan_btn.setEnabled(True)
        self.vram_scan_btn.clicked.connect(self._on_vram_scan)
        vram_scan_row.addWidget(self.vram_scan_btn)
        layout.addLayout(vram_scan_row)

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
        self._vram_scan_worker = None
        self._set_apply_buttons_enabled(False)
        self._apply_worker = None
        self._auto_apply_pending = False

        ensure_startup_points_to_current()

        if settings.get("defaults.scan_on_startup", False):
            self._auto_apply_pending = bool(
                settings.get("defaults.apply_after_scan_on_startup", False)
            )
            QTimer.singleShot(200, self._on_rescan)

    def _setup_simple_tab(self):
        outer = QVBoxLayout(self.simple_tab)

        _, hint_row = make_cheatsheet_button(
            self, "How It Works", SIMPLE_HOW_IT_WORKS_HTML,
            self._show_smu_cheatsheet,
            tooltip="How PP Table RAM patching works",
            label="How it works",
        )
        outer.addLayout(hint_row)

        form = QFormLayout()

        saved_clock = settings.get("defaults.simple_clock_mhz", 3500)
        self.clock_spin = make_spinbox(500, 5000, saved_clock, " MHz")

        clock_row = QHBoxLayout()
        clock_row.addWidget(self.clock_spin)
        save_default_btn = QPushButton("Save as default")
        save_default_btn.setToolTip("Remember the current clock value for next launch")
        save_default_btn.clicked.connect(self._on_save_clock_default)
        clock_row.addWidget(save_default_btn)
        clock_row.addStretch()
        form.addRow("Clock:", clock_row)

        outer.addLayout(form)
        self.simple_apply_btn = QPushButton("Apply")
        self.simple_apply_btn.clicked.connect(self._on_apply_simple)
        outer.addWidget(self.simple_apply_btn)

        startup_group = QGroupBox("Startup automation")
        startup_vbox = QVBoxLayout(startup_group)

        self.run_on_startup_cb = QCheckBox("Run on Windows startup")
        self.run_on_startup_cb.setToolTip(
            "Launch this application automatically when you log in to Windows"
        )
        self.run_on_startup_cb.setChecked(is_startup_enabled())
        self.run_on_startup_cb.toggled.connect(self._on_run_on_startup_toggled)
        startup_vbox.addWidget(self.run_on_startup_cb)

        self.scan_on_startup_cb = QCheckBox("Scan on startup")
        self.scan_on_startup_cb.setToolTip(
            "Automatically start a PPTable scan when the application launches"
        )
        self.scan_on_startup_cb.setChecked(
            bool(settings.get("defaults.scan_on_startup", False))
        )
        self.scan_on_startup_cb.toggled.connect(self._on_scan_on_startup_toggled)
        startup_vbox.addWidget(self.scan_on_startup_cb)

        self.apply_after_scan_cb = QCheckBox("Apply clocks after scan")
        self.apply_after_scan_cb.setToolTip(
            "Automatically apply the saved clock value once the startup scan finishes"
        )
        self.apply_after_scan_cb.setChecked(
            bool(settings.get("defaults.apply_after_scan_on_startup", False))
        )
        self.apply_after_scan_cb.setEnabled(self.scan_on_startup_cb.isChecked())
        self.apply_after_scan_cb.toggled.connect(
            lambda v: settings.set("defaults.apply_after_scan_on_startup", v)
        )
        startup_vbox.addWidget(self.apply_after_scan_cb)

        outer.addWidget(startup_group)
        outer.addStretch()

    def _on_scan_progress(self, pct: float, msg: str):
        self.progress_bar.setValue(int(pct))
        self.scan_status_label.setText(msg)
        if "Search pattern" in msg or "VBIOS" in msg or "hardcoded" in msg \
                or "DMA" in msg:
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
            default_vbios_path=DEFAULT_VBIOS_PATH,
            num_threads=self.scan_workers_spin.value(),
        )
        self._scan_thread.progress_signal.connect(
            self._on_scan_progress, Qt.ConnectionType.QueuedConnection
        )
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
        for key, cv_label in cv_widgets.items():
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
                    cv_label.setText("Unavailable")
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
                pass
            else:
                if isinstance(ram_data, dict) and key in ram_data:
                    unit = self._param_unit.get(key, "")
                    cv_label.setText(_fmt(ram_data[key], unit))
                    updated += 1
                elif ram_data is not None:
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

    def _on_feature_result(self, bit: int, verified_ok: bool, actual_enabled: bool, message: str):
        """Update per-feature result label after toggle verification (main thread)."""
        result_labels = getattr(self, "_smu_feature_result_labels", {})
        label = result_labels.get(bit)
        if label is not None:
            if verified_ok:
                label.setText("VERIFIED")
                label.setStyleSheet("color: #2ecc71; font-weight: bold;")
            else:
                label.setText("FAILED")
                label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            label.setToolTip(message)
        state_labels = getattr(self, "_smu_feature_state_labels", {})
        sl = state_labels.get(bit)
        if sl is not None:
            sl.setText("ON" if actual_enabled else "OFF")
        cb = self._detailed_param_widgets.get(f"SMU_FEAT_{bit}")
        if cb is not None:
            cb.setChecked(actual_enabled)

    def _on_allowed_mask_result(self, result: dict):
        """Update per-feature Control column after allowed-mask probe (main thread)."""
        ctrl_labels = getattr(self, "_smu_feature_control_labels", {})
        if not ctrl_labels or not result:
            return
        self._allowed_mask_probed = True
        allowed_mask = result.get("allowed_mask", 0)
        dedicated_bits = result.get("dedicated_msg_bits", set())
        enable_ok = result.get("enable_all_ok", False)

        for bit, label in ctrl_labels.items():
            is_allowed = bool(allowed_mask & (1 << bit))
            has_dedicated = bit in dedicated_bits

            if is_allowed:
                text = "Allowed"
                style = "color: #2ecc71; font-weight: bold;"
                tip = f"Bit {bit}: in the firmware allowed mask — controllable via Enable/DisableSmuFeatures"
            else:
                text = "Locked"
                style = "color: #888;"
                tip = f"Bit {bit}: NOT in the allowed mask — firmware-locked, Enable/DisableSmuFeatures ignored"

            if has_dedicated:
                text += " +msg"
                tip += "\n\nDedicated SMU messages available (bypasses feature mask)"

            if not enable_ok:
                tip += "\n\nNote: EnableAllSmuFeatures was rejected — mask may be incomplete"

            label.setText(text)
            label.setStyleSheet(style)
            label.setToolTip(tip)

    def _on_scan_finished(self, result: ScanResult | None):
        self.scan_result = result
        if result is None:
            self._auto_apply_pending = False
            self._log("Scan failed (no result).")
            self.scan_status_label.setText("Scan failed.")
            self._set_apply_buttons_enabled(False)
            self._update_memory_placeholder("No addresses")
            self.progress_bar.setValue(100)
            self.rescan_btn.setEnabled(True)
            return
        if result.error:
            self._auto_apply_pending = False
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

        if self._auto_apply_pending:
            self._auto_apply_pending = False
            if self._can_apply():
                self._log("Auto-apply: applying saved clock after startup scan...")
                QTimer.singleShot(100, self._on_apply_simple)
            else:
                self._log("Auto-apply: skipped — scan did not produce usable results.")

    # ------------------------------------------------------------------
    # VRAM DMA scan
    # ------------------------------------------------------------------

    def _on_vram_scan(self):
        if self._vram_scan_worker is not None and self._vram_scan_worker.isRunning():
            return
        self.vram_scan_btn.setEnabled(False)
        self.vram_progress_bar.setValue(0)
        self.vram_status_label.setText("Scanning VRAM for DMA buffer...")
        self._vram_scan_worker = VramDmaScanWorker(
            lambda: _get_vbios_values(),
            default_vbios_path=DEFAULT_VBIOS_PATH,
        )
        self._vram_scan_worker.progress_signal.connect(
            self._on_vram_scan_progress, Qt.ConnectionType.QueuedConnection
        )
        self._vram_scan_worker.finished_signal.connect(self._on_vram_scan_finished)
        self._vram_scan_worker.log_signal.connect(
            self._log_gui, Qt.ConnectionType.QueuedConnection
        )
        self._vram_scan_worker.start()
        self._log("VRAM DMA scan started...")

    def _on_vram_scan_progress(self, pct: float, msg: str):
        self.vram_progress_bar.setValue(int(pct))
        self.vram_status_label.setText(msg)

    def _on_vram_scan_finished(self, result):
        self.vram_scan_btn.setEnabled(True)
        self.vram_progress_bar.setValue(100)
        if result is not None:
            offset = result.get("offset", 0)
            method = result.get("method", "unknown")
            vram_mb = result.get("vram_size", 0) / (1024 * 1024)
            self.vram_status_label.setText(
                f"Found DMA buffer at 0x{offset:X}  (method: {method})"
            )
            self._log(
                f"VRAM scan complete: DMA buffer at offset 0x{offset:X} "
                f"(method: {method}, VRAM: {vram_mb:.0f} MB)"
            )
        else:
            self.vram_status_label.setText("VRAM scan finished — DMA buffer not found")
            self._log("VRAM scan complete: DMA buffer not found.")

    def _setup_detailed_tabs(self):
        """Set up PP tab and OD sub-tab (OD is added to SMU inner tabs)."""
        vb = self.vbios_values

        # smu_key: "od" = from od_table (use table_key as attr), "gfxclk"/"ppt"/"temp" = from metrics
        self._param_smu_key = {}
        self._param_unit = {}
        self._detailed_param_widgets = {}
        self._param_current_value_widget = {}  # key -> QLabel for Current value column
        self._detailed_tables = {}

        def _add_smu_row(table, human, key, unit, vb_val, widget, smu_key=None, row_apply_fn=None):
            info = add_param_row(
                table, human, key, unit, widget,
                apply_fn=row_apply_fn, apply_label=human,
                run_with_hardware=self._run_with_hardware,
            )
            self._param_current_value_widget[key] = info["cv_label"]
            self._param_unit[key] = info["unit_str"]
            self._detailed_param_widgets[key] = widget
            if smu_key:
                self._param_smu_key[key] = smu_key

        # (1) PP Section: full decoded PP tree (all UPP fields)
        pp_grp = QGroupBox("PP — PowerPlay Table")
        pp_tree = QTreeWidget()
        pp_tree.setColumnCount(5)
        pp_tree.setHeaderLabels(["Field", "VBIOS value", "Current value", "Custom input", ""])
        pp_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        pp_tree.header().setStretchLastSection(True)
        self._pp_tree = pp_tree
        self._pp_ram_offset_map = {}
        self._pp_patch_keys = set()
        rom_bytes, _ = read_vbios_decoded(DEFAULT_VBIOS_PATH)
        decoded = decode_pp_table_full(rom_bytes, rom_path=DEFAULT_VBIOS_PATH) if rom_bytes else None
        decoded_tree = decoded.data if decoded else None

        _bc_pp_off = getattr(self.vbios_values, 'baseclock_pp_offset', 0)

        _SKIP_PAT = re.compile(
            r'^(Padding|Spare|Reserve|MmHubPadding|PADDING_)', re.IGNORECASE,
        )
        _PP_SMU_KEY_MAP = {
            "smc_pptable/SkuTable/DriverReportedClocks/GameClockAc": "gfxclk",
            "smc_pptable/SkuTable/MsgLimits/Power/0/0": "ppt",
            "smc_pptable/SkuTable/MsgLimits/Temperature/0": "temp",
        }

        def _infer_unit(field_name):
            n = field_name.lower()
            if any(p in n for p in ("clock", "freq", "fmin", "fmax", "clk")):
                return "MHz"
            if any(p in n for p in ("power", "ppt", "socketpowerlimit")):
                return "W"
            if any(p in n for p in ("tdc", "edclimit")):
                return "A"
            if any(p in n for p in ("temp", "ctflimit")):
                return "°C"
            if any(p in n for p in ("voltage", "vmax", "vmin")):
                return "mV"
            if "rpm" in n:
                return "RPM"
            return ""

        def _infer_group(path):
            if "DriverReportedClocks/" in path:
                return "clocks"
            if "MsgLimits/" in path:
                return "msglimits"
            segments = path.split("/")
            for seg in segments:
                if seg.startswith("FreqTable") or seg.startswith("Gfxclk"):
                    return "freq"
            if "CustomSkuTable/" in path:
                low = path.lower()
                if any(k in low for k in ("fan", "acoustic", "pwm", "zerorpm")):
                    return "fan"
                if any(k in low for k in ("temp", "ctf")):
                    return "temps"
                return "power"
            if "BoardTable/" in path:
                return "board"
            if any(k in path.lower() for k in ("voltage", "vmax", "vmin")):
                return "voltage"
            return "other"

        def _mk_pp_field_apply(full_path, spin):
            """Closure: read spin value and patch one PP field in RAM."""
            def _apply(hw):
                meta = self._pp_ram_offset_map.get(full_path)
                if not meta:
                    return {"ok": False, "msg": f"No offset for {full_path}"}
                return patch_pp_single_field(
                    hw["inpout"], self.scan_result,
                    meta["offset"], spin.value(), meta.get("type", "H"),
                )
            return _apply

        def _add_pp_leaf(parent_item, name, leaf, full_path):
            _QSPIN_MAX = (1 << 31) - 1
            vb_val = int(leaf.get("value", 0))
            raw_offset = int(leaf.get("offset", -1))
            field_type = str(leaf.get("type", "H"))
            unit = _infer_unit(name)
            group = _infer_group(full_path)
            smu_key = _PP_SMU_KEY_MAP.get(full_path)

            if field_type in ("Q", "q"):
                max_val = _QSPIN_MAX
            elif field_type in ("I", "L", "i", "l"):
                max_val = min(2_000_000_000, _QSPIN_MAX)
            elif field_type in ("B", "b"):
                max_val = 255
            elif field_type in ("H", "h"):
                max_val = 65535
            else:
                max_val = _QSPIN_MAX
            vb_val = max(0, min(vb_val, max_val))
            widget = make_spinbox(0, max_val, vb_val, f" {unit}" if unit else "")

            item = QTreeWidgetItem(parent_item, [name, str(vb_val)])
            cv_label = QLabel("---")
            pp_tree.setItemWidget(item, 2, cv_label)
            pp_tree.setItemWidget(item, 3, widget)

            self._param_current_value_widget[full_path] = cv_label
            self._param_unit[full_path] = f" {unit}" if unit else ""
            self._detailed_param_widgets[full_path] = widget
            self._pp_patch_keys.add(full_path)
            if smu_key:
                self._param_smu_key[full_path] = smu_key
            if raw_offset >= 0:
                self._pp_ram_offset_map[full_path] = {
                    "offset": raw_offset - _bc_pp_off,
                    "type": field_type,
                    "group": group,
                }
                apply_fn = _mk_pp_field_apply(full_path, widget)
                btn = make_set_button(name, apply_fn, self._run_with_hardware, max_width=40)
                pp_tree.setItemWidget(item, 4, btn)

        def _populate_pp_tree(parent_item, node, path_prefix):
            if not isinstance(node, dict):
                return
            if "entries" in node and isinstance(node["entries"], list):
                for idx, child in enumerate(node["entries"]):
                    child_path = f"{path_prefix}/{idx}" if path_prefix else str(idx)
                    if isinstance(child, dict) and "value" in child and "offset" in child:
                        _add_pp_leaf(parent_item, str(idx), child, child_path)
                    else:
                        item = QTreeWidgetItem(parent_item, [str(idx)])
                        _populate_pp_tree(item, child, child_path)
                return
            for key, child in node.items():
                if _SKIP_PAT.match(str(key)):
                    continue
                child_path = f"{path_prefix}/{key}" if path_prefix else str(key)
                if isinstance(child, dict):
                    if "value" in child and "offset" in child:
                        _add_pp_leaf(parent_item, str(key), child, child_path)
                    else:
                        item = QTreeWidgetItem(parent_item, [str(key)])
                        _populate_pp_tree(item, child, child_path)
                elif isinstance(child, list):
                    item = QTreeWidgetItem(parent_item, [str(key)])
                    for idx, elem in enumerate(child):
                        elem_path = f"{child_path}/{idx}"
                        if isinstance(elem, dict) and "value" in elem and "offset" in elem:
                            _add_pp_leaf(item, str(idx), elem, elem_path)
                        elif isinstance(elem, dict):
                            sub = QTreeWidgetItem(item, [str(idx)])
                            _populate_pp_tree(sub, elem, elem_path)

        if decoded_tree is not None:
            _populate_pp_tree(pp_tree.invisibleRootItem(), decoded_tree, "")
            root = pp_tree.invisibleRootItem()
            for i in range(root.childCount()):
                top = root.child(i)
                top.setExpanded(True)
                for j in range(top.childCount()):
                    top.child(j).setExpanded(True)
        else:
            def _add_fallback_leaf(name, key, unit, vb_val, smu_key=None):
                sb = make_spinbox(0, 65535, int(vb_val) if vb_val and vb_val != "—" else 0,
                                  f" {unit}" if unit else "")
                item = QTreeWidgetItem(pp_tree.invisibleRootItem(),
                                       [name, str(vb_val) if vb_val else "—"])
                cv_label = QLabel("---")
                pp_tree.setItemWidget(item, 2, cv_label)
                pp_tree.setItemWidget(item, 3, sb)
                self._param_current_value_widget[key] = cv_label
                self._param_unit[key] = f" {unit}" if unit else ""
                self._detailed_param_widgets[key] = sb
                self._pp_patch_keys.add(key)
                if smu_key:
                    self._param_smu_key[key] = smu_key

            _add_fallback_leaf("Game Clock", "GameClockAc", "MHz", vb.gameclock_ac, "gfxclk")
            _add_fallback_leaf("Boost Clock", "BoostClockAc", "MHz", vb.boostclock_ac)
            _add_fallback_leaf("PPT AC", "PPT0_AC", "W", vb.power_ac, "ppt")
            _add_fallback_leaf("PPT DC", "PPT0_DC", "W", vb.power_dc)
            _add_fallback_leaf("TDC GFX", "TDC_GFX", "A", vb.tdc_gfx)
            _add_fallback_leaf("TDC SOC", "TDC_SOC", "A", vb.tdc_soc)
            _add_fallback_leaf("Temp Edge", "Temp_Edge", "°C", vb.temp_edge or 100, "temp")
            _add_fallback_leaf("Temp Hotspot", "Temp_Hotspot", "°C", vb.temp_hotspot or 110)
            _add_fallback_leaf("Temp Mem", "Temp_Mem", "°C", vb.temp_mem or 100)
            _add_fallback_leaf("Temp VR GFX", "Temp_VR_GFX", "°C", vb.temp_vr_gfx or 115)
            _add_fallback_leaf("Temp VR SOC", "Temp_VR_SOC", "°C", vb.temp_vr_soc or 115)

        pp_layout = QVBoxLayout(pp_grp)
        pp_layout.addWidget(pp_tree)
        pp_btn_row = QHBoxLayout()
        self.pp_refresh_btn = QPushButton("Refresh")
        self.pp_refresh_btn.setToolTip("Read live values from RAM and SMU")
        self.pp_refresh_btn.clicked.connect(self._on_detailed_refresh_click)
        self.pp_refresh_btn.setEnabled(True)
        pp_btn_row.addWidget(self.pp_refresh_btn)
        self.clocks_apply_btn = QPushButton("Apply PP")
        self.clocks_apply_btn.setToolTip("Patches all PP table fields in driver RAM (no SMU commands)")
        self.clocks_apply_btn.clicked.connect(self._on_apply_pp)
        self.msglimits_apply_btn = self.clocks_apply_btn
        pp_btn_row.addWidget(self.clocks_apply_btn)
        pp_layout.addLayout(pp_btn_row)

        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setWidget(pp_grp)
        pp_tab_layout = QVBoxLayout(self.pp_tab)
        _, pp_hint_row = make_cheatsheet_button(
            self, "PP", PP_HELP_HTML, self._show_smu_cheatsheet,
            tooltip="How PP Table RAM patching works",
            label="PP \u2014 PowerPlay Table",
        )
        pp_tab_layout.addLayout(pp_hint_row)
        pp_tab_layout.addWidget(pp_scroll)

        # (2) OD Section — extracted into _setup_od_subtab, lives as SMU sub-tab
        self._od_scroll = self._setup_od_subtab(decoded)

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

    def _setup_od_subtab(self, decoded):
        """Build the OD (OverDrive) sub-tab contents and return a QScrollArea."""
        od_limits = extract_od_limits_from_decoded(decoded)
        self._param_od_array_spec = {}

        def _add_od_row(table, human, key, unit, vb_val, smu_key, widget, row_apply_fn=None,
                       feature_bit=None, limits_key=None):
            allowed_str = "\u2014"
            if od_limits is not None and feature_bit is not None:
                allowed_str = "Yes" if od_limits.is_allowed(feature_bit) else "No"
            info = add_param_row(
                table, human, key, unit, widget,
                cv_col=4, widget_col=5, set_col=6,
                extra_items=[(3, QTableWidgetItem(allowed_str))],
                apply_fn=row_apply_fn, apply_label=human,
                run_with_hardware=self._run_with_hardware,
            )
            self._param_smu_key[key] = smu_key
            self._param_current_value_widget[key] = info["cv_label"]
            self._param_unit[key] = info["unit_str"]
            self._detailed_param_widgets[key] = widget
            if od_limits is not None:
                lk = limits_key if limits_key is not None else key
                rng = od_limits.get_range(lk)
                if rng is not None:
                    widget.setRange(rng[0], rng[1])

        def _mk_od_apply(od_attr, feature_bit, spin, label):
            def fn(hw):
                if hw.get("virt") is None:
                    msg = "DMA buffer not available — run DRAM Scan first"
                    self._log(msg)
                    return (False, msg)
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
                if hw.get("virt") is None:
                    msg = "DMA buffer not available — run DRAM Scan first"
                    self._log(msg)
                    return (False, msg)
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

        od_w = QWidget()
        od_top_layout = QVBoxLayout(od_w)

        _, hint_row = make_cheatsheet_button(
            self, "OD", OD_HELP_HTML, self._show_smu_cheatsheet,
            label="OD \u2014 OverDrive Table",
        )
        od_top_layout.addLayout(hint_row)

        od_table = QTableWidget()
        od_table.setColumnCount(7)
        od_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Unit", "Allowed",
            "Current value", "Custom input", "Set",
        ])
        od_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        od_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["OD"] = od_table

        det_gfx_offset = make_spinbox(0, 2000, 200, " MHz")
        _add_od_row(od_table, "Gfxclk Offset", "GfxclkFoffset", "MHz", None, "od", det_gfx_offset,
                    row_apply_fn=_mk_od_apply("GfxclkFoffset", PP_OD_FEATURE_GFXCLK_BIT, det_gfx_offset, "Gfxclk Offset"),
                    feature_bit=PP_OD_FEATURE_GFXCLK_BIT)

        det_od_ppt = make_spinbox(-50, 100, 10, "%")
        _add_od_row(od_table, "PPT %", "Ppt", "%", None, "od", det_od_ppt,
                    row_apply_fn=_mk_od_apply("Ppt", PP_OD_FEATURE_PPT_BIT, det_od_ppt, "PPT %"),
                    feature_bit=PP_OD_FEATURE_PPT_BIT)

        det_od_tdc = make_spinbox(-50, 100, 0, "%")
        _add_od_row(od_table, "TDC %", "Tdc", "%", None, "od", det_od_tdc,
                    row_apply_fn=_mk_od_apply("Tdc", PP_OD_FEATURE_TDC_BIT, det_od_tdc, "TDC %"),
                    feature_bit=PP_OD_FEATURE_TDC_BIT)

        det_uclk_min = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "UCLK min", "UclkFmin", "MHz", None, "od", det_uclk_min,
                    row_apply_fn=_mk_od_apply("UclkFmin", PP_OD_FEATURE_UCLK_BIT, det_uclk_min, "UCLK min"),
                    feature_bit=PP_OD_FEATURE_UCLK_BIT)

        det_uclk_max = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "UCLK max", "UclkFmax", "MHz", None, "od", det_uclk_max,
                    row_apply_fn=_mk_od_apply("UclkFmax", PP_OD_FEATURE_UCLK_BIT, det_uclk_max, "UCLK max"),
                    feature_bit=PP_OD_FEATURE_UCLK_BIT)

        det_fclk_min = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "FCLK min", "FclkFmin", "MHz", None, "od", det_fclk_min,
                    row_apply_fn=_mk_od_apply("FclkFmin", PP_OD_FEATURE_FCLK_BIT, det_fclk_min, "FCLK min"),
                    feature_bit=PP_OD_FEATURE_FCLK_BIT)

        det_fclk_max = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "FCLK max", "FclkFmax", "MHz", None, "od", det_fclk_max,
                    row_apply_fn=_mk_od_apply("FclkFmax", PP_OD_FEATURE_FCLK_BIT, det_fclk_max, "FCLK max"),
                    feature_bit=PP_OD_FEATURE_FCLK_BIT)

        for i in range(PP_NUM_OD_VF_CURVE_POINTS):
            key = f"VoltageOffsetZone{i}"
            self._param_od_array_spec[key] = ("VoltageOffsetPerZoneBoundary", i)
            spin = make_spinbox(-500, 500, 0, " mV")
            _add_od_row(od_table, f"V/F Zone {i}", key, "mV", None, "od", spin,
                        row_apply_fn=_mk_od_apply_array(
                            "VoltageOffsetPerZoneBoundary", i, PP_OD_FEATURE_GFX_VF_CURVE_BIT, spin, f"V/F Zone {i}"),
                        feature_bit=PP_OD_FEATURE_GFX_VF_CURVE_BIT, limits_key="VoltageOffsetPerZoneBoundary")

        det_vdd_gfx = make_spinbox(0, 2000, 0, " mV", "no change")
        _add_od_row(od_table, "VddGfx Vmax", "VddGfxVmax", "mV", None, "od", det_vdd_gfx,
                    row_apply_fn=_mk_od_apply("VddGfxVmax", PP_OD_FEATURE_GFX_VMAX_BIT, det_vdd_gfx, "VddGfx Vmax"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_vdd_soc = make_spinbox(0, 2000, 0, " mV", "no change")
        _add_od_row(od_table, "VddSoc Vmax", "VddSocVmax", "mV", None, "od", det_vdd_soc,
                    row_apply_fn=_mk_od_apply("VddSocVmax", PP_OD_FEATURE_SOC_VMAX_BIT, det_vdd_soc, "VddSoc Vmax"),
                    feature_bit=PP_OD_FEATURE_SOC_VMAX_BIT)

        det_fan_target_temp = make_spinbox(0, 120, 0, " °C", "no change")
        _add_od_row(od_table, "Fan Target Temp", "FanTargetTemperature", "°C", None, "od", det_fan_target_temp,
                    row_apply_fn=_mk_od_apply("FanTargetTemperature", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_target_temp, "Fan Target Temp"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_fan_min_pwm = make_spinbox(0, 255, 0, "", "no change")
        _add_od_row(od_table, "Fan Min PWM", "FanMinimumPwm", "", None, "od", det_fan_min_pwm,
                    row_apply_fn=_mk_od_apply("FanMinimumPwm", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_min_pwm, "Fan Min PWM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_max_op_temp = make_spinbox(0, 127, 0, " °C", "no change")
        _add_od_row(od_table, "Max Op Temp", "MaxOpTemp", "°C", None, "od", det_max_op_temp,
                    row_apply_fn=_mk_od_apply("MaxOpTemp", PP_OD_FEATURE_TEMPERATURE_BIT, det_max_op_temp, "Max Op Temp"),
                    feature_bit=PP_OD_FEATURE_TEMPERATURE_BIT)

        det_gfx_edc = make_spinbox(-32768, 32767, 0, "", "no change")
        _add_od_row(od_table, "Gfx EDC", "GfxEdc", "", None, "od", det_gfx_edc,
                    row_apply_fn=_mk_od_apply("GfxEdc", PP_OD_FEATURE_EDC_BIT, det_gfx_edc, "Gfx EDC"),
                    feature_bit=PP_OD_FEATURE_EDC_BIT)

        det_gfx_pcc = make_spinbox(-32768, 32767, 0, "", "no change")
        _add_od_row(od_table, "Gfx PCC Limit", "GfxPccLimitControl", "", None, "od", det_gfx_pcc,
                    row_apply_fn=_mk_od_apply("GfxPccLimitControl", PP_OD_FEATURE_EDC_BIT, det_gfx_pcc, "Gfx PCC Limit"),
                    feature_bit=PP_OD_FEATURE_EDC_BIT)

        det_gfx_fmax_vmax = make_spinbox(0, 5000, 0, " MHz", "no change")
        _add_od_row(od_table, "Gfxclk Fmax@Vmax", "GfxclkFmaxVmax", "MHz", None, "od", det_gfx_fmax_vmax,
                    row_apply_fn=_mk_od_apply("GfxclkFmaxVmax", PP_OD_FEATURE_GFX_VMAX_BIT, det_gfx_fmax_vmax, "Gfxclk Fmax@Vmax"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_gfx_fmax_vmax_temp = make_spinbox(0, 127, 0, " °C", "no change")
        _add_od_row(od_table, "Gfxclk Fmax@Vmax Temp", "GfxclkFmaxVmaxTemperature", "°C", None, "od", det_gfx_fmax_vmax_temp,
                    row_apply_fn=_mk_od_apply("GfxclkFmaxVmaxTemperature", PP_OD_FEATURE_GFX_VMAX_BIT, det_gfx_fmax_vmax_temp, "Gfxclk Fmax@Vmax Temp"),
                    feature_bit=PP_OD_FEATURE_GFX_VMAX_BIT)

        det_idle_pwr = make_spinbox(0, 255, 0, "", "no change")
        _add_od_row(od_table, "Idle Pwr Saving Ctrl", "IdlePwrSavingFeaturesCtrl", "", None, "od", det_idle_pwr,
                    row_apply_fn=_mk_od_apply("IdlePwrSavingFeaturesCtrl", PP_OD_FEATURE_FULL_CTRL_BIT, det_idle_pwr, "Idle Pwr Saving Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_runtime_pwr = make_spinbox(0, 255, 0, "", "no change")
        _add_od_row(od_table, "Runtime Pwr Saving Ctrl", "RuntimePwrSavingFeaturesCtrl", "", None, "od", det_runtime_pwr,
                    row_apply_fn=_mk_od_apply("RuntimePwrSavingFeaturesCtrl", PP_OD_FEATURE_FULL_CTRL_BIT, det_runtime_pwr, "Runtime Pwr Saving Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        for i in range(NUM_OD_FAN_MAX_POINTS):
            key_pwm = f"FanLinearPwm{i}"
            self._param_od_array_spec[key_pwm] = ("FanLinearPwmPoints", i)
            spin = make_spinbox(0, 255, 0, "", "no change")
            _add_od_row(od_table, f"Fan PWM pt {i}", key_pwm, "%", None, "od", spin,
                        row_apply_fn=_mk_od_apply_array("FanLinearPwmPoints", i, PP_OD_FEATURE_FAN_CURVE_BIT, spin, f"Fan PWM pt {i}"),
                        feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT, limits_key="FanLinearPwmPoints")
            key_temp = f"FanLinearTemp{i}"
            self._param_od_array_spec[key_temp] = ("FanLinearTempPoints", i)
            spin_temp = make_spinbox(0, 127, 0, " °C", "no change")
            _add_od_row(od_table, f"Fan Temp pt {i}", key_temp, "°C", None, "od", spin_temp,
                        row_apply_fn=_mk_od_apply_array("FanLinearTempPoints", i, PP_OD_FEATURE_FAN_CURVE_BIT, spin_temp, f"Fan Temp pt {i}"),
                        feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT, limits_key="FanLinearTempPoints")

        det_acoustic_target = make_spinbox(0, 65535, 0, " RPM", "no change")
        _add_od_row(od_table, "Acoustic Target RPM", "AcousticTargetRpmThreshold", " RPM", None, "od", det_acoustic_target,
                    row_apply_fn=_mk_od_apply("AcousticTargetRpmThreshold", PP_OD_FEATURE_FAN_CURVE_BIT, det_acoustic_target, "Acoustic Target RPM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_acoustic_limit = make_spinbox(0, 65535, 0, " RPM", "no change")
        _add_od_row(od_table, "Acoustic Limit RPM", "AcousticLimitRpmThreshold", " RPM", None, "od", det_acoustic_limit,
                    row_apply_fn=_mk_od_apply("AcousticLimitRpmThreshold", PP_OD_FEATURE_FAN_CURVE_BIT, det_acoustic_limit, "Acoustic Limit RPM"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_fan_zero_rpm = make_spinbox(0, 1, 0, "", "no change")
        _add_od_row(od_table, "Fan Zero RPM Enable", "FanZeroRpmEnable", "", None, "od", det_fan_zero_rpm,
                    row_apply_fn=_mk_od_apply("FanZeroRpmEnable", PP_OD_FEATURE_ZERO_FAN_BIT, det_fan_zero_rpm, "Fan Zero RPM Enable"),
                    feature_bit=PP_OD_FEATURE_ZERO_FAN_BIT)

        det_fan_zero_stop = make_spinbox(0, 127, 0, " °C", "no change")
        _add_od_row(od_table, "Fan Zero RPM Stop Temp", "FanZeroRpmStopTemp", "°C", None, "od", det_fan_zero_stop,
                    row_apply_fn=_mk_od_apply("FanZeroRpmStopTemp", PP_OD_FEATURE_ZERO_FAN_BIT, det_fan_zero_stop, "Fan Zero RPM Stop Temp"),
                    feature_bit=PP_OD_FEATURE_ZERO_FAN_BIT)

        det_fan_mode = make_spinbox(0, 255, 0, "", "no change")
        _add_od_row(od_table, "Fan Mode", "FanMode", "", None, "od", det_fan_mode,
                    row_apply_fn=_mk_od_apply("FanMode", PP_OD_FEATURE_FAN_CURVE_BIT, det_fan_mode, "Fan Mode"),
                    feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT)

        det_advanced_od = make_spinbox(0, 1, 0, "", "no change")
        _add_od_row(od_table, "Advanced OD Mode", "AdvancedOdModeEnabled", "", None, "od", det_advanced_od,
                    row_apply_fn=_mk_od_apply("AdvancedOdModeEnabled", PP_OD_FEATURE_FULL_CTRL_BIT, det_advanced_od, "Advanced OD Mode"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_gfx_volt_full = make_spinbox(0, 65535, 0, " mV", "no change")
        _add_od_row(od_table, "Gfx Voltage Full Ctrl", "GfxVoltageFullCtrlMode", "mV", None, "od", det_gfx_volt_full,
                    row_apply_fn=_mk_od_apply("GfxVoltageFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_gfx_volt_full, "Gfx Voltage Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_soc_volt_full = make_spinbox(0, 65535, 0, " mV", "no change")
        _add_od_row(od_table, "Soc Voltage Full Ctrl", "SocVoltageFullCtrlMode", "mV", None, "od", det_soc_volt_full,
                    row_apply_fn=_mk_od_apply("SocVoltageFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_soc_volt_full, "Soc Voltage Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_gfxclk_full = make_spinbox(0, 5000, 0, " MHz", "no change")
        _add_od_row(od_table, "Gfxclk Full Ctrl", "GfxclkFullCtrlMode", "MHz", None, "od", det_gfxclk_full,
                    row_apply_fn=_mk_od_apply("GfxclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_gfxclk_full, "Gfxclk Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_uclk_full = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "UCLK Full Ctrl", "UclkFullCtrlMode", "MHz", None, "od", det_uclk_full,
                    row_apply_fn=_mk_od_apply("UclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_uclk_full, "UCLK Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        det_fclk_full = make_spinbox(0, 3000, 0, " MHz", "no change")
        _add_od_row(od_table, "FCLK Full Ctrl", "FclkFullCtrlMode", "MHz", None, "od", det_fclk_full,
                    row_apply_fn=_mk_od_apply("FclkFullCtrlMode", PP_OD_FEATURE_FULL_CTRL_BIT, det_fclk_full, "FCLK Full Ctrl"),
                    feature_bit=PP_OD_FEATURE_FULL_CTRL_BIT)

        od_top_layout.addWidget(od_table)
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
        od_top_layout.addLayout(od_btn_row)

        od_scroll = QScrollArea()
        od_scroll.setWidgetResizable(True)
        od_scroll.setWidget(od_w)
        return od_scroll

    def _setup_smu_tab(self):
        """Set up the SMU tab with a nested QTabWidget containing 7 sub-tabs:
        Status, Clock Limits, Controls, Throttlers, Features, Tables, OD.
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
            info = add_param_row(
                table, human, key, unit, widget,
                apply_fn=row_apply_fn, apply_label=human,
                run_with_hardware=self._run_with_hardware,
            )
            self._param_current_value_widget[key] = info["cv_label"]
            self._param_unit[key] = info["unit_str"]
            self._detailed_param_widgets[key] = widget
            if smu_key:
                self._param_smu_key[key] = smu_key

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
            _, row = make_cheatsheet_button(
                self, tab_title, html_content, self._show_smu_cheatsheet,
            )
            layout_target.addLayout(row)

        # ==================================================================
        # Sub-tab 1: Status (read-only, 2-column Name | Value)
        # ==================================================================
        self._smu_status_labels = {}

        status_w = QWidget()
        status_lay = QVBoxLayout(status_w)
        _add_cheatsheet_btn(status_lay, "Status", STATUS_CHEATSHEET)
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
        clock_w = QWidget()
        clock_lay = QVBoxLayout(clock_w)
        _add_cheatsheet_btn(clock_lay, "Clock Limits", CLOCK_CHEATSHEET)

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
                    smu = hw["smu"]
                    resp, ret_val = smu.send_msg(msg_id, ((clk_id & 0xFFFF) << 16) | (v & 0xFFFF))
                    if resp == 0x01:
                        try:
                            verify = smu.get_max_freq(clk_id) if lt.endswith("Max") else smu.get_min_freq(clk_id)
                            if verify == v:
                                self._log(f"SMU: {cn} {lt} = {v} MHz  [OK, verified]")
                            else:
                                self._log(f"SMU: {cn} {lt} = {v} MHz  [OK, but readback={verify} MHz — PMFW clamped]")
                        except Exception:
                            self._log(f"SMU: {cn} {lt} = {v} MHz  [OK, readback query failed]")
                    elif resp == 0xFF:
                        self._log(f"SMU: {cn} {lt} = {v} MHz  [FAIL — PMFW rejected, param=0x{ret_val:08X}]")
                        return (False, f"{cn} {lt}: PMFW rejected (FAIL)")
                    elif resp == 0xFE:
                        self._log(f"SMU: {cn} {lt} = {v} MHz  [UNKNOWN_CMD — msg 0x{msg_id:02X} not supported]")
                        return (False, f"{cn} {lt}: unknown command")
                    else:
                        self._log(f"SMU: {cn} {lt} = {v} MHz  [resp=0x{resp:02X}]")
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
                spin = make_spinbox(0, max_mhz, 0, " MHz", "\u2014")
                input_row.addWidget(spin)

                _fn = _mk_freq_apply(_clk_id, _FREQ_MSG[lt], spin, clk_name, lt)
                set_btn = make_set_button(
                    f"{clk_name} {lt}", _fn, self._run_with_hardware,
                    max_width=40,
                )
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
        ppt_cv_label = make_current_value_label()
        ppt_lay.addWidget(QLabel("Current:"))
        ppt_lay.addWidget(ppt_cv_label)
        smu_ppt_spin = make_spinbox(0, 600, 0, " W", "\u2014")
        ppt_lay.addWidget(smu_ppt_spin)

        def _mk_ppt_apply(spin):
            def fn(hw):
                v = spin.value()
                if v > 0:
                    smu = hw["smu"]
                    resp, ret_val = smu.send_msg(PPSMC.SetPptLimit, v & 0xFFFFFFFF)
                    if resp == 0x01:
                        try:
                            verify = smu.get_ppt_limit()
                            if verify == v:
                                self._log(f"SMU: PPT Limit = {v} W  [OK, verified]")
                            else:
                                self._log(f"SMU: PPT Limit = {v} W  [OK, but readback={verify} W — PMFW clamped]")
                        except Exception:
                            self._log(f"SMU: PPT Limit = {v} W  [OK, readback query failed]")
                    elif resp == 0xFF:
                        self._log(f"SMU: PPT Limit = {v} W  [FAIL — PMFW rejected]")
                        return (False, f"PPT Limit: PMFW rejected")
                    else:
                        self._log(f"SMU: PPT Limit = {v} W  [resp=0x{resp:02X}]")
            return fn

        ppt_set_btn = make_set_button(
            "PPT Limit", _mk_ppt_apply(smu_ppt_spin), self._run_with_hardware,
        )
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
        ctrl_w = QWidget()
        ctrl_lay = QVBoxLayout(ctrl_w)
        _add_cheatsheet_btn(ctrl_lay, "Controls", CONTROLS_CHEATSHEET)
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

        smu_temp_input = make_spinbox(-1, 15, -1, "", "no change")
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
        # Sub-tab 4: Throttlers (per-bit mask control with quick actions)
        # ==================================================================
        _THROTTLER_CATEGORIES = {
            0: "Thermal",  1: "Thermal",  2: "Thermal",  3: "Thermal",
            4: "Thermal (MEM)",
            5: "VR Thermal",  6: "VR Thermal",
            7: "VR Thermal (MEM)",  8: "VR Thermal (MEM)",
            9: "Thermal",  10: "Thermal",  11: "Thermal",
            12: "Current",  13: "Current",
            14: "Power",  15: "Power",  16: "Power",  17: "Power",
            18: "Reliability",
            19: "Other",  20: "Other",
        }
        _CATEGORY_COLORS = {
            "Thermal":          "#e8a030",
            "Thermal (MEM)":    "#e8a030",
            "VR Thermal":       "#d08020",
            "VR Thermal (MEM)": "#d08020",
            "Current":          "#5090d0",
            "Power":            "#40a060",
            "Reliability":      "#e04040",
            "Other":            "#888888",
        }
        _MEM_BITS = {4, 7, 8}
        _FIT_BIT = 18

        throt_w = QWidget()
        throt_lay = QVBoxLayout(throt_w)
        _add_cheatsheet_btn(throt_lay, "Throttlers", THROTTLERS_CHEATSHEET)

        throt_tbl = QTableWidget()
        throt_tbl.setColumnCount(4)
        throt_tbl.setHorizontalHeaderLabels(["Bit", "Name", "Category", "Enable"])
        throt_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        throt_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        throt_tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        throt_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        throt_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        throt_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        throt_tbl.verticalHeader().setVisible(False)

        self._throttler_checkboxes: dict[int, QCheckBox] = {}

        for bit in range(THROTTLER_COUNT):
            row = throt_tbl.rowCount()
            throt_tbl.insertRow(row)

            bit_item = QTableWidgetItem(str(bit))
            bit_item.setFlags(bit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            throt_tbl.setItem(row, 0, bit_item)

            tname = THROTTLER_NAMES[bit] if bit < len(THROTTLER_NAMES) else f"BIT_{bit}"
            name_item = QTableWidgetItem(tname)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if bit in _MEM_BITS or bit == _FIT_BIT:
                name_item.setForeground(Qt.GlobalColor.yellow)
            throt_tbl.setItem(row, 1, name_item)

            cat = _THROTTLER_CATEGORIES.get(bit, "Other")
            cat_label = QLabel(cat)
            color = _CATEGORY_COLORS.get(cat, "#888888")
            cat_label.setStyleSheet(f"color: {color}; font-weight: bold; padding: 2px 6px;")
            throt_tbl.setCellWidget(row, 2, cat_label)

            cb = QCheckBox()
            cb.setChecked(True)
            cb.setToolTip(f"Bit {bit}: enable/disable {tname} throttler")
            throt_tbl.setCellWidget(row, 3, cb)
            self._throttler_checkboxes[bit] = cb

        throt_tbl.resizeRowsToContents()
        throt_lay.addWidget(throt_tbl)

        # --- Quick Actions ---
        actions_group = QGroupBox("Quick Actions")
        actions_lay = QHBoxLayout(actions_group)

        def _read_throttler_mask() -> int:
            mask = 0
            for i, cb in self._throttler_checkboxes.items():
                if cb.isChecked():
                    mask |= (1 << i)
            return mask

        def _set_checkboxes_from_mask(mask: int):
            for i, cb in self._throttler_checkboxes.items():
                cb.setChecked(bool(mask & (1 << i)))

        def _apply_mask_fn(hw):
            mask = _read_throttler_mask()
            hw["smu"].send_msg(PPSMC.SetThrottlerMask, mask)
            self._log(f"SMU: Throttler mask = 0x{mask:06X} ({bin(mask).count('1')}/21 enabled)")

        apply_btn = QPushButton("Apply Mask")
        apply_btn.setToolTip("Send the current checkbox state as SetThrottlerMask to SMU")
        apply_btn.clicked.connect(
            lambda: self._run_with_hardware("Set ThrottlerMask", _apply_mask_fn, require_scan=False))
        actions_lay.addWidget(apply_btn)

        def _disable_mem_fn(hw):
            for b in _MEM_BITS:
                self._throttler_checkboxes[b].setChecked(False)
            mask = _read_throttler_mask()
            hw["smu"].send_msg(PPSMC.SetThrottlerMask, mask)
            self._log(f"SMU: Disabled mem throttlers, mask = 0x{mask:06X}")

        dis_mem_btn = QPushButton("Disable Mem")
        dis_mem_btn.setToolTip("Uncheck bits 4, 7, 8 (Temp_Mem, VR_Mem0, VR_Mem1) and apply")
        dis_mem_btn.clicked.connect(
            lambda: self._run_with_hardware("Disable Mem Throttlers", _disable_mem_fn, require_scan=False))
        actions_lay.addWidget(dis_mem_btn)

        def _disable_fit_fn(hw):
            self._throttler_checkboxes[_FIT_BIT].setChecked(False)
            mask = _read_throttler_mask()
            hw["smu"].send_msg(PPSMC.SetThrottlerMask, mask)
            self._log(f"SMU: Disabled FIT throttler, mask = 0x{mask:06X}")

        dis_fit_btn = QPushButton("Disable FIT")
        dis_fit_btn.setToolTip("Uncheck bit 18 (FIT / reliability) and apply")
        dis_fit_btn.clicked.connect(
            lambda: self._run_with_hardware("Disable FIT Throttler", _disable_fit_fn, require_scan=False))
        actions_lay.addWidget(dis_fit_btn)

        def _disable_all_clicked():
            reply = QMessageBox.warning(
                self, "Disable All Throttlers",
                "This removes ALL firmware throttle protection.\n"
                "Hardware damage is possible if temperatures or currents exceed safe limits.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                _set_checkboxes_from_mask(0)
                self._run_with_hardware(
                    "Disable All Throttlers",
                    lambda hw: (hw["smu"].send_msg(PPSMC.SetThrottlerMask, 0),
                                self._log("SMU: ALL throttlers disabled (mask = 0x000000)")),
                    require_scan=False,
                )

        dis_all_btn = QPushButton("Disable All")
        dis_all_btn.setToolTip("Set mask to 0x000000 — removes all throttle protection (confirmation required)")
        dis_all_btn.setStyleSheet("color: #ff4444;")
        dis_all_btn.clicked.connect(_disable_all_clicked)
        actions_lay.addWidget(dis_all_btn)

        def _enable_all_fn(hw):
            full_mask = (1 << THROTTLER_COUNT) - 1
            _set_checkboxes_from_mask(full_mask)
            hw["smu"].send_msg(PPSMC.SetThrottlerMask, full_mask)
            self._log(f"SMU: ALL throttlers enabled (mask = 0x{full_mask:06X})")

        en_all_btn = QPushButton("Enable All")
        en_all_btn.setToolTip("Set mask to 0x1FFFFF — restore all throttle protection")
        en_all_btn.clicked.connect(
            lambda: self._run_with_hardware("Enable All Throttlers", _enable_all_fn, require_scan=False))
        actions_lay.addWidget(en_all_btn)

        actions_lay.addStretch()
        throt_lay.addWidget(actions_group)

        _add_refresh_btn(throt_lay)
        throt_scroll = QScrollArea()
        throt_scroll.setWidgetResizable(True)
        throt_scroll.setWidget(throt_w)
        self._smu_inner_tabs.addTab(throt_scroll, "Throttlers")

        # ==================================================================
        # Sub-tab 5: Features (per-bit toggle with individual Set)
        # ==================================================================
        feat_w = QWidget()
        feat_lay = QVBoxLayout(feat_w)
        _add_cheatsheet_btn(feat_lay, "Features", FEATURES_CHEATSHEET)

        # -- Features with dedicated SMU messages (bypass feature mask) --
        _DEDICATED_MSG_FEATURES = {
            SMU_FEATURE.GFXOFF:  ("AllowGfxOff", "DisallowGfxOff"),
            SMU_FEATURE.GFX_DCS: ("AllowGfxDcs", "DisallowGfxDcs"),
        }

        # -- "Unlock All Features" and "Probe Allowed Mask" buttons --
        unlock_lay = QHBoxLayout()
        unlock_btn = QPushButton("Unlock All Features")
        unlock_btn.setToolTip(
            "Send SetAllowedFeaturesMask(0xFFFFFFFF, 0xFFFFFFFF) to the SMU.\n"
            "This unlocks all 64 feature bits for enable/disable, matching\n"
            "the Linux driver's behaviour. Required before toggling features\n"
            "that the Windows driver's restrictive mask blocks."
        )
        unlock_btn.setStyleSheet(
            "QPushButton { background-color: #2c3e50; color: #f39c12; "
            "font-weight: bold; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #34495e; }"
        )
        def _unlock_all_features(hw):
            smu = hw["smu"]
            try:
                smu.set_allowed_features_mask(0xFFFFFFFF, 0xFFFFFFFF)
                self._log("SMU: Allowed features mask set to 0xFFFFFFFF:0xFFFFFFFF (all unlocked)")
            except RuntimeError as e:
                err = str(e)
                self._log(f"SMU: SetAllowedFeaturesMask rejected: {err}")
                if "PREREQ" in err.upper():
                    self._log(
                        "  The SMU firmware (or SCPM) controls the allowed-features mask on this GPU\n"
                        "  and won't let us override it.  Feature toggles that the Windows driver\n"
                        "  didn't include in the mask will continue to be silently ignored.\n"
                        "  This is a known firmware limitation on some RDNA4 boards."
                    )
                return (False, f"Unlock rejected: {err}")
        unlock_btn.clicked.connect(
            lambda: self._run_with_hardware(
                "Unlock All Features", _unlock_all_features, require_scan=False
            )
        )
        unlock_lay.addWidget(unlock_btn)

        probe_btn = QPushButton("Probe Allowed Mask")
        probe_btn.setToolTip(
            "Send EnableAllSmuFeatures to discover which bits the firmware\n"
            "actually permits. Compares before/after feature masks to map\n"
            "controllable vs firmware-locked bits. Attempts to restore the\n"
            "original state after probing."
        )
        probe_btn.setStyleSheet(
            "QPushButton { background-color: #1a3a2a; color: #2ecc71; "
            "font-weight: bold; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #245038; }"
        )
        def _probe_allowed_mask(hw):
            from src.engine.smu import SMU_RESP_OK
            smu = hw["smu"]
            self._log("SMU: Probing allowed mask — reading features before...")
            features_before = smu.get_running_features()
            self._log(f"  Before: 0x{features_before:016X}")

            self._log("  Sending EnableAllSmuFeatures (0x06)...")
            resp, retval = smu.enable_all_features()
            resp_ok = (resp == SMU_RESP_OK)
            self._log(f"  EnableAllSmuFeatures response: 0x{resp:02X} ({'OK' if resp_ok else 'FAILED'})")

            time.sleep(0.05)
            features_after = smu.get_running_features()
            self._log(f"  After:  0x{features_after:016X}")

            newly_enabled = features_after & ~features_before
            newly_count = bin(newly_enabled).count('1')
            self._log(f"  Newly enabled: {newly_count} bits  (0x{newly_enabled:016X})")

            locked_bits = ~features_after & 0x07FFFFFFFFFFFFFF
            allowed_count = bin(features_after & 0x07FFFFFFFFFFFFFF).count('1')
            locked_count = bin(locked_bits & 0x07FFFFFFFFFFFFFF).count('1')
            self._log(f"  Allowed mask map: {allowed_count} allowed, {locked_count} locked (of known bits)")

            if newly_enabled:
                self._log("  Restoring: disabling newly-enabled features...")
                lo = newly_enabled & 0xFFFFFFFF
                hi = (newly_enabled >> 32) & 0xFFFFFFFF
                try:
                    if lo:
                        smu.disable_features_low(lo)
                    if hi:
                        smu.disable_features_high(hi)
                    time.sleep(0.05)
                    features_restored = smu.get_running_features()
                    still_extra = features_restored & newly_enabled
                    if still_extra:
                        self._log(f"  Partial restore — {bin(still_extra).count('1')} bits could not be re-disabled")
                    else:
                        self._log("  Restore complete — all newly-enabled bits disabled")
                except Exception as e:
                    self._log(f"  Restore failed: {e}")

            result = {
                "features_before": features_before,
                "features_after": features_after,
                "allowed_mask": features_after,
                "newly_enabled": newly_enabled,
                "enable_all_ok": resp_ok,
                "dedicated_msg_bits": set(_DEDICATED_MSG_FEATURES.keys()),
            }
            self.allowed_mask_signal.emit(result)
            return result

        probe_btn.clicked.connect(
            lambda: self._run_with_hardware(
                "Probe Allowed Mask", _probe_allowed_mask, require_scan=False
            )
        )
        unlock_lay.addWidget(probe_btn)

        unlock_lay.addStretch()
        feat_lay.addLayout(unlock_lay)

        # -- Risk levels for color coding --
        _DANGEROUS_BITS = frozenset({
            SMU_FEATURE.FW_CTF, SMU_FEATURE.THROTTLERS,
            SMU_FEATURE.VR0HOT, SMU_FEATURE.FAN_CONTROL,
        })
        _CAUTION_BITS = frozenset({
            SMU_FEATURE.GFXOFF, SMU_FEATURE.BACO, SMU_FEATURE.GFX_ULV,
            SMU_FEATURE.DS_GFXCLK, SMU_FEATURE.DS_SOCCLK,
            SMU_FEATURE.DS_FCLK, SMU_FEATURE.DS_UCLK,
            SMU_FEATURE.DS_DCFCLK, SMU_FEATURE.DS_LCLK,
            SMU_FEATURE.GFX_EDC, SMU_FEATURE.SOC_EDC_XVMIN,
            SMU_FEATURE.FAN_ABNORMAL, SMU_FEATURE.FW_DSTATE,
            SMU_FEATURE.EDC_PWRBRK,
        })

        feat_tbl = QTableWidget()
        feat_tbl.setColumnCount(7)
        feat_tbl.setHorizontalHeaderLabels(["Bit", "Name", "State", "Toggle", "Set", "Result", "Control"])
        feat_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        feat_tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        feat_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        feat_tbl.verticalHeader().setVisible(False)

        self._smu_feature_state_labels = {}
        self._smu_feature_result_labels = {}
        self._smu_feature_control_labels = {}
        self._allowed_mask_probed = False

        def _mk_feature_apply(bit, cb):
            fname = _FEATURE_NAMES.get(bit, f"BIT_{bit}")
            def fn(hw):
                smu = hw["smu"]
                want_enabled = cb.isChecked()
                if bit < 32:
                    mask = 1 << bit
                    if want_enabled:
                        smu.enable_features_low(mask)
                    else:
                        smu.disable_features_low(mask)
                else:
                    mask = 1 << (bit - 32)
                    if want_enabled:
                        smu.enable_features_high(mask)
                    else:
                        smu.disable_features_high(mask)

                action = "Enable" if want_enabled else "Disable"
                self._log(f"SMU: {action} feature {fname} (bit {bit}) — verifying...")

                time.sleep(0.05)
                try:
                    features_after = smu.get_running_features()
                    actual = bool(features_after & (1 << bit))
                    if actual == want_enabled:
                        state_word = "enabled" if want_enabled else "disabled"
                        msg = f"SMU: {fname} (bit {bit}) verified {state_word}"
                        self._log(f"  ✓ {msg}")
                        self.feature_result_signal.emit(bit, True, actual, msg)
                    else:
                        actual_word = "enabled" if actual else "disabled"
                        msg = (f"SMU: {fname} (bit {bit}) toggle SILENTLY IGNORED — "
                               f"still {actual_word}  (mask=0x{features_after:016X})")
                        self._log(f"  ✗ {msg}")
                        self._log(f"    Hint: try 'Unlock All Features' first, then retry.")
                        self.feature_result_signal.emit(bit, False, actual, msg)
                        return (False, f"{fname} toggle silently ignored by SMU")
                except Exception as exc:
                    msg = f"SMU: {fname} readback failed: {exc}"
                    self._log(f"  ? {msg}")
                    self.feature_result_signal.emit(bit, False, want_enabled, msg)
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
            if bit in _DANGEROUS_BITS:
                name_item.setForeground(Qt.GlobalColor.red)
                name_item.setToolTip(f"DANGEROUS — {fname}: disabling may cause hardware damage or instability")
            elif bit in _CAUTION_BITS:
                name_item.setForeground(Qt.GlobalColor.darkYellow)
                name_item.setToolTip(f"CAUTION — {fname}: may affect power saving or thermal management")
            else:
                name_item.setToolTip(fname)
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
            elif bit in _DANGEROUS_BITS:
                cb.setToolTip(f"⚠ DANGEROUS — Bit {bit}: enable/disable {fname}")
            elif bit in _CAUTION_BITS:
                cb.setToolTip(f"⚠ Caution — Bit {bit}: enable/disable {fname}")
            else:
                cb.setToolTip(f"Bit {bit}: enable/disable {fname}")
            feat_tbl.setCellWidget(row, 3, cb)
            self._detailed_param_widgets[f"SMU_FEAT_{bit}"] = cb

            _fn = _mk_feature_apply(bit, cb)
            set_btn = make_set_button(fname, _fn, self._run_with_hardware)
            feat_tbl.setCellWidget(row, 4, set_btn)

            result_label = QLabel("")
            result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            feat_tbl.setCellWidget(row, 5, result_label)
            self._smu_feature_result_labels[bit] = result_label

            ctrl_label = QLabel("—")
            ctrl_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if bit in _DEDICATED_MSG_FEATURES:
                enable_name, disable_name = _DEDICATED_MSG_FEATURES[bit]
                ctrl_label.setToolTip(
                    f"Dedicated messages: {enable_name} / {disable_name}\n"
                    f"These bypass the feature mask — use Controls tab to send them."
                )
            feat_tbl.setCellWidget(row, 6, ctrl_label)
            self._smu_feature_control_labels[bit] = ctrl_label

        feat_tbl.resizeRowsToContents()
        feat_lay.addWidget(feat_tbl)
        _add_refresh_btn(feat_lay)
        feat_scroll = QScrollArea()
        feat_scroll.setWidgetResizable(True)
        feat_scroll.setWidget(feat_w)
        self._smu_inner_tabs.addTab(feat_scroll, "Features")

        # ==================================================================
        # Sub-tab 6: Metrics (live SmuMetrics_t)
        # ==================================================================
        metrics_w = QWidget()
        metrics_lay = QVBoxLayout(metrics_w)
        _add_cheatsheet_btn(metrics_lay, "Metrics", METRICS_CHEATSHEET)

        metrics_header = QLabel("Live Metrics (SmuMetrics_t)")
        metrics_header.setStyleSheet("font-weight: bold; font-size: 10pt;")
        metrics_lay.addWidget(metrics_header)

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

        self._smu_metrics_interval_spin = make_spinbox(1, 30, 2, " s")
        self._smu_metrics_interval_spin.setToolTip("Auto-refresh interval in seconds")
        self._smu_metrics_interval_spin.valueChanged.connect(
            self._on_smu_metrics_interval_changed)
        metrics_ctrl_row.addWidget(self._smu_metrics_interval_spin)

        self._smu_metrics_status_label = QLabel("")
        self._smu_metrics_status_label.setStyleSheet("color: #888;")
        metrics_ctrl_row.addWidget(self._smu_metrics_status_label)

        metrics_ctrl_row.addStretch()
        metrics_lay.addLayout(metrics_ctrl_row)

        self._smu_metrics_table = QTableWidget()
        self._smu_metrics_table.setColumnCount(2)
        self._smu_metrics_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self._smu_metrics_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._smu_metrics_table.horizontalHeader().setStretchLastSection(True)
        self._smu_metrics_table.verticalHeader().setVisible(False)
        self._smu_metrics_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        metrics_lay.addWidget(self._smu_metrics_table)

        self._metrics_value_items: dict[str, QTableWidgetItem] = {}
        self._metrics_worker = None

        self._metrics_auto_timer = QTimer(self)
        self._metrics_auto_timer.timeout.connect(self._on_smu_metrics_timer_tick)

        self._init_metrics_table_rows()

        metrics_scroll = QScrollArea()
        metrics_scroll.setWidgetResizable(True)
        metrics_scroll.setWidget(metrics_w)
        self._smu_inner_tabs.addTab(metrics_scroll, "Metrics")

        # ==================================================================
        # Sub-tab 7: Tables (raw SMU table dumps + PFE settings)
        # ==================================================================
        tables_w = QWidget()
        tables_lay = QVBoxLayout(tables_w)
        _add_cheatsheet_btn(tables_lay, "Tables", TABLES_CHEATSHEET)

        other_header = QLabel("Other SMU Tables (on demand)")
        other_header.setStyleSheet(
            "font-weight: bold; font-size: 10pt;")
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
            "TABLE_ECCINFO (id=11) — ECC error-correction counters")
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

        # --- PFE Settings section (PPTable header patching) ---
        pfe_header = QLabel("PFE Settings (PPTable Header — FeaturesToRun / DebugOverrides)")
        pfe_header.setStyleSheet(
            "font-weight: bold; font-size: 10pt; margin-top: 12px;")
        tables_lay.addWidget(pfe_header)

        pfe_btn_row = QHBoxLayout()

        self._pfe_read_btn = QPushButton("Read PFE Settings")
        self._pfe_read_btn.setToolTip(
            "Read PFE_Settings_t from TABLE_PPTABLE: FeaturesToRun, "
            "FwDStateMask, DebugOverrides")
        self._pfe_read_btn.clicked.connect(self._on_pfe_read)
        pfe_btn_row.addWidget(self._pfe_read_btn)

        self._pfe_patch_features_btn = QPushButton("Patch FeaturesToRun")
        self._pfe_patch_features_btn.setToolTip(
            "Add GFX_EDC(41), CLOCK_POWER_DOWN_BYPASS(43), EDC_PWRBRK(49) "
            "to FeaturesToRun and write back via TransferTableDram2Smu")
        self._pfe_patch_features_btn.clicked.connect(self._on_pfe_patch_features)
        pfe_btn_row.addWidget(self._pfe_patch_features_btn)

        self._pfe_patch_debug_btn = QPushButton("Patch DebugOverrides")
        self._pfe_patch_debug_btn.setToolTip(
            "Set DISABLE_FMAX_VMAX (0x40) + ENABLE_PROFILING_MODE (0x1000) "
            "in DebugOverrides and write back")
        self._pfe_patch_debug_btn.clicked.connect(self._on_pfe_patch_debug)
        pfe_btn_row.addWidget(self._pfe_patch_debug_btn)

        self._pfe_check_caps_btn = QPushButton("Check OD Memory Caps")
        self._pfe_check_caps_btn.setToolTip(
            "Check ODCAP bits 4 (AUTO_OC_MEMORY), 5 (MEMORY_TIMING_TUNE), "
            "6 (MANUAL_AC_TIMING) and UCLK OD support")
        self._pfe_check_caps_btn.clicked.connect(self._on_pfe_check_caps)
        pfe_btn_row.addWidget(self._pfe_check_caps_btn)

        pfe_btn_row.addStretch()
        tables_lay.addLayout(pfe_btn_row)

        # Tools DRAM path buttons (Phase 6: write via msg 0x53)
        tools_lbl = QLabel("Tools DRAM Path (msg 0x53 — bypasses Driver path rejection)")
        tools_lbl.setStyleSheet("font-size: 8pt; color: #f93; margin-top: 6px;")
        tables_lay.addWidget(tools_lbl)

        pfe_tools_row = QHBoxLayout()

        self._pfe_patch_features_tools_btn = QPushButton(
            "Patch FeaturesToRun (Tools Path)")
        self._pfe_patch_features_tools_btn.setToolTip(
            "Same as 'Patch FeaturesToRun' but writes via "
            "TransferTableDram2SmuWithAddr (0x53) instead of 0x13.\n"
            "Falls back to TABLE_CUSTOM_SKUTABLE (id=12) if TABLE_PPTABLE fails.")
        self._pfe_patch_features_tools_btn.clicked.connect(
            self._on_pfe_patch_features_tools)
        pfe_tools_row.addWidget(self._pfe_patch_features_tools_btn)

        self._pfe_patch_debug_tools_btn = QPushButton(
            "Patch DebugOverrides (Tools Path)")
        self._pfe_patch_debug_tools_btn.setToolTip(
            "Same as 'Patch DebugOverrides' but writes via "
            "TransferTableDram2SmuWithAddr (0x53).\n"
            "Falls back to TABLE_CUSTOM_SKUTABLE (id=12) if TABLE_PPTABLE fails.")
        self._pfe_patch_debug_tools_btn.clicked.connect(
            self._on_pfe_patch_debug_tools)
        pfe_tools_row.addWidget(self._pfe_patch_debug_tools_btn)

        pfe_tools_row.addStretch()
        tables_lay.addLayout(pfe_tools_row)

        self._pfe_result_view = QPlainTextEdit()
        self._pfe_result_view.setReadOnly(True)
        self._pfe_result_view.setStyleSheet(
            "background: #1a1a2a; color: #cdf; padding: 6px; "
            "font-family: Consolas, monospace; font-size: 8pt;")
        self._pfe_result_view.setMaximumHeight(320)
        self._pfe_result_view.setPlaceholderText(
            "Click a button above to read/patch PFE settings...")
        tables_lay.addWidget(self._pfe_result_view)

        self._pfe_worker = None

        tables_scroll = QScrollArea()
        tables_scroll.setWidgetResizable(True)
        tables_scroll.setWidget(tables_w)
        self._smu_inner_tabs.addTab(tables_scroll, "Tables")
        self._smu_inner_tabs.addTab(self._od_scroll, "OD")

    # ------------------------------------------------------------------
    # Metrics sub-tab: helpers
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

    # ------------------------------------------------------------------
    # PFE Settings handlers
    # ------------------------------------------------------------------

    def _pfe_set_buttons_enabled(self, enabled: bool):
        self._pfe_read_btn.setEnabled(enabled)
        self._pfe_patch_features_btn.setEnabled(enabled)
        self._pfe_patch_debug_btn.setEnabled(enabled)
        self._pfe_check_caps_btn.setEnabled(enabled)
        self._pfe_patch_features_tools_btn.setEnabled(enabled)
        self._pfe_patch_debug_tools_btn.setEnabled(enabled)

    def _on_pfe_read(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_result_view.setPlainText("Reading PFE_Settings_t from TABLE_PPTABLE...")
        self._log("PFE: reading PFE_Settings_t...")
        self._pfe_worker = PfeWorker("read_pfe")
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_features(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_result_view.setPlainText(
            "Patching FeaturesToRun: adding GFX_EDC(41), "
            "CLOCK_POWER_DOWN_BYPASS(43), EDC_PWRBRK(49)...")
        self._log("PFE: patching FeaturesToRun [41, 43, 49]...")
        self._pfe_worker = PfeWorker(
            "patch_features",
            extra_bits=[
                SMU_FEATURE.GFX_EDC,
                SMU_FEATURE.CLOCK_POWER_DOWN_BYPASS,
                SMU_FEATURE.EDC_PWRBRK,
            ],
        )
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_debug(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        from src.engine.overclock_engine import (
            DEBUG_OVERRIDE_DISABLE_FMAX_VMAX,
            DEBUG_OVERRIDE_ENABLE_PROFILING_MODE,
        )
        flags = DEBUG_OVERRIDE_DISABLE_FMAX_VMAX | DEBUG_OVERRIDE_ENABLE_PROFILING_MODE
        self._pfe_result_view.setPlainText(
            f"Patching DebugOverrides: setting flags 0x{flags:08X}\n"
            "  DISABLE_FMAX_VMAX (0x40) + ENABLE_PROFILING_MODE (0x1000)...")
        self._log(f"PFE: patching DebugOverrides flags=0x{flags:08X}...")
        self._pfe_worker = PfeWorker("patch_debug", flags=flags)
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_check_caps(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_result_view.setPlainText(
            "Checking OD memory timing capabilities...")
        self._log("PFE: checking OD memory timing caps...")
        self._pfe_worker = PfeWorker("check_od_caps")
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_features_tools(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_result_view.setPlainText(
            "Patching FeaturesToRun via Tools DRAM path (msg 0x53):\n"
            "  Adding GFX_EDC(41), CLOCK_POWER_DOWN_BYPASS(43), EDC_PWRBRK(49)\n"
            "  Write via TransferTableDram2SmuWithAddr instead of Dram2Smu...")
        self._log("PFE: patching FeaturesToRun via Tools path [41, 43, 49]...")
        self._pfe_worker = PfeWorker(
            "patch_features_tools",
            extra_bits=[
                SMU_FEATURE.GFX_EDC,
                SMU_FEATURE.CLOCK_POWER_DOWN_BYPASS,
                SMU_FEATURE.EDC_PWRBRK,
            ],
        )
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_debug_tools(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            self._log("PFE operation already in progress")
            return
        self._pfe_set_buttons_enabled(False)
        from src.engine.overclock_engine import (
            DEBUG_OVERRIDE_DISABLE_FMAX_VMAX,
            DEBUG_OVERRIDE_ENABLE_PROFILING_MODE,
        )
        flags = DEBUG_OVERRIDE_DISABLE_FMAX_VMAX | DEBUG_OVERRIDE_ENABLE_PROFILING_MODE
        self._pfe_result_view.setPlainText(
            f"Patching DebugOverrides via Tools DRAM path (msg 0x53):\n"
            f"  Setting flags 0x{flags:08X}\n"
            "  DISABLE_FMAX_VMAX (0x40) + ENABLE_PROFILING_MODE (0x1000)\n"
            "  Write via TransferTableDram2SmuWithAddr instead of Dram2Smu...")
        self._log(f"PFE: patching DebugOverrides via Tools path flags=0x{flags:08X}...")
        self._pfe_worker = PfeWorker("patch_debug_tools", flags=flags)
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_result(self, action: str, result: dict):
        if isinstance(result, dict) and "error" in result:
            self._pfe_result_view.setPlainText(
                f"PFE [{action}]: Error — {result['error']}")
            self._log(f"PFE [{action}]: {result['error']}")
            return

        lines = [f"PFE [{action}] Result", "=" * 60]

        if action == "read_pfe":
            lines.append(f"  Version:          {result.get('version', '?')}")
            feat64 = result.get('features_to_run_64', 0)
            lo = result.get('features_to_run_lo', 0)
            hi = result.get('features_to_run_hi', 0)
            lines.append(f"  FeaturesToRun:    0x{feat64:016X}")
            lines.append(f"    [0] (bits 0-31):  0x{lo:08X}")
            lines.append(f"    [1] (bits 32-63): 0x{hi:08X}")
            lines.append(f"  FwDStateMask:     0x{result.get('fw_dstate_mask', 0):08X}")
            dbg = result.get('debug_overrides', 0)
            lines.append(f"  DebugOverrides:   0x{dbg:08X}")
            decoded = result.get('debug_overrides_decoded', [])
            if decoded:
                for flag, name in decoded:
                    lines.append(f"    0x{flag:08X}  {name}")
            else:
                lines.append("    (none set)")
            lines.append("")
            lines.append("  FeaturesToRun bit detail:")
            for bit in range(64):
                if feat64 & (1 << bit):
                    name = _FEATURE_NAMES.get(bit, f"BIT_{bit}")
                    lines.append(f"    [{bit:2d}] {name}")

        elif action == "patch_features":
            from src.engine.smu import SMU_RESP_OK
            resp = result.get('smu_resp', -1)
            resp_str = "OK" if resp == SMU_RESP_OK else f"0x{resp:02X}"
            lines.append(f"  SMU response: {resp_str}")
            lines.append(
                f"  FeaturesToRun before: lo=0x{result.get('old_lo', 0):08X} "
                f"hi=0x{result.get('old_hi', 0):08X}")
            lines.append(
                f"  FeaturesToRun after:  lo=0x{result.get('new_lo', 0):08X} "
                f"hi=0x{result.get('new_hi', 0):08X}")
            lines.append("")
            lines.append("  Per-bit verification:")
            for bit, name, was_on, now_on in result.get('bits_detail', []):
                status = "ON" if now_on else "OFF"
                change = ""
                if not was_on and now_on:
                    change = " <-- NEWLY ENABLED"
                elif not was_on and not now_on:
                    change = " (PMFW did not enable)"
                lines.append(f"    [{bit:2d}] {name:30s} {status}{change}")
            newly = result.get('newly_enabled', 0)
            if newly:
                lines.append(f"\n  Newly enabled features mask: 0x{newly:016X}")
            else:
                lines.append(
                    "\n  No new features became running — PMFW may ignore "
                    "FeaturesToRun changes at runtime.")

        elif action == "patch_debug":
            from src.engine.smu import SMU_RESP_OK
            resp = result.get('smu_resp', -1)
            resp_str = "OK" if resp == SMU_RESP_OK else f"0x{resp:02X}"
            lines.append(f"  SMU response: {resp_str}")
            lines.append(
                f"  DebugOverrides before: 0x{result.get('old_debug_overrides', 0):08X}")
            lines.append(
                f"  DebugOverrides after:  0x{result.get('new_debug_overrides', 0):08X}")
            lines.append(f"  Flags applied:         0x{result.get('flags_applied', 0):08X}")
            lines.append("")
            for flag, name, was_set, now_set in result.get('flags_detail', []):
                now_str = "SET" if now_set else "NOT SET"
                change = ""
                if not was_set and now_set:
                    change = " <-- NEWLY SET"
                elif not was_set and now_set is None:
                    change = " (verification read failed)"
                elif was_set:
                    change = " (was already set)"
                lines.append(f"    0x{flag:08X}  {name:40s} {now_str}{change}")
            after = result.get('after_pfe')
            if after:
                lines.append(
                    f"\n  Verified DebugOverrides: "
                    f"0x{after.get('debug_overrides', 0):08X}")

        elif action in ("patch_features_tools", "patch_debug_tools"):
            wr = result.get('write_result', {})
            mc = wr.get('mc_addr', 0)
            vs = wr.get('vram_start', 0)
            success = wr.get('success', False)

            lines.append(f"  Tools DRAM Path Write")
            lines.append(f"  MC address:    0x{mc:016X}")
            lines.append(f"  vram_start:    0x{vs:016X}")
            lines.append("")
            for att in wr.get('attempts', []):
                from src.engine.smu import SMU_RESP_OK as _OK
                r = att.get('resp', -1)
                r_str = "OK" if r == _OK else f"0x{r:02X}"
                lines.append(
                    f"  {att.get('label', '?'):26s} -> resp={r_str} "
                    f"ret=0x{att.get('ret', 0):08X}")
            lines.append("")
            lines.append(
                f"  Result: {'SUCCESS' if success else 'FAILED — PMFW rejected all table IDs'}")

            if action == "patch_features_tools":
                lines.append("")
                lines.append(
                    f"  FeaturesToRun before: lo=0x{result.get('old_lo', 0):08X} "
                    f"hi=0x{result.get('old_hi', 0):08X}")
                lines.append(
                    f"  FeaturesToRun after:  lo=0x{result.get('new_lo', 0):08X} "
                    f"hi=0x{result.get('new_hi', 0):08X}")
                lines.append("")
                lines.append("  Per-bit verification:")
                for bit, name, was_on, now_on in result.get('bits_detail', []):
                    status = "ON" if now_on else "OFF"
                    change = ""
                    if not was_on and now_on:
                        change = " <-- NEWLY ENABLED"
                    elif not was_on and not now_on:
                        change = " (PMFW did not enable)"
                    lines.append(
                        f"    [{bit:2d}] {name:30s} {status}{change}")
                newly = result.get('newly_enabled', 0)
                if newly:
                    lines.append(
                        f"\n  Newly enabled features mask: 0x{newly:016X}")
                else:
                    lines.append(
                        "\n  No new features became running — PMFW may ignore "
                        "FeaturesToRun changes at runtime.")

            else:  # patch_debug_tools
                lines.append("")
                lines.append(
                    f"  DebugOverrides before: "
                    f"0x{result.get('old_debug_overrides', 0):08X}")
                lines.append(
                    f"  DebugOverrides after:  "
                    f"0x{result.get('new_debug_overrides', 0):08X}")
                lines.append(
                    f"  Flags applied:         "
                    f"0x{result.get('flags_applied', 0):08X}")
                lines.append("")
                for flag, name, was_set, now_set in result.get('flags_detail', []):
                    now_str = "SET" if now_set else "NOT SET"
                    change = ""
                    if not was_set and now_set:
                        change = " <-- NEWLY SET"
                    elif not was_set and now_set is None:
                        change = " (verification read failed)"
                    elif was_set:
                        change = " (was already set)"
                    lines.append(
                        f"    0x{flag:08X}  {name:40s} {now_str}{change}")
                after = result.get('after_pfe')
                if after:
                    lines.append(
                        f"\n  Verified DebugOverrides: "
                        f"0x{after.get('debug_overrides', 0):08X}")

        elif action == "check_od_caps":
            uclk = result.get('uclk', {})
            if 'dpm_min' in uclk:
                lines.append(
                    f"  UCLK DPM range: {uclk['dpm_min']} - {uclk['dpm_max']} MHz")
            if 'od_UclkFmin' in uclk:
                lines.append(
                    f"  OD table UCLK:  {uclk['od_UclkFmin']} - "
                    f"{uclk['od_UclkFmax']} MHz")
                lines.append(
                    f"  OD table FCLK:  {uclk['od_FclkFmin']} - "
                    f"{uclk['od_FclkFmax']} MHz")
            od_feat = result.get('od_features', {})
            if 'FeatureCtrlMask' in od_feat:
                mask = od_feat['FeatureCtrlMask']
                lines.append(
                    f"\n  OD FeatureCtrlMask: 0x{mask:08X}")
                lines.append(
                    f"    UCLK OD:    {'YES' if od_feat.get('UCLK_bit') else 'NO'}")
                lines.append(
                    f"    FCLK OD:    {'YES' if od_feat.get('FCLK_bit') else 'NO'}")
            caps = result.get('caps', {})
            if 'raw_at_0x105C' in caps:
                lines.append(
                    f"\n  PPTable @0x105C (BasicMin FeatureCtrlMask): "
                    f"{caps['raw_at_0x105C']}")
                lines.append(
                    f"    UCLK bit in PPTable: "
                    f"{'YES' if caps.get('UCLK_bit_in_pptable') else 'NO'}")
                lines.append(
                    f"    FCLK bit in PPTable: "
                    f"{'YES' if caps.get('FCLK_bit_in_pptable') else 'NO'}")
            lines.append("")
            if od_feat.get('UCLK_bit'):
                lines.append(
                    "  UCLK OD is SUPPORTED — memory clock can be adjusted via OD table.")
            else:
                lines.append(
                    "  UCLK OD is NOT supported in the current OD FeatureCtrlMask.")
            lines.append(
                "\n  Note: ODCAP bits (AUTO_OC_MEMORY, MEMORY_TIMING_TUNE, "
                "MANUAL_AC_TIMING) are\n  exposed via D3DKMTEscape CN escape "
                "headers, not directly in the DMA PPTable.\n  Use the Escape OD "
                "tab to query those capabilities from the Windows driver.")

        self._pfe_result_view.setPlainText("\n".join(lines))
        self._log(f"PFE [{action}]: completed")

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

        _, hint_row = make_cheatsheet_button(
            self, "Registry Patch", REG_CHEATSHEET_HTML,
            self._show_smu_cheatsheet, label="",
        )
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
            spin = make_spinbox(min_val, max_val, current if current is not None else 0)
            spin.setToolTip(f"Value to apply ({min_val}\u2013{max_val})")
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

    # ===================================================================
    # Memory tab
    # ===================================================================

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
        self._detailed_worker.log_signal.connect(self._log_gui, Qt.ConnectionType.QueuedConnection)
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
        """Return OverclockSettings from Simple tab (clock only, no offset)."""
        return OverclockSettings(
            clock=self.clock_spin.value(),
            offset=0,
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
            game_clock=_val("smc_pptable/SkuTable/DriverReportedClocks/GameClockAc", self.vbios_values.gameclock_ac),
            boost_clock=_val("smc_pptable/SkuTable/DriverReportedClocks/BoostClockAc", self.vbios_values.boostclock_ac),
            power_ac=_val("smc_pptable/SkuTable/MsgLimits/Power/0/0", self.vbios_values.power_ac),
            power_dc=_val("smc_pptable/SkuTable/MsgLimits/Power/0/1", self.vbios_values.power_dc),
            tdc_gfx=_val("smc_pptable/SkuTable/MsgLimits/Tdc/0", self.vbios_values.tdc_gfx),
            tdc_soc=_val("smc_pptable/SkuTable/MsgLimits/Tdc/1", self.vbios_values.tdc_soc),
            temp_edge=_val("smc_pptable/SkuTable/MsgLimits/Temperature/0", 100),
            temp_hotspot=_val("smc_pptable/SkuTable/MsgLimits/Temperature/1", 110),
            temp_mem=_val("smc_pptable/SkuTable/MsgLimits/Temperature/4", 100),
            temp_vr_gfx=_val("smc_pptable/SkuTable/MsgLimits/Temperature/6", 115),
            temp_vr_soc=_val("smc_pptable/SkuTable/MsgLimits/Temperature/7", 115),
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

    def _on_save_clock_default(self):
        mhz = self.clock_spin.value()
        settings.set("defaults.simple_clock_mhz", mhz)
        self._log(f"Saved default clock: {mhz} MHz")

    def _on_run_on_startup_toggled(self, checked: bool):
        if checked:
            ok = enable_startup()
            if not ok:
                self.run_on_startup_cb.blockSignals(True)
                self.run_on_startup_cb.setChecked(False)
                self.run_on_startup_cb.blockSignals(False)
                self._log("Failed to create Windows startup task")
                return
            self._log("Added to Windows startup (Task Scheduler)")
        else:
            disable_startup()
            self._log("Removed from Windows startup")

    def _on_scan_on_startup_toggled(self, checked: bool):
        settings.set("defaults.scan_on_startup", checked)
        self.apply_after_scan_cb.setEnabled(checked)
        if not checked:
            self.apply_after_scan_cb.setChecked(False)

    def _on_apply_simple(self):
        """Apply Simple tab: patch PPTable clocks in RAM, then DisallowGfxOff + workload cycle."""
        settings = self.get_simple_settings()
        self._log(f"Simple Apply: clock={settings.clock} MHz")

        def do_apply(hw):
            vb = _get_vbios_values()
            if vb is None:
                vb = self.vbios_values
            inpout, smu = hw["inpout"], hw["smu"]
            if self.scan_result and self.scan_result.valid_addrs:
                results = apply_clocks_only(
                    inpout, smu, self.scan_result, settings,
                    vbios_values=vb,
                    progress_callback=lambda pct, msg: self._log(msg),
                )
                self._log(f"Clocks: {results['patched_count']} patched, "
                          f"{results['skipped_count']} skipped.")
                if hw.get("virt") is None:
                    self._log("Note: DMA buffer not available — OD/metrics "
                              "readback skipped. Run DRAM Scan to enable.")

        self._run_with_hardware("Simple Apply", do_apply)

    def _on_apply_pp(self):
        """Apply PP section: patch all PP fields via RAM (no SMU commands)."""
        pp_values = self.get_detailed_pp_patch_values()
        self._log(f"Apply PP: {len(pp_values)} field(s) to patch (RAM-only)")

        def do_apply(hw):
            log_cb = lambda pct, msg: self._log(msg)
            if self.scan_result and self.scan_result.valid_addrs:
                res = _apply_pp_field_groups(
                    hw["inpout"], self.scan_result, pp_values,
                    self._pp_ram_offset_map, groups=None,
                    progress_callback=log_cb,
                )
                self._log(f"PP: {res['field_writes']} field writes across "
                          f"{res['patched_count']} addrs "
                          f"({res['skipped_count']} skipped)")
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
            if hw.get("virt") is None:
                return (False, "DMA buffer not available — run DRAM Scan first to enable OD writes")
            apply_od_table_only(hw["smu"], hw["virt"], settings)
            self._log("OD table applied.")

        self._run_with_hardware("OD Apply", do_apply)

    # ==================================================================
    # Escape OD tab — D3DKMTEscape OD8 write interface (no admin)
    # ==================================================================

    _ESCAPE_OD_GROUPS = [
        ("Core OC / Power", [
            Od8Setting.GFXCLK_FOFFSET,
            Od8Setting.GFXCLK_FMAX,
            Od8Setting.PPT,
            Od8Setting.TDC,
        ]),
        ("Memory / Fabric Clocks", [
            Od8Setting.UCLK_FMIN,
            Od8Setting.UCLK_FMAX,
            Od8Setting.FCLK_FMIN,
            Od8Setting.FCLK_FMAX,
        ]),
        ("GFX V/F Curve", [
            Od8Setting.GFX_CURVE_VF_0,
            Od8Setting.GFX_CURVE_VF_1,
            Od8Setting.GFX_CURVE_VF_2,
            Od8Setting.GFX_CURVE_VF_3,
            Od8Setting.GFX_CURVE_VF_4,
            Od8Setting.VF_CURVE_VOLTAGE_OFFSET,
        ]),
        ("Fan Controls", [
            Od8Setting.FAN_MODE,
            Od8Setting.FAN_ZERO_RPM_ENABLE,
            Od8Setting.FAN_ZERO_RPM_STOP_TEMP,
            Od8Setting.OPERATING_TEMP_MAX,
            Od8Setting.FAN_MINIMUM_PWM,
            Od8Setting.FAN_ACOUSTIC_TARGET,
            Od8Setting.FAN_ACOUSTIC_LIMIT,
            Od8Setting.FAN_TARGET_TEMPERATURE,
        ]),
        ("Fan Curve Points", [
            Od8Setting.FAN_CURVE_PWM_0, Od8Setting.FAN_CURVE_TEMP_0,
            Od8Setting.FAN_CURVE_PWM_1, Od8Setting.FAN_CURVE_TEMP_1,
            Od8Setting.FAN_CURVE_PWM_2, Od8Setting.FAN_CURVE_TEMP_2,
            Od8Setting.FAN_CURVE_PWM_3, Od8Setting.FAN_CURVE_TEMP_3,
            Od8Setting.FAN_CURVE_PWM_4, Od8Setting.FAN_CURVE_TEMP_4,
            Od8Setting.FAN_CURVE_PWM_5, Od8Setting.FAN_CURVE_TEMP_5,
        ]),
        ("Voltage Limits", [
            Od8Setting.VDDGFX_VMAX,
            Od8Setting.VDDSOC_VMAX,
        ]),
        ("Advanced / Full Control", [
            Od8Setting.ADVANCED_OD_MODE,
            Od8Setting.FULL_CTRL_GFX_VOLTAGE,
            Od8Setting.FULL_CTRL_SOC_VOLTAGE,
            Od8Setting.FULL_CTRL_GFXCLK,
            Od8Setting.FULL_CTRL_UCLK,
            Od8Setting.FULL_CTRL_FCLK,
        ]),
        ("EDC / PCC", [
            Od8Setting.GFX_EDC,
            Od8Setting.GFX_PCC_LIMIT,
        ]),
        ("RDNA4 Extensions", [
            Od8Setting.AC_TIMING,
            Od8Setting.RDNA4_EXT_48,
            Od8Setting.RDNA4_EXT_67,
            Od8Setting.RDNA4_EXT_68,
            Od8Setting.RDNA4_EXT_69,
        ]),
        ("Driver Internal", [
            Od8Setting.RESET_FLAG,
        ]),
    ]

    def _escape_spin_params(self, idx):
        """Return (min, max, default, suffix) for an OD8 index spinbox."""
        _OVERRIDES = {
            Od8Setting.FAN_ZERO_RPM_ENABLE: (0, 1, 0, ""),
            Od8Setting.FAN_MODE: (0, 1, 0, ""),
            Od8Setting.ADVANCED_OD_MODE: (0, 1, 0, ""),
            Od8Setting.RESET_FLAG: (0, 1, 0, ""),
            Od8Setting.GFXCLK_FOFFSET: (-500, 500, 0, " MHz"),
            Od8Setting.GFXCLK_FMAX: (0, 5000, 0, " MHz"),
            Od8Setting.PPT: (-100, 200, 0, "%"),
            Od8Setting.TDC: (-100, 200, 0, ""),
        }
        if idx in _OVERRIDES:
            return _OVERRIDES[idx]
        mapping = OD8_RDNA4_FIELD_MAP.get(idx)
        unit = mapping.unit if mapping else ""
        if unit == "mV":
            return (-500, 2000, 0, " mV")
        if unit == "MHz":
            return (0, 5000, 0, " MHz")
        if unit == "%":
            return (0, 255, 0, "%")
        if unit in ("C", "\u00b0C"):
            return (0, 127, 0, " \u00b0C")
        if unit == "RPM":
            return (0, 65535, 0, " RPM")
        return (-32768, 65535, 0, "")

    def _setup_escape_tab(self):
        """Build the Escape OD tab — D3DKMTEscape OD8 write interface."""
        outer_layout = QVBoxLayout(self.escape_tab)

        _, hint_row = make_cheatsheet_button(
            self, "Escape OD", ESCAPE_OD_HELP_HTML, self._show_smu_cheatsheet,
            label="Escape OD \u2014 D3DKMTEscape OD8 (no admin)",
        )
        outer_layout.addLayout(hint_row)

        info = QLabel(
            "Send OD8 settings via D3DKMTEscape \u2014 the same WDDM path Adrenalin uses. "
            "No admin privileges required.  Read first, then Set individual indices or Apply All Modified."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; font-size: 9pt; padding: 4px;")
        outer_layout.addWidget(info)

        top_row = QHBoxLayout()
        self._escape_read_btn = QPushButton("Read Current Values")
        self._escape_read_btn.setToolTip("Read all 73 OD8 values from the driver via a no-op escape write")
        self._escape_read_btn.clicked.connect(self._on_escape_read)
        top_row.addWidget(self._escape_read_btn)
        self._escape_status_label = QLabel("Not connected")
        self._escape_status_label.setStyleSheet("color: #888;")
        top_row.addWidget(self._escape_status_label)
        top_row.addStretch()
        outer_layout.addLayout(top_row)

        tbl = QTableWidget()
        tbl.setColumnCount(7)
        tbl.setHorizontalHeaderLabels([
            "Index", "Setting Name", "Conf.", "Unit",
            "Current Value", "Input", "Set",
        ])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.verticalHeader().setVisible(False)
        self._escape_table = tbl

        self._escape_od_widgets = {}
        self._escape_od_current_values = {}

        for group_name, indices in self._ESCAPE_OD_GROUPS:
            row = tbl.rowCount()
            tbl.insertRow(row)
            hdr = QTableWidgetItem(group_name)
            hdr.setBackground(Qt.GlobalColor.darkGray)
            hdr.setForeground(Qt.GlobalColor.white)
            font = hdr.font()
            font.setBold(True)
            hdr.setFont(font)
            tbl.setItem(row, 0, hdr)
            for c in range(1, 7):
                spacer = QTableWidgetItem("")
                spacer.setBackground(Qt.GlobalColor.darkGray)
                tbl.setItem(row, c, spacer)
            tbl.setSpan(row, 0, 1, 7)

            for idx in indices:
                idx_int = int(idx)
                try:
                    name = Od8Setting(idx_int).name
                except ValueError:
                    name = f"UNKNOWN_{idx_int}"

                mapping = OD8_RDNA4_FIELD_MAP.get(idx_int)
                conf = mapping.confidence if mapping else "?"
                unit = mapping.unit if mapping else ""

                row = tbl.rowCount()
                tbl.insertRow(row)

                idx_item = QTableWidgetItem(str(idx_int))
                idx_item.setFlags(idx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, 0, idx_item)

                name_item = QTableWidgetItem(name)
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, 1, name_item)

                conf_item = QTableWidgetItem(f"[{conf}]")
                conf_item.setFlags(conf_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if conf == "F":
                    conf_item.setForeground(Qt.GlobalColor.green)
                elif conf == "G":
                    conf_item.setForeground(Qt.GlobalColor.cyan)
                elif conf == "I":
                    conf_item.setForeground(Qt.GlobalColor.yellow)
                tbl.setItem(row, 2, conf_item)

                unit_item = QTableWidgetItem(unit)
                unit_item.setFlags(unit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, 3, unit_item)

                cv_label = QLabel("\u2014")
                cv_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                tbl.setCellWidget(row, 4, cv_label)

                sp_min, sp_max, sp_def, sp_suffix = self._escape_spin_params(idx_int)
                spin = make_spinbox(sp_min, sp_max, sp_def, sp_suffix)
                tbl.setCellWidget(row, 5, spin)

                set_btn = QPushButton("Set")
                set_btn.setMaximumWidth(60)
                set_btn.clicked.connect(
                    lambda _checked, i=idx_int: self._on_escape_set_single(i))
                tbl.setCellWidget(row, 6, set_btn)

                self._escape_od_widgets[idx_int] = {
                    "spin": spin, "cv_label": cv_label,
                }

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_w = QWidget()
        scroll_lay = QVBoxLayout(scroll_w)
        scroll_lay.addWidget(tbl)

        bottom_row = QHBoxLayout()
        self._escape_apply_btn = QPushButton("Apply All Modified")
        self._escape_apply_btn.setToolTip(
            "Send all rows whose input differs from the current driver value")
        self._escape_apply_btn.clicked.connect(self._on_escape_apply_all)
        bottom_row.addWidget(self._escape_apply_btn)

        self._escape_reset_btn = QPushButton("Reset to Defaults")
        self._escape_reset_btn.setToolTip("Send OD8 ResetFlag (index 71) to revert all OD settings")
        self._escape_reset_btn.clicked.connect(self._on_escape_reset)
        bottom_row.addWidget(self._escape_reset_btn)

        bottom_row.addStretch()
        scroll_lay.addLayout(bottom_row)
        scroll.setWidget(scroll_w)
        outer_layout.addWidget(scroll)

        self._escape_worker = None

    # ------------------------------------------------------------------
    # Escape OD handlers
    # ------------------------------------------------------------------

    def _on_escape_read(self):
        """Read all OD8 current values via D3DKMTEscape (no-op write)."""
        if self._escape_worker is not None and self._escape_worker.isRunning():
            return
        self._escape_read_btn.setEnabled(False)
        self._escape_status_label.setText("Reading...")

        def do_read():
            from src.io.d3dkmt_escape import D3DKMTClient
            client = D3DKMTClient.open_amd_adapter()
            try:
                client.query_session()
                client.query_session()
                values = client.od_read_current_values()
                return {"values": values}
            finally:
                client.close()

        self._escape_worker = EscapeWorker("Escape Read", do_read, self)
        self._escape_worker.result_signal.connect(self._on_escape_read_result)
        self._escape_worker.finished.connect(
            lambda: setattr(self, "_escape_worker", None))
        self._escape_worker.start()

    def _on_escape_read_result(self, action_name, result):
        self._escape_read_btn.setEnabled(True)
        if isinstance(result, dict) and "error" in result:
            self._escape_status_label.setText(f"Error: {result['error']}")
            self._log(f"Escape Read: {result['error']}")
            return
        values = result.get("values", {})
        self._escape_od_current_values = values
        ts = time.strftime("%H:%M:%S")
        for idx, wdict in self._escape_od_widgets.items():
            val = values.get(idx)
            if val is not None:
                wdict["cv_label"].setText(str(val))
                wdict["spin"].setValue(val)
            else:
                wdict["cv_label"].setText("\u2014")
        nonzero = {k: v for k, v in sorted(values.items()) if v != 0}
        self._escape_status_label.setText(
            f"Read OK at {ts} \u2014 {len(values)} values, {len(nonzero)} non-zero")
        self._log(f"Escape Read: {len(values)} OD8 values at {ts}, "
                  f"{len(nonzero)} non-zero")
        for idx, val in nonzero.items():
            try:
                name = Od8Setting(idx).name
            except ValueError:
                name = str(idx)
            self._log(f"  [{idx}] {name} = {val}")

    def _on_escape_set_single(self, idx):
        """Write a single OD8 index via D3DKMTEscape."""
        if self._escape_worker is not None and self._escape_worker.isRunning():
            self._log("Escape: operation already in progress")
            return
        wdict = self._escape_od_widgets.get(idx)
        if not wdict:
            return
        value = wdict["spin"].value()
        try:
            name = Od8Setting(idx).name
        except ValueError:
            name = str(idx)
        self._escape_status_label.setText(f"Writing idx {idx} ({name}) = {value}...")
        self._log(f"Escape Set: idx {idx} ({name}) = {value}")

        def do_write():
            from src.io.d3dkmt_escape import D3DKMTClient
            client = D3DKMTClient.open_amd_adapter()
            try:
                client.query_session()
                client.query_session()
                resp = client.od_write({idx: (value, 1)})
                readback = client.od_read_current_values()
                return {
                    "status": resp.status,
                    "values": readback,
                    "idx": idx,
                    "value": value,
                }
            finally:
                client.close()

        self._escape_worker = EscapeWorker(f"Escape Set [{idx}]", do_write, self)
        self._escape_worker.result_signal.connect(self._on_escape_write_result)
        self._escape_worker.finished.connect(
            lambda: setattr(self, "_escape_worker", None))
        self._escape_worker.start()

    def _on_escape_apply_all(self):
        """Write all modified OD8 indices via D3DKMTEscape."""
        if self._escape_worker is not None and self._escape_worker.isRunning():
            self._log("Escape: operation already in progress")
            return
        if not self._escape_od_current_values:
            self._log("Escape Apply: read current values first")
            self._escape_status_label.setText("Read current values first")
            return

        entries = {}
        for idx, wdict in self._escape_od_widgets.items():
            input_val = wdict["spin"].value()
            current_val = self._escape_od_current_values.get(idx)
            if current_val is not None and input_val != current_val:
                entries[idx] = (input_val, 1)

        if not entries:
            self._log("Escape Apply: no modified entries")
            self._escape_status_label.setText("No changes to apply")
            return

        self._escape_status_label.setText(f"Applying {len(entries)} entries...")
        idx_list = ", ".join(str(k) for k in sorted(entries))
        self._log(f"Escape Apply: sending {len(entries)} entries: [{idx_list}]")

        sent_detail = {idx: val for idx, (val, _) in entries.items()}

        def do_write():
            from src.io.d3dkmt_escape import D3DKMTClient
            client = D3DKMTClient.open_amd_adapter()
            try:
                client.query_session()
                client.query_session()
                resp = client.od_write(entries)
                readback = client.od_read_current_values()
                return {
                    "status": resp.status,
                    "values": readback,
                    "entries_sent": len(entries),
                    "sent_detail": sent_detail,
                }
            finally:
                client.close()

        self._escape_worker = EscapeWorker("Escape Apply All", do_write, self)
        self._escape_worker.result_signal.connect(self._on_escape_write_result)
        self._escape_worker.finished.connect(
            lambda: setattr(self, "_escape_worker", None))
        self._escape_worker.start()

    def _on_escape_reset(self):
        """Send ResetFlag (index 71) via D3DKMTEscape."""
        if self._escape_worker is not None and self._escape_worker.isRunning():
            self._log("Escape: operation already in progress")
            return
        self._escape_status_label.setText("Sending reset...")
        self._log("Escape Reset: sending ResetFlag (idx 71) = 1")

        def do_reset():
            from src.io.d3dkmt_escape import D3DKMTClient
            client = D3DKMTClient.open_amd_adapter()
            try:
                client.query_session()
                client.query_session()
                resp = client.od_write({Od8Setting.RESET_FLAG: (1, 1)})
                readback = client.od_read_current_values()
                return {
                    "status": resp.status,
                    "values": readback,
                }
            finally:
                client.close()

        self._escape_worker = EscapeWorker("Escape Reset", do_reset, self)
        self._escape_worker.result_signal.connect(self._on_escape_write_result)
        self._escape_worker.finished.connect(
            lambda: setattr(self, "_escape_worker", None))
        self._escape_worker.start()

    def _on_escape_write_result(self, action_name, result):
        """Handle escape write/reset result — update UI and current values."""
        if isinstance(result, dict) and "error" in result:
            self._escape_status_label.setText(f"Error: {result['error']}")
            self._log(f"{action_name}: {result['error']}")
            return
        status = result.get("status", -1)
        values = result.get("values", {})

        if status == 0:
            self._escape_status_label.setText(f"{action_name}: OK (status=0)")
            self._log(f"{action_name}: success (status=0)")
        else:
            try:
                fail_name = OdFail(status).name
            except ValueError:
                fail_name = f"UNKNOWN_{status}"
            self._escape_status_label.setText(
                f"{action_name}: REJECTED (status={status}, {fail_name})")
            self._log(f"{action_name}: rejected \u2014 status={status} ({fail_name})")

        old_values = self._escape_od_current_values
        self._escape_od_current_values = values
        for idx, wdict in self._escape_od_widgets.items():
            val = values.get(idx)
            if val is not None:
                wdict["cv_label"].setText(str(val))

        def _verify_line(idx, sent_val):
            old = old_values.get(idx, "?")
            new = values.get(idx, "?")
            try:
                name = Od8Setting(idx).name
            except ValueError:
                name = str(idx)
            if new == sent_val:
                tag = "CONFIRMED"
            elif old == new:
                tag = "UNCHANGED"
            else:
                tag = f"CHANGED (unexpected)"
            self._log(f"  [{idx}] {name}: before={old}, sent={sent_val}, "
                      f"readback={new} \u2014 {tag}")
            return new == sent_val

        if "idx" in result:
            _verify_line(result["idx"], result.get("value", "?"))

        sent_detail = result.get("sent_detail")
        if sent_detail:
            confirmed = sum(1 for idx, val in sent_detail.items()
                           if _verify_line(idx, val))
            self._log(f"  {confirmed}/{len(sent_detail)} confirmed in readback")


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Adrenalift")
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
    global _atexit_clean
    _log_to_file("main(): starting application")
    try:
        if getattr(sys, "frozen", False):
            ensure_driver_files_copied()
        app = QApplication(sys.argv)
        app.setApplicationName("Adrenalift")
        win = MainWindow()
        win.show()
        _log_to_file("main(): window shown, entering event loop")
        ret = app.exec()
        _log_to_file(f"main(): event loop exited with code {ret}")
        _atexit_clean = True
        return ret
    except SystemExit:
        _atexit_clean = True
        raise
    except Exception:
        _log_exception_to_file("main()")
        raise


if __name__ == "__main__":
    sys.exit(main())
