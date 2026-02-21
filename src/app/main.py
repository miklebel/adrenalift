"""
RDNA4 Overclock GUI -- PySide6 Main Window
==========================================

Main application window with:
  - VBIOS gate screen: file picker + copy to bios/ when no VBIOS present
  - Main overclock UI: Simple/Detailed Settings tabs, log panel, progress bar, Apply button
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QSize, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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

from src.io.vbios_parser import VbiosValues, parse_vbios_from_bytes, parse_vbios_or_defaults
from src.io.vbios_storage import read_vbios_decoded, write_vbios_encoded
from src.io.mmio import ensure_driver_files_copied

try:
    from src.tools.reg_patch import (
        RegistryPatch,
        PATCH_VALUES,
        BACKUP_FILE,
    )
except (ImportError, RuntimeError):
    RegistryPatch = None
    PATCH_VALUES = []
    BACKUP_FILE = None
from src.engine.overclock_engine import (
    OverclockSettings,
    ScanOptions,
    ScanResult,
    cleanup_hardware,
    init_hardware,
    apply_clocks_only,
    apply_msglimits_only,
    apply_od_table_only,
    apply_smu_features_only,
    scan_for_pptable,
    read_od,
    read_metrics,
    read_pptable_at_addr,
    is_valid_pptable,
)

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
        try:
            hw = init_hardware()
            self.apply_fn(hw)
        except Exception as e:
            err = str(e)
        finally:
            if hw:
                try:
                    cleanup_hardware(hw)
                except Exception:
                    pass
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
        try:
            result = self.fn()
        except Exception as e:
            err = str(e)
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
        try:
            hw = init_hardware()
            inpout = hw["inpout"]
        except Exception:
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
                    results.append((addr, "Error", None))
        finally:
            cleanup_hardware(hw)
        self.results_signal.emit(results)


# ---------------------------------------------------------------------------
# Detailed Tab Refresh Worker
# ---------------------------------------------------------------------------


class DetailedRefreshWorker(QThread):
    """Background worker to read Live RAM (PPTable) and Live SMU (OD + metrics) for Detailed tab."""

    results_signal = Signal(object, object, object)  # ram_data, od_table, (gfxclk, gfxclk2, ppt, temp)

    def __init__(self, valid_addrs: list, parent=None):
        super().__init__(parent)
        self.valid_addrs = valid_addrs

    def run(self):
        ram_data = None
        od_table = None
        metrics = None
        hw = None
        try:
            hw = init_hardware()
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
                    pass

            try:
                od_table = read_od(smu, virt)
            except Exception:
                pass

            try:
                metrics = read_metrics(smu, virt)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            if hw:
                cleanup_hardware(hw)
        self.results_signal.emit(ram_data, od_table, metrics)


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
        vbios_values = self.get_vbios_fn()
        if vbios_values is None:
            vbios_values = parse_vbios_or_defaults(DEFAULT_VBIOS_PATH)

        hw = None
        try:
            hw = init_hardware()
            inpout = hw["inpout"]
        except Exception as e:
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
                merged = sorted(set(result.valid_addrs) | set(self.merge_with_addrs))
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
            self.finished_signal.emit(result)
        except Exception as e:
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

    def __init__(self, vbios_values: VbiosValues, *, used_defaults: bool = False, diagnostic_lines: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.vbios_values = vbios_values
        self.used_defaults = used_defaults
        self.diagnostic_lines = diagnostic_lines or []
        self.scan_result: ScanResult | None = None
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
        self.detailed_tab = QWidget()
        self.memory_tab = QWidget()
        self.registry_tab = QWidget()
        self._setup_simple_tab()
        self._setup_detailed_tab()
        self._setup_memory_tab()
        self._setup_registry_tab()
        self.tabs.addTab(self.simple_tab, "Simple Settings")
        self.tabs.addTab(self.detailed_tab, "Detailed Settings")
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
        self.scan_status_label = QLabel("Scanning for PPTable...")
        self.scan_status_label.setStyleSheet("color: #888; font-size: 9pt;")
        scan_row.addWidget(self.scan_status_label)
        scan_row.addStretch()
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.setToolTip("Rescan memory and add new PPTable addresses to the pool")
        self.rescan_btn.clicked.connect(self._on_rescan)
        self.rescan_btn.setEnabled(False)
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
            self._log("Starting background scan...")
        else:
            self._log("VBIOS values loaded. Starting background scan...")

        self._scan_thread = ScanThread(lambda: _get_vbios_values())
        self._scan_thread.progress_signal.connect(self._on_scan_progress)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.start()

        # Apply buttons start disabled until scan completes
        self._set_apply_buttons_enabled(False)
        self._apply_worker = None

    def _setup_simple_tab(self):
        outer = QVBoxLayout(self.simple_tab)
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
        self._log("Rescanning for additional PPTable addresses...")

    def _update_detailed_live_columns(self, ram_data, od_table, metrics):
        """Update Live RAM and Live SMU columns in all Detailed tables from refresh results."""
        def _fmt(val, suffix=""):
            if val is not None:
                return f"{val}{suffix}"
            return "—"

        for section_name, table in self._detailed_tables.items():
            for row in range(table.rowCount()):
                key = table.item(row, 1).text() if table.item(row, 1) else ""
                ram_key = self._param_ram_key.get(key)
                smu_key = self._param_smu_key.get(key)

                live_ram_item = table.item(row, 5)
                live_smu_item = table.item(row, 6)
                if live_ram_item is None:
                    live_ram_item = QTableWidgetItem()
                    table.setItem(row, 5, live_ram_item)
                if live_smu_item is None:
                    live_smu_item = QTableWidgetItem()
                    table.setItem(row, 6, live_smu_item)

                if ram_key and ram_data:
                    val = ram_data.get(ram_key)
                    live_ram_item.setText(_fmt(val, self._param_unit.get(key, "")))
                elif ram_key:
                    live_ram_item.setText("Unavailable" if section_name == "PP" else "—")
                else:
                    live_ram_item.setText("—")

                if smu_key == "od":
                    if od_table:
                        val = getattr(od_table, key, None)
                        if val is not None:
                            unit = self._param_unit.get(key, "")
                            live_smu_item.setText(_fmt(val, unit))
                        else:
                            live_smu_item.setText("—")
                    else:
                        live_smu_item.setText("Unavailable" if section_name in ("OD", "SMU") else "—")
                elif smu_key == "gfxclk" and metrics:
                    live_smu_item.setText(_fmt(metrics[0], " MHz"))
                elif smu_key == "ppt" and metrics:
                    live_smu_item.setText(_fmt(metrics[2], " W"))
                elif smu_key == "temp" and metrics:
                    live_smu_item.setText(_fmt(metrics[3], " °C"))
                else:
                    live_smu_item.setText("—")

    def _update_od_from_scan(self, od):
        """Update OD Custom input spinboxes and Live SMU from scan_result.od_table."""
        if od is None:
            return
        w = self._detailed_param_widgets
        if "GfxclkFoffset" in w:
            w["GfxclkFoffset"].setValue(max(0, od.GfxclkFoffset))
        if "Ppt" in w:
            w["Ppt"].setValue(od.Ppt)
        if "Tdc" in w:
            w["Tdc"].setValue(od.Tdc)
        if "UclkFmin" in w:
            w["UclkFmin"].setValue(od.UclkFmin)
        if "UclkFmax" in w:
            w["UclkFmax"].setValue(od.UclkFmax)
        if "FclkFmin" in w:
            w["FclkFmin"].setValue(od.FclkFmin)
        if "FclkFmax" in w:
            w["FclkFmax"].setValue(od.FclkFmax)
        self._update_detailed_live_columns(None, od, None)

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
            self._log(
                f"Scan complete: found {len(result.valid_addrs)} valid PPTable(s) at "
                + ", ".join(f"0x{a:012X}" for a in result.valid_addrs)
            )
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

    def _setup_detailed_tab(self):
        layout = QVBoxLayout(self.detailed_tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        scroll_layout = QVBoxLayout(content)

        vb = self.vbios_values

        # Param definitions: (human_name, table_key, source, unit, vbios_val, ram_key, smu_key)
        # smu_key: "od" = from od_table (use table_key as attr), "gfxclk"/"ppt"/"temp" = from metrics
        self._param_ram_key = {}
        self._param_smu_key = {}
        self._param_unit = {}
        self._detailed_param_widgets = {}
        self._detailed_tables = {}

        def _add_pp_row(table, human, key, unit, vb_val, ram_key, smu_key, widget):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem("PP"))
            table.setItem(row, 3, QTableWidgetItem(unit))
            table.setItem(row, 4, QTableWidgetItem(str(vb_val) if vb_val is not None else "—"))
            table.setItem(row, 5, QTableWidgetItem("—"))
            table.setItem(row, 6, QTableWidgetItem("—"))
            table.setCellWidget(row, 7, widget)
            self._param_ram_key[key] = ram_key
            self._param_smu_key[key] = smu_key
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget

        def _add_od_row(table, human, key, unit, vb_val, smu_key, widget):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem("OD"))
            table.setItem(row, 3, QTableWidgetItem(unit))
            table.setItem(row, 4, QTableWidgetItem(str(vb_val) if vb_val is not None else "—"))
            table.setItem(row, 5, QTableWidgetItem("—"))
            table.setItem(row, 6, QTableWidgetItem("—"))
            table.setCellWidget(row, 7, widget)
            self._param_smu_key[key] = smu_key
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget

        def _add_smu_row(table, human, key, unit, vb_val, widget):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(human))
            table.setItem(row, 1, QTableWidgetItem(key))
            table.setItem(row, 2, QTableWidgetItem("SMU"))
            table.setItem(row, 3, QTableWidgetItem(unit))
            table.setItem(row, 4, QTableWidgetItem(str(vb_val) if vb_val is not None else "—"))
            table.setItem(row, 5, QTableWidgetItem("—"))
            table.setItem(row, 6, QTableWidgetItem("—"))
            table.setCellWidget(row, 7, widget)
            self._param_unit[key] = f" {unit}" if unit else ""
            self._detailed_param_widgets[key] = widget

        # (1) PP Section: Clocks + MsgLimits
        pp_grp = QGroupBox("PP — Clocks & MsgLimits")
        pp_table = QTableWidget()
        pp_table.setColumnCount(8)
        pp_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Source", "Unit",
            "VBIOS", "Live RAM", "Live SMU", "Custom input",
        ])
        pp_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        pp_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["PP"] = pp_table

        det_game_clock = QSpinBox()
        det_game_clock.setRange(500, 5000)
        det_game_clock.setValue(vb.gameclock_ac)
        det_game_clock.setSuffix(" MHz")
        _add_pp_row(pp_table, "Game Clock", "GameClockAc", "MHz", vb.gameclock_ac,
                    "gameclock_ac", "gfxclk", det_game_clock)

        det_boost_clock = QSpinBox()
        det_boost_clock.setRange(500, 5000)
        det_boost_clock.setValue(vb.boostclock_ac)
        det_boost_clock.setSuffix(" MHz")
        _add_pp_row(pp_table, "Boost Clock", "BoostClockAc", "MHz", vb.boostclock_ac,
                    "boostclock_ac", None, det_boost_clock)

        det_power_ac = QSpinBox()
        det_power_ac.setRange(50, 600)
        det_power_ac.setValue(vb.power_ac)
        det_power_ac.setSuffix(" W")
        _add_pp_row(pp_table, "PPT AC", "PPT0_AC", "W", vb.power_ac,
                    "ppt0_ac", "ppt", det_power_ac)

        det_power_dc = QSpinBox()
        det_power_dc.setRange(50, 600)
        det_power_dc.setValue(vb.power_dc)
        det_power_dc.setSuffix(" W")
        _add_pp_row(pp_table, "PPT DC", "PPT0_DC", "W", vb.power_dc,
                    "ppt0_dc", None, det_power_dc)

        det_tdc_gfx = QSpinBox()
        det_tdc_gfx.setRange(20, 500)
        det_tdc_gfx.setValue(vb.tdc_gfx)
        det_tdc_gfx.setSuffix(" A")
        _add_pp_row(pp_table, "TDC GFX", "TDC_GFX", "A", vb.tdc_gfx,
                    "tdc_gfx", None, det_tdc_gfx)

        det_tdc_soc = QSpinBox()
        det_tdc_soc.setRange(0, 200)
        det_tdc_soc.setValue(vb.tdc_soc)
        det_tdc_soc.setSuffix(" A")
        _add_pp_row(pp_table, "TDC SOC", "TDC_SOC", "A", vb.tdc_soc,
                    "tdc_soc", None, det_tdc_soc)

        det_temp_edge = QSpinBox()
        det_temp_edge.setRange(0, 150)
        det_temp_edge.setValue(vb.temp_edge if vb.temp_edge else 100)
        det_temp_edge.setSuffix(" °C")
        _add_pp_row(pp_table, "Temp Edge", "Temp_Edge", "°C", vb.temp_edge or "—",
                    "temp_edge", "temp", det_temp_edge)

        det_temp_hotspot = QSpinBox()
        det_temp_hotspot.setRange(0, 150)
        det_temp_hotspot.setValue(vb.temp_hotspot if vb.temp_hotspot else 110)
        det_temp_hotspot.setSuffix(" °C")
        _add_pp_row(pp_table, "Temp Hotspot", "Temp_Hotspot", "°C", vb.temp_hotspot or "—",
                    "temp_hotspot", None, det_temp_hotspot)

        det_temp_mem = QSpinBox()
        det_temp_mem.setRange(0, 150)
        det_temp_mem.setValue(vb.temp_mem if vb.temp_mem else 100)
        det_temp_mem.setSuffix(" °C")
        _add_pp_row(pp_table, "Temp Mem", "Temp_Mem", "°C", vb.temp_mem or "—",
                    "temp_mem", None, det_temp_mem)

        det_temp_vr_gfx = QSpinBox()
        det_temp_vr_gfx.setRange(0, 200)
        det_temp_vr_gfx.setValue(vb.temp_vr_gfx if vb.temp_vr_gfx else 115)
        det_temp_vr_gfx.setSuffix(" °C")
        _add_pp_row(pp_table, "Temp VR GFX", "Temp_VR_GFX", "°C", vb.temp_vr_gfx or "—",
                    "temp_vr_gfx", None, det_temp_vr_gfx)

        det_temp_vr_soc = QSpinBox()
        det_temp_vr_soc.setRange(0, 200)
        det_temp_vr_soc.setValue(vb.temp_vr_soc if vb.temp_vr_soc else 115)
        det_temp_vr_soc.setSuffix(" °C")
        _add_pp_row(pp_table, "Temp VR SOC", "Temp_VR_SOC", "°C", vb.temp_vr_soc or "—",
                    "temp_vr_soc", None, det_temp_vr_soc)

        pp_layout = QVBoxLayout(pp_grp)
        pp_layout.addWidget(pp_table)
        self.clocks_apply_btn = QPushButton("Apply PP")
        self.clocks_apply_btn.setToolTip("Patches clocks and MsgLimits in RAM, sends SetSoftMin/Max and SetPptLimit to SMU")
        self.clocks_apply_btn.clicked.connect(self._on_apply_pp)
        self.msglimits_apply_btn = self.clocks_apply_btn  # same button for _set_apply_buttons_enabled
        pp_layout.addWidget(self.clocks_apply_btn)
        scroll_layout.addWidget(pp_grp)

        # (2) OD Section
        od_grp = QGroupBox("OD — OverDrive Table")
        od_table = QTableWidget()
        od_table.setColumnCount(8)
        od_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Source", "Unit",
            "VBIOS", "Live RAM", "Live SMU", "Custom input",
        ])
        od_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        od_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["OD"] = od_table

        det_gfx_offset = QSpinBox()
        det_gfx_offset.setRange(0, 2000)
        det_gfx_offset.setValue(200)
        det_gfx_offset.setSuffix(" MHz")
        _add_od_row(od_table, "Gfxclk Offset", "GfxclkFoffset", "MHz", None, "od", det_gfx_offset)

        det_od_ppt = QSpinBox()
        det_od_ppt.setRange(-50, 100)
        det_od_ppt.setValue(10)
        det_od_ppt.setSuffix("%")
        _add_od_row(od_table, "PPT %", "Ppt", "%", None, "od", det_od_ppt)

        det_od_tdc = QSpinBox()
        det_od_tdc.setRange(-50, 100)
        det_od_tdc.setValue(0)
        det_od_tdc.setSuffix("%")
        _add_od_row(od_table, "TDC %", "Tdc", "%", None, "od", det_od_tdc)

        det_uclk_min = QSpinBox()
        det_uclk_min.setRange(0, 3000)
        det_uclk_min.setValue(0)
        det_uclk_min.setSpecialValueText("no change")
        det_uclk_min.setSuffix(" MHz")
        _add_od_row(od_table, "UCLK min", "UclkFmin", "MHz", None, "od", det_uclk_min)

        det_uclk_max = QSpinBox()
        det_uclk_max.setRange(0, 3000)
        det_uclk_max.setValue(0)
        det_uclk_max.setSpecialValueText("no change")
        det_uclk_max.setSuffix(" MHz")
        _add_od_row(od_table, "UCLK max", "UclkFmax", "MHz", None, "od", det_uclk_max)

        det_fclk_min = QSpinBox()
        det_fclk_min.setRange(0, 3000)
        det_fclk_min.setValue(0)
        det_fclk_min.setSpecialValueText("no change")
        det_fclk_min.setSuffix(" MHz")
        _add_od_row(od_table, "FCLK min", "FclkFmin", "MHz", None, "od", det_fclk_min)

        det_fclk_max = QSpinBox()
        det_fclk_max.setRange(0, 3000)
        det_fclk_max.setValue(0)
        det_fclk_max.setSpecialValueText("no change")
        det_fclk_max.setSuffix(" MHz")
        _add_od_row(od_table, "FCLK max", "FclkFmax", "MHz", None, "od", det_fclk_max)

        od_layout = QVBoxLayout(od_grp)
        od_layout.addWidget(od_table)
        self.od_apply_btn = QPushButton("Apply OD")
        self.od_apply_btn.setToolTip("Sends OD table (offset, PPT%, TDC%, UCLK/FCLK) to SMU via table transfer")
        self.od_apply_btn.clicked.connect(self._on_apply_od)
        od_layout.addWidget(self.od_apply_btn)
        scroll_layout.addWidget(od_grp)

        # (3) SMU Section
        smu_grp = QGroupBox("SMU — Features")
        smu_table = QTableWidget()
        smu_table.setColumnCount(8)
        smu_table.setHorizontalHeaderLabels([
            "Human name", "Table key", "Source", "Unit",
            "VBIOS", "Live RAM", "Live SMU", "Custom input",
        ])
        smu_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        smu_table.horizontalHeader().setStretchLastSection(True)
        self._detailed_tables["SMU"] = smu_table

        det_lock_features = QCheckBox()
        det_lock_features.setChecked(False)
        _add_smu_row(smu_table, "Lock features", "LockFeatures", "", "Off", det_lock_features)

        det_min_clock = QSpinBox()
        det_min_clock.setRange(0, 5000)
        det_min_clock.setValue(0)
        det_min_clock.setSpecialValueText("use game clock")
        det_min_clock.setSuffix(" MHz")
        _add_smu_row(smu_table, "Min-clock floor", "MinClock", "MHz", 0, det_min_clock)

        smu_layout = QVBoxLayout(smu_grp)
        smu_layout.addWidget(smu_table)
        self.smu_apply_btn = QPushButton("Apply SMU")
        self.smu_apply_btn.setToolTip("Sets min-clock floor and DisableSmuFeaturesLow (lock DS_GFXCLK/GFX_ULV/GFXOFF)")
        self.smu_apply_btn.clicked.connect(self._on_apply_smu_features)
        smu_layout.addWidget(self.smu_apply_btn)
        scroll_layout.addWidget(smu_grp)

        scroll_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        # Detailed tab refresh: 1s timer when scan has addrs or od_table
        self._detailed_worker = None
        self._detailed_timer = QTimer(self)
        self._detailed_timer.setInterval(1000)
        self._detailed_timer.timeout.connect(self._on_detailed_refresh_tick)

    def _setup_memory_tab(self):
        """Memory tab: live view of PPTable copies in RAM, 1s refresh."""
        layout = QVBoxLayout(self.memory_tab)

        memory_tooltip = (
            "First row: VBIOS (reference) = original values from bios/vbios.rom. "
            "Other rows: Live PPTable data in RAM (may be patched). "
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
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(1000)
        self._memory_timer.timeout.connect(self._on_memory_refresh_tick)

        self._update_memory_placeholder("Scanning...")

    def _setup_registry_tab(self):
        """Registry Patch tab: table with Name, Current (read-only checkbox), Custom (input checkbox)."""
        layout = QVBoxLayout(self.registry_tab)
        self._reg_worker = None
        self._reg_checkboxes = {}

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

        # Table: Name, Current (disabled checkbox), Custom (input checkbox)
        self.reg_table = QTableWidget()
        self.reg_table.setColumnCount(3)
        self.reg_table.setHorizontalHeaderLabels(["Name", "Current", "Custom"])
        self.reg_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.reg_table.horizontalHeader().setStretchLastSection(True)

        patch_data = self._reg_report.get("patch", {})
        for name, entry in patch_data.items():
            row = self.reg_table.rowCount()
            self.reg_table.insertRow(row)
            current = entry.get("current")
            # Checkbox: checked = 1, unchecked = 0
            current_is_one = current == 1 if current is not None else False

            self.reg_table.setItem(row, 0, QTableWidgetItem(name))

            current_cb = QCheckBox()
            current_cb.setChecked(current_is_one)
            current_cb.setEnabled(False)
            current_cb.setToolTip("Current registry value (read-only)")
            self.reg_table.setCellWidget(row, 1, current_cb)

            custom_cb = QCheckBox()
            custom_cb.setChecked(current_is_one)
            custom_cb.setToolTip("Value to apply: checked=1, unchecked=0")
            self._reg_checkboxes[name] = custom_cb
            self.reg_table.setCellWidget(row, 2, custom_cb)

        layout.addWidget(self.reg_table)

        # Buttons
        btn_row = QHBoxLayout()
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
        """Apply Custom column values (checked=1, unchecked=0) to registry."""
        if self._reg_patch is None:
            return
        if hasattr(self, "_reg_worker") and self._reg_worker is not None and self._reg_worker.isRunning():
            return

        values = {n: 1 if cb.isChecked() else 0 for n, cb in self._reg_checkboxes.items()}

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

    def _update_reg_table(self, report: dict):
        """Update registry table from report."""
        if not hasattr(self, "reg_table") or self.reg_table is None:
            return
        patch_data = report.get("patch", {})
        self.reg_table.setRowCount(0)
        self._reg_checkboxes.clear()
        for name, entry in patch_data.items():
            row = self.reg_table.rowCount()
            self.reg_table.insertRow(row)
            current = entry.get("current")
            current_is_one = current == 1 if current is not None else False

            self.reg_table.setItem(row, 0, QTableWidgetItem(name))

            current_cb = QCheckBox()
            current_cb.setChecked(current_is_one)
            current_cb.setEnabled(False)
            current_cb.setToolTip("Current registry value (read-only)")
            self.reg_table.setCellWidget(row, 1, current_cb)

            custom_cb = QCheckBox()
            custom_cb.setChecked(current_is_one)
            custom_cb.setToolTip("Value to apply: checked=1, unchecked=0")
            self._reg_checkboxes[name] = custom_cb
            self.reg_table.setCellWidget(row, 2, custom_cb)

    def _on_memory_refresh_tick(self):
        """Timer tick: refresh memory table if we have addrs and worker is idle."""
        if self._memory_worker is not None and self._memory_worker.isRunning():
            return
        if self.scan_result is None:
            self._update_memory_placeholder("Scanning...")
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        if not addrs:
            self._memory_timer.stop()
            self._update_memory_placeholder("No addresses")
            return
        self._memory_worker = MemoryRefreshWorker(addrs, self)
        self._memory_worker.results_signal.connect(self._on_memory_refresh_results)
        self._memory_worker.finished.connect(lambda: setattr(self, "_memory_worker", None))
        self._memory_worker.start()

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
        """Start 1s refresh timer when scan has valid_addrs."""
        if self.scan_result and getattr(self.scan_result, "valid_addrs", None):
            addrs = self.scan_result.valid_addrs
            if addrs and not self._memory_timer.isActive():
                self._memory_timer.start()
                self._on_memory_refresh_tick()

    def _on_detailed_refresh_tick(self):
        """Timer tick: refresh Detailed tab Live RAM/Live SMU columns."""
        if self._detailed_worker is not None and self._detailed_worker.isRunning():
            return
        if self.scan_result is None:
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        if not addrs and not getattr(self.scan_result, "od_table", None):
            return
        self._detailed_worker = DetailedRefreshWorker(addrs, self)
        self._detailed_worker.results_signal.connect(self._on_detailed_refresh_results)
        self._detailed_worker.finished.connect(lambda: setattr(self, "_detailed_worker", None))
        self._detailed_worker.start()

    def _on_detailed_refresh_results(self, ram_data, od_table, metrics):
        """Update Detailed tab Live columns from worker results."""
        self._update_detailed_live_columns(ram_data, od_table, metrics)

    def _start_detailed_refresh_if_ready(self):
        """Start 1s refresh timer for Detailed tab when scan has addrs or od_table."""
        if not self.scan_result:
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        od = getattr(self.scan_result, "od_table", None)
        if (addrs or od) and not self._detailed_timer.isActive():
            self._detailed_timer.start()
            self._on_detailed_refresh_tick()

    def _log(self, msg: str):
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
        """Enable or disable all section Apply buttons."""
        self.simple_apply_btn.setEnabled(enabled)
        self.clocks_apply_btn.setEnabled(enabled)
        self.msglimits_apply_btn.setEnabled(enabled)
        self.od_apply_btn.setEnabled(enabled)
        self.smu_apply_btn.setEnabled(enabled)

    def _run_with_hardware(self, action_name: str, apply_fn):
        """Run apply_fn(hw) in background thread. Prevents UI freeze (PP apply does scan_memory)."""
        if not self._can_apply():
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

    def _on_apply_simple(self):
        """Apply Simple tab: clock + offset (patches PPTable clocks + applies OD)."""
        settings = self.get_simple_settings()
        self._log(f"Simple Apply: clock={settings.clock} MHz, offset={settings.offset} MHz")

        def do_apply(hw):
            inpout, smu, virt = hw["inpout"], hw["smu"], hw["virt"]
            if self.scan_result and self.scan_result.valid_addrs:
                apply_clocks_only(inpout, smu, self.scan_result, settings)
                self._log("Clocks patched + freq limits sent.")
            apply_od_table_only(smu, virt, settings, only_offset=True)
            self._log("OD table (offset only) applied.")
            self._log("Simple Apply done.")

        self._run_with_hardware("Simple Apply", do_apply)

    def _on_apply_pp(self):
        """Apply PP section: clocks + MsgLimits (patch RAM, send SMU commands)."""
        settings = self.get_detailed_settings()
        self._log(f"Apply PP: Game={settings._game_clock()} Boost={settings._boost_clock()} MHz, PPT={settings._power_ac()}W")

        def do_apply(hw):
            vb = _get_vbios_values()
            if vb is None:
                vb = self.vbios_values
            if self.scan_result and self.scan_result.valid_addrs:
                apply_clocks_only(hw["inpout"], hw["smu"], self.scan_result, settings)
                apply_msglimits_only(
                    hw["inpout"], hw["smu"], self.scan_result, settings, ScanOptions(),
                    vbios_values=vb,
                )
            self._log("PP applied.")

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

    def _on_apply_smu_features(self):
        settings = self.get_detailed_settings()
        self._log(f"SMU Features Apply: min_clock={settings.effective_min_clock} MHz, lock={settings.lock_features}")

        def do_apply(hw):
            apply_smu_features_only(hw["smu"], settings)
            self._log("SMU features applied.")

        self._run_with_hardware("SMU Features Apply", do_apply)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RDNA4 Overclock")
        self.setMinimumSize(520, 480)
        self.resize(600, 560)

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
    # When frozen: copy driver files to exe dir before anything else so first run
    # matches post-restart layout (parse was failing until drivers existed)
    if getattr(sys, "frozen", False):
        ensure_driver_files_copied()
    app = QApplication(sys.argv)
    app.setApplicationName("RDNA4 Overclock")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
