"""
Adrenalift -- Main Overclock Widget (Orchestrator)
====================================================

Assembles all tab modules into a QTabWidget, owns shared state
(scan_result, vbios_values), progress bars, log panel, and
scan / VRAM-scan handlers.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import APP_VERSION, APP_BUILD, DEFAULT_VBIOS_PATH, _get_vbios_values
from src.app.logging_setup import _log_to_file
from src.app.settings import settings
from src.app.startup_task import ensure_startup_points_to_current
from src.app.ui_helpers import make_spinbox

from src.app.tab_simple import SimpleTab
from src.app.tab_pp import PPTab
from src.app.tab_od import ODTab
from src.app.tab_smu import SMUTab
from src.app.tab_memory import MemoryTab
from src.app.tab_registry import RegistryTab
from src.app.tab_escape import EscapeTab

from src.app.workers import (
    ApplyWorker,
    DetailedRefreshWorker,
    ScanThread,
    VramDmaScanWorker,
)
from src.io.vbios_parser import VbiosValues
from src.engine.overclock_engine import (
    OverclockSettings,
    ScanResult,
    apply_clocks_only,
    _apply_pp_field_groups,
    apply_od_table_only,
)


class MainOverclockWidget(QWidget):
    """Main UI with Simple/Detailed tabs, log panel, progress bar, and Apply button."""

    log_request_signal = Signal(str)

    def __init__(self, vbios_values: VbiosValues, *, used_defaults: bool = False,
                 diagnostic_lines: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.log_request_signal.connect(self._log_gui, Qt.ConnectionType.QueuedConnection)
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

        # -- Construct tabs --
        self.tabs = QTabWidget()

        self.simple_tab = SimpleTab(
            vbios_values,
            log_fn=self._log,
            run_with_hardware_fn=self._run_with_hardware,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
        )
        self.simple_tab.simple_apply_btn.clicked.connect(self._on_apply_simple)

        self.pp_tab = PPTab(
            vbios_values,
            log_fn=self._log,
            run_with_hardware_fn=self._run_with_hardware,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
            get_scan_result_fn=lambda: self.scan_result,
        )
        self.pp_tab.pp_refresh_btn.clicked.connect(self._on_detailed_refresh_click)
        self.pp_tab.clocks_apply_btn.clicked.connect(self._on_apply_pp)

        self.od_tab = ODTab(
            self.pp_tab.decoded,
            log_fn=self._log,
            run_with_hardware_fn=self._run_with_hardware,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
        )
        self.od_tab.od_refresh_btn.clicked.connect(self._on_detailed_refresh_click)
        self.od_tab.od_apply_btn.clicked.connect(self._on_apply_od)

        self.smu_tab = SMUTab(
            log_fn=self._log,
            run_with_hardware_fn=self._run_with_hardware,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
            od_scroll=self.od_tab.scroll,
        )

        self.memory_tab = MemoryTab(
            vbios_values,
            log_fn=self._log,
            get_scan_result_fn=lambda: self.scan_result,
        )

        self.registry_tab = RegistryTab(
            log_fn=self._log,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
        )

        self.escape_tab = EscapeTab(
            log_fn=self._log,
            show_cheatsheet_fn=self._show_smu_cheatsheet,
        )

        self.tabs.addTab(self.simple_tab, "Simple Settings")
        self.tabs.addTab(self.pp_tab, "PP")
        self.tabs.addTab(self.smu_tab, "SMU")
        self.tabs.addTab(self.memory_tab, "Memory")
        self.tabs.addTab(self.registry_tab, "Registry Patch")
        self.tabs.addTab(self.escape_tab, "Escape OD")
        layout.addWidget(self.tabs)

        # -- Merge param dicts from tabs --
        self._detailed_param_widgets: dict = {}
        self._param_current_value_widget: dict = {}
        self._param_smu_key: dict = {}
        self._param_unit: dict = {}
        self._param_od_array_spec: dict = {}
        self._detailed_tables: dict = {}

        for tab in (self.pp_tab, self.od_tab, self.smu_tab):
            self._detailed_param_widgets.update(tab.param_widgets)
            self._param_current_value_widget.update(tab.param_current_value_widget)
            self._param_smu_key.update(tab.param_smu_key)
            self._param_unit.update(tab.param_unit)

        self._param_od_array_spec.update(self.od_tab.param_od_array_spec)
        self._detailed_tables.update(self.smu_tab.detailed_tables)

        # -- Progress bar and scan status --
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
        self.vram_status_label = QLabel("Ready — press VRAM Scan to find DMA buffer")
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
        self.log_output.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
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
        self._detailed_worker = None

        ensure_startup_points_to_current()

        if settings.get("defaults.scan_on_startup", False):
            self._auto_apply_pending = bool(
                settings.get("defaults.apply_after_scan_on_startup", False)
            )
            QTimer.singleShot(200, self._on_rescan)

    # ------------------------------------------------------------------
    # Cheatsheet dialog (shared)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        _log_to_file(msg)
        app = QApplication.instance()
        if app and QThread.currentThread() is app.thread():
            self._log_gui(msg)
        else:
            self.log_request_signal.emit(msg)

    def _log_gui(self, msg: str):
        self.log_output.appendPlainText(msg)
        sb = self.log_output.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _on_scan_progress(self, pct: float, msg: str):
        self.progress_bar.setValue(int(pct))
        self.scan_status_label.setText(msg)
        if "Search pattern" in msg or "VBIOS" in msg or "hardcoded" in msg or "DMA" in msg:
            self._log(msg)

    def _on_rescan(self):
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

    def _on_scan_finished(self, result: ScanResult | None):
        self.scan_result = result
        if result is None:
            self._auto_apply_pending = False
            self._log("Scan failed (no result).")
            self.scan_status_label.setText("Scan failed.")
            self._set_apply_buttons_enabled(False)
            self.memory_tab.update_placeholder("No addresses")
            self.progress_bar.setValue(100)
            self.rescan_btn.setEnabled(True)
            return
        if result.error:
            self._auto_apply_pending = False
            self._log(f"Scan failed: {result.error}")
            if getattr(result, "od_table", None) is not None:
                self._update_od_from_scan(result.od_table)
                self.scan_status_label.setText("PPTable not found — OD/SMU apply available")
                self._set_apply_buttons_enabled(True)
                self._start_detailed_refresh_if_ready()
            else:
                self.scan_status_label.setText(f"Scan failed: {result.error}")
                self._set_apply_buttons_enabled(False)
            self.memory_tab.update_placeholder("No addresses")
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
            self.scan_status_label.setText(f"Ready — {len(result.valid_addrs)} PPTable(s) found")
            self.memory_tab.start_refresh_if_ready()
            self._start_detailed_refresh_if_ready()
        else:
            self._log("Scan complete: no valid PPTable addresses found.")
            self._start_detailed_refresh_if_ready()
            self.scan_status_label.setText("No PPTable found — OD/SMU apply available.")
            self.memory_tab.update_placeholder("No addresses")
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
            self.vram_status_label.setText(f"Found DMA buffer at 0x{offset:X}  (method: {method})")
            self._log(f"VRAM scan complete: DMA buffer at offset 0x{offset:X} (method: {method}, VRAM: {vram_mb:.0f} MB)")
        else:
            self.vram_status_label.setText("VRAM scan finished — DMA buffer not found")
            self._log("VRAM scan complete: DMA buffer not found.")

    # ------------------------------------------------------------------
    # Detailed refresh (cross-tab)
    # ------------------------------------------------------------------

    def _update_detailed_live_columns(self, ram_data, od_table, metrics, smu_state=None):
        def _fmt(val, suffix=""):
            if val is not None:
                return f"{val}{suffix}"
            return "—"
        updated = 0
        for key, cv_label in self._param_current_value_widget.items():
            if cv_label is None or not hasattr(cv_label, "setText"):
                continue
            smu_key = self._param_smu_key.get(key)
            if smu_key == "od":
                if od_table:
                    arr_spec = self._param_od_array_spec.get(key)
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
        for key, (attr, idx) in self._param_od_array_spec.items():
            if key in w and hasattr(w[key], "setValue"):
                arr = getattr(od, attr, None)
                if arr is not None and idx < len(arr):
                    w[key].setValue(arr[idx])
        self._update_detailed_live_columns(None, od, None, None)

    def _on_detailed_refresh_click(self):
        if self._detailed_worker is not None and self._detailed_worker.isRunning():
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) if self.scan_result else []
        self._set_detailed_refresh_enabled(False)
        self._detailed_worker = DetailedRefreshWorker(
            addrs,
            pp_ram_offset_map=self.pp_tab.pp_ram_offset_map,
            parent=self,
        )
        self._detailed_worker.log_signal.connect(self._log_gui, Qt.ConnectionType.QueuedConnection)
        self._detailed_worker.results_signal.connect(self._on_detailed_refresh_results)
        self._detailed_worker.finished.connect(lambda: self._enable_detailed_refresh())
        self._detailed_worker.start()

    def _set_detailed_refresh_enabled(self, enabled: bool):
        self.pp_tab.pp_refresh_btn.setEnabled(enabled)
        self.od_tab.od_refresh_btn.setEnabled(enabled)
        self.smu_tab.set_refresh_enabled(enabled)

    def _enable_detailed_refresh(self):
        self._detailed_worker = None
        self._set_detailed_refresh_enabled(True)

    def _on_detailed_refresh_results(self, ram_data, od_table, metrics, smu_state=None):
        _log_to_file(f"_on_detailed_refresh_results: ram={ram_data is not None}, "
                     f"od={od_table is not None}, metrics={metrics is not None}, "
                     f"smu={smu_state is not None and len(smu_state) if smu_state else None}")
        self._update_detailed_live_columns(ram_data, od_table, metrics, smu_state)
        if od_table:
            self._update_od_from_scan(od_table)
            if self.scan_result is None:
                self.scan_result = ScanResult([], [], [], [], False, [], od_table=od_table)
            else:
                self.scan_result.od_table = od_table
            self._set_apply_buttons_enabled(self._can_apply())
        if smu_state:
            self.smu_tab.update_status_labels(smu_state)
            self.smu_tab.update_smu_widgets_from_state(smu_state)
            self.smu_tab.update_feature_checkboxes(smu_state)
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
        if not self.scan_result:
            return
        addrs = getattr(self.scan_result, "valid_addrs", []) or []
        od = getattr(self.scan_result, "od_table", None)
        if addrs or od:
            self._on_detailed_refresh_click()

    # ------------------------------------------------------------------
    # Apply helpers
    # ------------------------------------------------------------------

    def _can_apply(self) -> bool:
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
        self.simple_tab.set_apply_enabled(enabled)
        self.pp_tab.clocks_apply_btn.setEnabled(enabled)
        self.od_tab.od_apply_btn.setEnabled(enabled)

    def _run_with_hardware(self, action_name: str, apply_fn, require_scan=True):
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

    # ------------------------------------------------------------------
    # Apply actions
    # ------------------------------------------------------------------

    def get_simple_settings(self) -> OverclockSettings:
        return self.simple_tab.get_settings()

    def get_detailed_settings(self) -> OverclockSettings:
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

    def _on_apply_simple(self):
        s = self.get_simple_settings()
        self._log(f"Simple Apply: clock={s.clock} MHz")
        def do_apply(hw):
            vb = _get_vbios_values()
            if vb is None:
                vb = self.vbios_values
            inpout, smu = hw["inpout"], hw["smu"]
            if self.scan_result and self.scan_result.valid_addrs:
                results = apply_clocks_only(
                    inpout, smu, self.scan_result, s,
                    vbios_values=vb,
                    progress_callback=lambda pct, msg: self._log(msg),
                )
                self._log(f"Clocks: {results['patched_count']} patched, "
                          f"{results['skipped_count']} skipped.")
                if hw.get("virt") is None:
                    self._log("Note: DMA buffer not available — OD/metrics readback skipped.")
        self._run_with_hardware("Simple Apply", do_apply)

    def _on_apply_pp(self):
        pp_values = self.pp_tab.get_patch_values()
        self._log(f"Apply PP: {len(pp_values)} field(s) to patch (RAM-only)")
        def do_apply(hw):
            log_cb = lambda pct, msg: self._log(msg)
            if self.scan_result and self.scan_result.valid_addrs:
                res = _apply_pp_field_groups(
                    hw["inpout"], self.scan_result, pp_values,
                    self.pp_tab.pp_ram_offset_map, groups=None,
                    progress_callback=log_cb,
                )
                self._log(f"PP: {res['field_writes']} field writes across "
                          f"{res['patched_count']} addrs "
                          f"({res['skipped_count']} skipped)")
            else:
                self._log("PP: no valid addresses to patch.")
        self._run_with_hardware("Apply PP", do_apply)

    def _on_apply_od(self):
        s = self.get_detailed_settings()
        self._log(f"OD Apply: offset={s.offset} MHz, PPT={s.od_ppt}%, TDC={s.od_tdc}%")
        def do_apply(hw):
            if hw.get("virt") is None:
                return (False, "DMA buffer not available — run DRAM Scan first to enable OD writes")
            apply_od_table_only(hw["smu"], hw["virt"], s)
            self._log("OD table applied.")
        self._run_with_hardware("OD Apply", do_apply)
