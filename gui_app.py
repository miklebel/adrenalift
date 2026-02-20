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

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# When frozen by PyInstaller, use exe dir for bios/ and user data
if getattr(sys, "frozen", False):
    _script_dir = os.path.dirname(sys.executable)
else:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from vbios_parser import VbiosValues, parse_vbios, parse_vbios_from_bytes, parse_vbios_or_defaults
from mmio import ensure_driver_files_copied
from overclock_engine import (
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
)

# Default VBIOS path relative to script directory
DEFAULT_VBIOS_PATH = os.path.join(_script_dir, "bios", "vbios.rom")


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

        bios_dir = os.path.join(_script_dir, "bios")
        dest_path = os.path.join(bios_dir, "vbios.rom")
        try:
            os.makedirs(bios_dir, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(rom_bytes)
        except OSError as e:
            self.status_label.setText(f"Failed to copy: {e}")
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
# Background Scan Thread
# ---------------------------------------------------------------------------


class ScanThread(QThread):
    """Background scan for PPTable addresses. Uses 3-tier strategy: probe cache -> window scan -> full scan."""

    progress_signal = Signal(float, str)  # pct, msg
    finished_signal = Signal(object)  # ScanResult or None on error

    def __init__(self, vbios_values: VbiosValues, parent=None):
        super().__init__(parent)
        self.vbios_values = vbios_values

    def run(self):
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
                game_clock=self.vbios_values.gameclock_ac,
                boost_clock=self.vbios_values.boostclock_ac,
                clock=self.vbios_values.gameclock_ac,
            )
            scan_opts = ScanOptions()

            def on_progress(pct: float, msg: str):
                self.progress_signal.emit(pct, msg)

            result = scan_for_pptable(
                inpout,
                settings,
                scan_opts=scan_opts,
                progress_callback=on_progress,
                vbios_values=self.vbios_values,
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
        self._setup_simple_tab()
        self._setup_detailed_tab()
        self.tabs.addTab(self.simple_tab, "Simple Settings")
        self.tabs.addTab(self.detailed_tab, "Detailed Settings")
        layout.addWidget(self.tabs)

        # Progress bar and scan status
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.scan_status_label = QLabel("Scanning for PPTable...")
        self.scan_status_label.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(self.scan_status_label)

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

        self._scan_thread = ScanThread(vbios_values)
        self._scan_thread.progress_signal.connect(self._on_scan_progress)
        self._scan_thread.finished_signal.connect(self._on_scan_finished)
        self._scan_thread.start()

        # Apply buttons start disabled until scan completes
        self._set_apply_buttons_enabled(False)

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

    def _update_od_orig_labels(self, od):
        """Update OD Table 'orig' labels and spinbox values from read_od result."""
        self._od_label_gfx.setText(f"GfxclkFoffset (orig: {od.GfxclkFoffset} MHz):")
        self._od_label_ppt.setText(f"Ppt % (orig: {od.Ppt}%):")
        self._od_label_tdc.setText(f"Tdc % (orig: {od.Tdc}%):")
        self._od_label_uclk_min.setText(f"UCLK min (orig: {od.UclkFmin} MHz):")
        self._od_label_uclk_max.setText(f"UCLK max (orig: {od.UclkFmax} MHz):")
        self._od_label_fclk_min.setText(f"FCLK min (orig: {od.FclkFmin} MHz):")
        self._od_label_fclk_max.setText(f"FCLK max (orig: {od.FclkFmax} MHz):")
        self.det_gfx_offset.setValue(max(0, od.GfxclkFoffset))
        self.det_od_ppt.setValue(od.Ppt)
        self.det_od_tdc.setValue(od.Tdc)
        self.det_uclk_min.setValue(od.UclkFmin)
        self.det_uclk_max.setValue(od.UclkFmax)
        self.det_fclk_min.setValue(od.FclkFmin)
        self.det_fclk_max.setValue(od.FclkFmax)

    def _on_scan_finished(self, result: ScanResult | None):
        self.scan_result = result
        if result is None:
            self._log("Scan failed (no result).")
            self.scan_status_label.setText("Scan failed.")
            self._set_apply_buttons_enabled(False)
            self.progress_bar.setValue(100)
            return
        if result.error:
            self._log(f"Scan failed: {result.error}")
            if getattr(result, "od_table", None) is not None:
                self._update_od_orig_labels(result.od_table)
                self.scan_status_label.setText(
                    "PPTable not found — OD/SMU apply available"
                )
                self._set_apply_buttons_enabled(True)
            else:
                self.scan_status_label.setText(f"Scan failed: {result.error}")
                self._set_apply_buttons_enabled(False)
            self.progress_bar.setValue(100)
            return
        if getattr(result, "od_table", None) is not None:
            self._update_od_orig_labels(result.od_table)
        self._set_apply_buttons_enabled(True)
        if result.valid_addrs:
            self._log(
                f"Scan complete: found {len(result.valid_addrs)} valid PPTable(s) at "
                + ", ".join(f"0x{a:012X}" for a in result.valid_addrs)
            )
            self.scan_status_label.setText(
                f"Ready — {len(result.valid_addrs)} PPTable(s) found"
            )
        else:
            self._log("Scan complete: no valid PPTable addresses found.")
            self.scan_status_label.setText(
                "No PPTable found — OD/SMU apply available."
            )
        self.progress_bar.setValue(100)

    def _setup_detailed_tab(self):
        layout = QVBoxLayout(self.detailed_tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        scroll_layout = QVBoxLayout(content)

        vb = self.vbios_values

        # (1) Clocks: GameClockAc, BoostClockAc
        clocks_grp = QGroupBox("Clocks")
        clocks_form = QFormLayout(clocks_grp)
        self.det_game_clock = QSpinBox()
        self.det_game_clock.setRange(500, 5000)
        self.det_game_clock.setValue(vb.gameclock_ac)
        self.det_game_clock.setSuffix(" MHz")
        clocks_form.addRow(f"GameClockAc (orig: {vb.gameclock_ac} MHz):", self.det_game_clock)
        self.det_boost_clock = QSpinBox()
        self.det_boost_clock.setRange(500, 5000)
        self.det_boost_clock.setValue(vb.boostclock_ac)
        self.det_boost_clock.setSuffix(" MHz")
        clocks_form.addRow(f"BoostClockAc (orig: {vb.boostclock_ac} MHz):", self.det_boost_clock)
        self.clocks_apply_btn = QPushButton("Apply")
        self.clocks_apply_btn.clicked.connect(self._on_apply_clocks)
        clocks_form.addRow("", self.clocks_apply_btn)
        scroll_layout.addWidget(clocks_grp)

        # (2) MsgLimits: PPT AC/DC watts, TDC GFX/SOC amps, Temp limits
        msg_grp = QGroupBox("MsgLimits")
        msg_form = QFormLayout(msg_grp)
        self.det_power_ac = QSpinBox()
        self.det_power_ac.setRange(50, 600)
        self.det_power_ac.setValue(vb.power_ac)
        self.det_power_ac.setSuffix(" W")
        msg_form.addRow(f"PPT AC (orig: {vb.power_ac} W):", self.det_power_ac)
        self.det_power_dc = QSpinBox()
        self.det_power_dc.setRange(50, 600)
        self.det_power_dc.setValue(vb.power_dc)
        self.det_power_dc.setSuffix(" W")
        msg_form.addRow(f"PPT DC (orig: {vb.power_dc} W):", self.det_power_dc)
        self.det_tdc_gfx = QSpinBox()
        self.det_tdc_gfx.setRange(20, 500)
        self.det_tdc_gfx.setValue(vb.tdc_gfx)
        self.det_tdc_gfx.setSuffix(" A")
        msg_form.addRow(f"TDC GFX (orig: {vb.tdc_gfx} A):", self.det_tdc_gfx)
        self.det_tdc_soc = QSpinBox()
        self.det_tdc_soc.setRange(0, 200)
        self.det_tdc_soc.setValue(vb.tdc_soc)
        self.det_tdc_soc.setSuffix(" A")
        msg_form.addRow(f"TDC SOC (orig: {vb.tdc_soc} A):", self.det_tdc_soc)
        self.det_temp_edge = QSpinBox()
        self.det_temp_edge.setRange(0, 150)
        self.det_temp_edge.setValue(vb.temp_edge if vb.temp_edge else 100)
        self.det_temp_edge.setSuffix(" °C")
        msg_form.addRow(f"Temp Edge (orig: {vb.temp_edge or '—'}°C):", self.det_temp_edge)
        self.det_temp_hotspot = QSpinBox()
        self.det_temp_hotspot.setRange(0, 150)
        self.det_temp_hotspot.setValue(vb.temp_hotspot if vb.temp_hotspot else 110)
        self.det_temp_hotspot.setSuffix(" °C")
        msg_form.addRow(f"Temp Hotspot (orig: {vb.temp_hotspot or '—'}°C):", self.det_temp_hotspot)
        self.det_temp_mem = QSpinBox()
        self.det_temp_mem.setRange(0, 150)
        self.det_temp_mem.setValue(vb.temp_mem if vb.temp_mem else 100)
        self.det_temp_mem.setSuffix(" °C")
        msg_form.addRow(f"Temp Mem (orig: {vb.temp_mem or '—'}°C):", self.det_temp_mem)
        self.det_temp_vr_gfx = QSpinBox()
        self.det_temp_vr_gfx.setRange(0, 200)
        self.det_temp_vr_gfx.setValue(vb.temp_vr_gfx if vb.temp_vr_gfx else 115)
        self.det_temp_vr_gfx.setSuffix(" °C")
        msg_form.addRow(f"Temp VR GFX (orig: {vb.temp_vr_gfx or '—'}°C):", self.det_temp_vr_gfx)
        self.det_temp_vr_soc = QSpinBox()
        self.det_temp_vr_soc.setRange(0, 200)
        self.det_temp_vr_soc.setValue(vb.temp_vr_soc if vb.temp_vr_soc else 115)
        self.det_temp_vr_soc.setSuffix(" °C")
        msg_form.addRow(f"Temp VR SOC (orig: {vb.temp_vr_soc or '—'}°C):", self.det_temp_vr_soc)
        self.msglimits_apply_btn = QPushButton("Apply")
        self.msglimits_apply_btn.clicked.connect(self._on_apply_msglimits)
        msg_form.addRow("", self.msglimits_apply_btn)
        scroll_layout.addWidget(msg_grp)

        # (3) OD Table: GfxclkFoffset, Ppt%, Tdc%, UCLK min/max, FCLK min/max
        od_grp = QGroupBox("OD Table")
        od_form = QFormLayout(od_grp)
        self.det_gfx_offset = QSpinBox()
        self.det_gfx_offset.setRange(0, 2000)
        self.det_gfx_offset.setValue(200)
        self.det_gfx_offset.setSuffix(" MHz")
        self._od_label_gfx = QLabel("GfxclkFoffset (orig: —):")
        od_form.addRow(self._od_label_gfx, self.det_gfx_offset)
        self.det_od_ppt = QSpinBox()
        self.det_od_ppt.setRange(-50, 100)
        self.det_od_ppt.setValue(10)
        self.det_od_ppt.setSuffix("%")
        self._od_label_ppt = QLabel("Ppt % (orig: —):")
        od_form.addRow(self._od_label_ppt, self.det_od_ppt)
        self.det_od_tdc = QSpinBox()
        self.det_od_tdc.setRange(-50, 100)
        self.det_od_tdc.setValue(0)
        self.det_od_tdc.setSuffix("%")
        self._od_label_tdc = QLabel("Tdc % (orig: —):")
        od_form.addRow(self._od_label_tdc, self.det_od_tdc)
        self.det_uclk_min = QSpinBox()
        self.det_uclk_min.setRange(0, 3000)
        self.det_uclk_min.setValue(0)
        self.det_uclk_min.setSpecialValueText("no change")
        self.det_uclk_min.setSuffix(" MHz")
        self._od_label_uclk_min = QLabel("UCLK min (orig: —):")
        od_form.addRow(self._od_label_uclk_min, self.det_uclk_min)
        self.det_uclk_max = QSpinBox()
        self.det_uclk_max.setRange(0, 3000)
        self.det_uclk_max.setValue(0)
        self.det_uclk_max.setSpecialValueText("no change")
        self.det_uclk_max.setSuffix(" MHz")
        self._od_label_uclk_max = QLabel("UCLK max (orig: —):")
        od_form.addRow(self._od_label_uclk_max, self.det_uclk_max)
        self.det_fclk_min = QSpinBox()
        self.det_fclk_min.setRange(0, 3000)
        self.det_fclk_min.setValue(0)
        self.det_fclk_min.setSpecialValueText("no change")
        self.det_fclk_min.setSuffix(" MHz")
        self._od_label_fclk_min = QLabel("FCLK min (orig: —):")
        od_form.addRow(self._od_label_fclk_min, self.det_fclk_min)
        self.det_fclk_max = QSpinBox()
        self.det_fclk_max.setRange(0, 3000)
        self.det_fclk_max.setValue(0)
        self.det_fclk_max.setSpecialValueText("no change")
        self.det_fclk_max.setSuffix(" MHz")
        self._od_label_fclk_max = QLabel("FCLK max (orig: —):")
        od_form.addRow(self._od_label_fclk_max, self.det_fclk_max)
        self.od_apply_btn = QPushButton("Apply")
        self.od_apply_btn.clicked.connect(self._on_apply_od)
        od_form.addRow("", self.od_apply_btn)
        scroll_layout.addWidget(od_grp)

        # (4) SMU Features: lock features toggle, min-clock floor
        smu_grp = QGroupBox("SMU Features")
        smu_form = QFormLayout(smu_grp)
        self.det_lock_features = QCheckBox()
        self.det_lock_features.setChecked(False)
        smu_form.addRow("Lock features (DS_GFXCLK/GFX_ULV/GFXOFF, orig: Off):", self.det_lock_features)
        self.det_min_clock = QSpinBox()
        self.det_min_clock.setRange(0, 5000)
        self.det_min_clock.setValue(0)
        self.det_min_clock.setSpecialValueText("use game clock")
        self.det_min_clock.setSuffix(" MHz")
        smu_form.addRow("Min-clock floor (orig: 0):", self.det_min_clock)
        self.smu_apply_btn = QPushButton("Apply")
        self.smu_apply_btn.clicked.connect(self._on_apply_smu_features)
        smu_form.addRow("", self.smu_apply_btn)
        scroll_layout.addWidget(smu_grp)

        scroll_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

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
        """Run apply_fn(hw) with hardware init/cleanup. Logs errors."""
        if not self._can_apply():
            self._log(f"{action_name}: scan not ready.")
            return
        hw = None
        try:
            hw = init_hardware()
            apply_fn(hw)
        except Exception as e:
            self._log(f"{action_name} failed: {e}")
        finally:
            if hw:
                cleanup_hardware(hw)

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
        return OverclockSettings(
            game_clock=self.det_game_clock.value(),
            boost_clock=self.det_boost_clock.value(),
            power_ac=self.det_power_ac.value(),
            power_dc=self.det_power_dc.value(),
            tdc_gfx=self.det_tdc_gfx.value(),
            tdc_soc=self.det_tdc_soc.value(),
            temp_edge=self.det_temp_edge.value(),
            temp_hotspot=self.det_temp_hotspot.value(),
            temp_mem=self.det_temp_mem.value(),
            temp_vr_gfx=self.det_temp_vr_gfx.value(),
            temp_vr_soc=self.det_temp_vr_soc.value(),
            offset=self.det_gfx_offset.value(),
            od_ppt=self.det_od_ppt.value(),
            od_tdc=self.det_od_tdc.value(),
            uclk_min=self.det_uclk_min.value(),
            uclk_max=self.det_uclk_max.value(),
            fclk_min=self.det_fclk_min.value(),
            fclk_max=self.det_fclk_max.value(),
            min_clock=self.det_min_clock.value(),
            lock_features=self.det_lock_features.isChecked(),
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

    def _on_apply_clocks(self):
        settings = self.get_detailed_settings()
        self._log(f"Clocks Apply: Game={settings._game_clock()} Boost={settings._boost_clock()} MHz")

        def do_apply(hw):
            apply_clocks_only(hw["inpout"], hw["smu"], self.scan_result, settings)
            self._log("Clocks applied.")

        self._run_with_hardware("Clocks Apply", do_apply)

    def _on_apply_msglimits(self):
        settings = self.get_detailed_settings()
        self._log(f"MsgLimits Apply: PPT={settings._power_ac()}W, TDC={settings._tdc_gfx()}A")

        def do_apply(hw):
            apply_msglimits_only(
                hw["inpout"], hw["smu"], self.scan_result, settings, ScanOptions(),
                vbios_values=self.vbios_values,
            )
            self._log("MsgLimits applied.")

        self._run_with_hardware("MsgLimits Apply", do_apply)

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
        """If bios/vbios.rom exists, parse and show main UI."""
        if not os.path.isfile(DEFAULT_VBIOS_PATH):
            self.stacked.setCurrentWidget(self.gate)
            return

        diag: list[str] = []
        vals = parse_vbios(DEFAULT_VBIOS_PATH, diagnostic_out=diag)
        used_defaults = vals is None
        if used_defaults:
            vals = parse_vbios_or_defaults(DEFAULT_VBIOS_PATH)

        self._show_main_ui(vals, used_defaults=used_defaults, diagnostic_lines=diag if used_defaults else None)

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
