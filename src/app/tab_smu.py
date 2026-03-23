"""
Adrenalift -- SMU Tab
======================

7 sub-tabs: Status, Clock Limits, Controls, Throttlers, Features, Metrics, Tables+PFE.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import _METRICS_DISPLAY_SECTIONS
from src.app.help_texts import (
    STATUS_CHEATSHEET, CLOCK_CHEATSHEET, CONTROLS_CHEATSHEET,
    FEATURES_CHEATSHEET, THROTTLERS_CHEATSHEET, METRICS_CHEATSHEET,
    TABLES_CHEATSHEET,
)
from src.app.ui_helpers import (
    make_spinbox, make_set_button, add_param_row,
    make_current_value_label, make_cheatsheet_button,
)
from src.app.workers import (
    MetricsRefreshWorker, SmuTableReadWorker, PfeWorker,
)
from src.engine.smu import (
    PPSMC, PPCLK, SMU_FEATURE,
    _CLK_NAMES, _FEATURE_NAMES, _FEATURE_NAMES_LOW,
)
from src.engine.smu_metrics import THROTTLER_COUNT, THROTTLER_NAMES
from src.engine.od_table import TABLE_PPTABLE, TABLE_DRIVER_INFO, TABLE_ECCINFO
from src.app.logging_setup import _log_to_file


class SMUTab(QWidget):
    """SMU tab with nested sub-tabs: Status, Clock Limits, Controls,
    Throttlers, Features, Metrics, Tables+PFE, and OD (injected scroll).
    """

    feature_result_signal = Signal(int, bool, bool, str)
    allowed_mask_signal = Signal(object)

    def __init__(self, *, log_fn, run_with_hardware_fn, show_cheatsheet_fn, od_scroll):
        super().__init__()
        self._log = log_fn
        self._run_with_hardware = run_with_hardware_fn
        self._show_cheatsheet = show_cheatsheet_fn

        self.param_widgets: dict[str, object] = {}
        self.param_current_value_widget: dict[str, QLabel] = {}
        self.param_smu_key: dict[str, str] = {}
        self.param_unit: dict[str, str] = {}
        self.detailed_tables: dict[str, QTableWidget] = {}

        self._smu_status_labels: dict = {}
        self._smu_feature_state_labels: dict = {}
        self._smu_feature_result_labels: dict = {}
        self._smu_feature_control_labels: dict = {}
        self._allowed_mask_probed = False
        self._clock_limits_cv: dict = {}
        self._throttler_checkboxes: dict[int, QCheckBox] = {}
        self._smu_refresh_buttons: list[QPushButton] = []

        self.feature_result_signal.connect(self._on_feature_result, Qt.ConnectionType.QueuedConnection)
        self.allowed_mask_signal.connect(self._on_allowed_mask_result, Qt.ConnectionType.QueuedConnection)

        self._build_ui(od_scroll)

    # ==================================================================
    # Public API for orchestrator
    # ==================================================================

    def update_status_labels(self, smu_state):
        if not smu_state:
            return
        for smu_key, (label, unit) in self._smu_status_labels.items():
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

    def update_feature_checkboxes(self, smu_state):
        if not smu_state:
            return
        w = self.param_widgets
        for bit in range(64):
            enabled = smu_state.get(f"smu_feature_{bit}")
            if enabled is None:
                continue
            cb = w.get(f"SMU_FEAT_{bit}")
            if cb is not None and hasattr(cb, "setChecked"):
                cb.setChecked(bool(enabled))
            label = self._smu_feature_state_labels.get(bit)
            if label is not None:
                label.setText("ON" if enabled else "OFF")

    def update_smu_widgets_from_state(self, smu_state):
        if not smu_state:
            return
        w = self.param_widgets

        ppt_widget = w.get("SMU_PptLimit")
        ppt_val = smu_state.get("smu_ppt")
        if ppt_widget is not None and hasattr(ppt_widget, "setValue") and ppt_val is not None:
            ppt_widget.setValue(int(ppt_val))

        for clk_name in _CLK_NAMES.values():
            for limit_type, smu_suffix in [("SoftMin", "min"), ("SoftMax", "max"),
                                           ("HardMin", "min"), ("HardMax", "max")]:
                wkey = f"SMU_{clk_name}_{limit_type}"
                spin = w.get(wkey)
                freq_val = smu_state.get(f"smu_freq_{clk_name}_{smu_suffix}")
                if spin is not None and hasattr(spin, "setValue") and freq_val is not None:
                    spin.setValue(int(freq_val))

        for key, (label, smu_key) in self._clock_limits_cv.items():
            val = smu_state.get(smu_key)
            if val is not None:
                unit = " W" if "ppt" in smu_key else " MHz"
                label.setText(f"{val}{unit}")
            else:
                label.setText("—")

    def set_refresh_enabled(self, enabled: bool):
        for btn in self._smu_refresh_buttons:
            btn.setEnabled(enabled)

    # ==================================================================
    # Internal: feature result handlers (connected to own signals)
    # ==================================================================

    def _on_feature_result(self, bit: int, verified_ok: bool, actual_enabled: bool, message: str):
        label = self._smu_feature_result_labels.get(bit)
        if label is not None:
            if verified_ok:
                label.setText("VERIFIED")
                label.setStyleSheet("color: #2ecc71; font-weight: bold;")
            else:
                label.setText("FAILED")
                label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            label.setToolTip(message)
        sl = self._smu_feature_state_labels.get(bit)
        if sl is not None:
            sl.setText("ON" if actual_enabled else "OFF")
        cb = self.param_widgets.get(f"SMU_FEAT_{bit}")
        if cb is not None:
            cb.setChecked(actual_enabled)

    def _on_allowed_mask_result(self, result: dict):
        ctrl_labels = self._smu_feature_control_labels
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

    # ==================================================================
    # Build UI
    # ==================================================================

    def _build_ui(self, od_scroll):
        from src.engine.smu import _CLK_NAMES as _ALL_CLK_NAMES

        outer_layout = QVBoxLayout(self)
        self._smu_inner_tabs = QTabWidget()
        outer_layout.addWidget(self._smu_inner_tabs)

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
            self.param_current_value_widget[key] = info["cv_label"]
            self.param_unit[key] = info["unit_str"]
            self.param_widgets[key] = widget
            if smu_key:
                self.param_smu_key[key] = smu_key

        def _add_refresh_btn(layout_target):
            btn = QPushButton("Refresh")
            btn.setToolTip("Read all SMU state: DPM freq ranges, PPT, voltage, features")
            self._smu_refresh_buttons.append(btn)
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addStretch()
            layout_target.addLayout(row)
            return btn

        def _add_cheatsheet_btn(layout_target, tab_title, html_content):
            _, row = make_cheatsheet_button(
                self, tab_title, html_content, self._show_cheatsheet,
            )
            layout_target.addLayout(row)

        # ==============================================================
        # Sub-tab 1: Status
        # ==============================================================
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
        self.detailed_tables["SMU_Status"] = status_tbl

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

        # ==============================================================
        # Sub-tab 2: Clock Limits
        # ==============================================================
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

        clock_tbl = QTableWidget()
        clock_tbl.setColumnCount(1 + len(_LIMIT_TYPES))
        clock_tbl.setHorizontalHeaderLabels(["Clock"] + _LIMIT_TYPES)
        clock_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for ci in range(1, 1 + len(_LIMIT_TYPES)):
            clock_tbl.horizontalHeader().setSectionResizeMode(ci, QHeaderView.ResizeMode.Stretch)
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
                set_btn = make_set_button(f"{clk_name} {lt}", _fn, self._run_with_hardware, max_width=40)
                input_row.addWidget(set_btn)
                cell_lay.addLayout(input_row)

                clock_tbl.setCellWidget(row, col_idx, cell)
                self._clock_limits_cv[key] = (cv_label, smu_key)
                self.param_widgets[key] = spin

        clock_tbl.resizeRowsToContents()
        clock_lay.addWidget(clock_tbl)

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

        ppt_set_btn = make_set_button("PPT Limit", _mk_ppt_apply(smu_ppt_spin), self._run_with_hardware)
        ppt_lay.addWidget(ppt_set_btn)
        ppt_lay.addStretch()
        clock_lay.addWidget(ppt_box)

        self._clock_limits_cv["SMU_PptLimit"] = (ppt_cv_label, "smu_ppt")
        self.param_widgets["SMU_PptLimit"] = smu_ppt_spin

        _add_refresh_btn(clock_lay)
        clock_scroll = QScrollArea()
        clock_scroll.setWidgetResizable(True)
        clock_scroll.setWidget(clock_w)
        self._smu_inner_tabs.addTab(clock_scroll, "Clock Limits")

        # ==============================================================
        # Sub-tab 3: Controls
        # ==============================================================
        ctrl_w = QWidget()
        ctrl_lay = QVBoxLayout(ctrl_w)
        _add_cheatsheet_btn(ctrl_lay, "Controls", CONTROLS_CHEATSHEET)
        ctrl_tbl = _make_smu_table(with_set_col=True)
        self.detailed_tables["SMU_Controls"] = ctrl_tbl

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

        # ==============================================================
        # Sub-tab 4: Throttlers
        # ==============================================================
        self._build_throttlers_subtab()

        # ==============================================================
        # Sub-tab 5: Features
        # ==============================================================
        self._build_features_subtab()

        # ==============================================================
        # Sub-tab 6: Metrics
        # ==============================================================
        self._build_metrics_subtab()

        # ==============================================================
        # Sub-tab 7: Tables + PFE
        # ==============================================================
        self._build_tables_subtab()

        # OD sub-tab (injected from orchestrator)
        self._smu_inner_tabs.addTab(od_scroll, "OD")

    # ------------------------------------------------------------------
    # Sub-tab 4: Throttlers
    # ------------------------------------------------------------------

    def _build_throttlers_subtab(self):
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
        _, row = make_cheatsheet_button(self, "Throttlers", THROTTLERS_CHEATSHEET, self._show_cheatsheet)
        throt_lay.addLayout(row)

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

        for bit in range(THROTTLER_COUNT):
            r = throt_tbl.rowCount()
            throt_tbl.insertRow(r)
            bit_item = QTableWidgetItem(str(bit))
            bit_item.setFlags(bit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            throt_tbl.setItem(r, 0, bit_item)

            tname = THROTTLER_NAMES[bit] if bit < len(THROTTLER_NAMES) else f"BIT_{bit}"
            name_item = QTableWidgetItem(tname)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if bit in _MEM_BITS or bit == _FIT_BIT:
                name_item.setForeground(Qt.GlobalColor.yellow)
            throt_tbl.setItem(r, 1, name_item)

            cat = _THROTTLER_CATEGORIES.get(bit, "Other")
            cat_label = QLabel(cat)
            color = _CATEGORY_COLORS.get(cat, "#888888")
            cat_label.setStyleSheet(f"color: {color}; font-weight: bold; padding: 2px 6px;")
            throt_tbl.setCellWidget(r, 2, cat_label)

            cb = QCheckBox()
            cb.setChecked(True)
            cb.setToolTip(f"Bit {bit}: enable/disable {tname} throttler")
            throt_tbl.setCellWidget(r, 3, cb)
            self._throttler_checkboxes[bit] = cb

        throt_tbl.resizeRowsToContents()
        throt_lay.addWidget(throt_tbl)

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

        _add_refresh_btn_inline = QPushButton("Refresh")
        _add_refresh_btn_inline.setToolTip("Read all SMU state: DPM freq ranges, PPT, voltage, features")
        self._smu_refresh_buttons.append(_add_refresh_btn_inline)
        rfr = QHBoxLayout()
        rfr.addWidget(_add_refresh_btn_inline)
        rfr.addStretch()
        throt_lay.addLayout(rfr)

        throt_scroll = QScrollArea()
        throt_scroll.setWidgetResizable(True)
        throt_scroll.setWidget(throt_w)
        self._smu_inner_tabs.addTab(throt_scroll, "Throttlers")

    # ------------------------------------------------------------------
    # Sub-tab 5: Features
    # ------------------------------------------------------------------

    def _build_features_subtab(self):
        _DEDICATED_MSG_FEATURES = {
            SMU_FEATURE.GFXOFF:  ("AllowGfxOff", "DisallowGfxOff"),
            SMU_FEATURE.GFX_DCS: ("AllowGfxDcs", "DisallowGfxDcs"),
        }
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

        feat_w = QWidget()
        feat_lay = QVBoxLayout(feat_w)
        _, row = make_cheatsheet_button(self, "Features", FEATURES_CHEATSHEET, self._show_cheatsheet)
        feat_lay.addLayout(row)

        unlock_lay = QHBoxLayout()
        unlock_btn = QPushButton("Unlock All Features")
        unlock_btn.setToolTip(
            "Send SetAllowedFeaturesMask(0xFFFFFFFF, 0xFFFFFFFF) to the SMU.\n"
            "This unlocks all 64 feature bits for enable/disable."
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
                        "  and won't let us override it."
                    )
                return (False, f"Unlock rejected: {err}")
        unlock_btn.clicked.connect(
            lambda: self._run_with_hardware("Unlock All Features", _unlock_all_features, require_scan=False))
        unlock_lay.addWidget(unlock_btn)

        probe_btn = QPushButton("Probe Allowed Mask")
        probe_btn.setToolTip(
            "Send EnableAllSmuFeatures to discover which bits the firmware permits."
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
            lambda: self._run_with_hardware("Probe Allowed Mask", _probe_allowed_mask, require_scan=False))
        unlock_lay.addWidget(probe_btn)
        unlock_lay.addStretch()
        feat_lay.addLayout(unlock_lay)

        feat_tbl = QTableWidget()
        feat_tbl.setColumnCount(7)
        feat_tbl.setHorizontalHeaderLabels(["Bit", "Name", "State", "Toggle", "Set", "Result", "Control"])
        feat_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in range(2, 7):
            feat_tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        feat_tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        feat_tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        feat_tbl.verticalHeader().setVisible(False)

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
                        self._log(f"  \u2713 {msg}")
                        self.feature_result_signal.emit(bit, True, actual, msg)
                    else:
                        actual_word = "enabled" if actual else "disabled"
                        msg = (f"SMU: {fname} (bit {bit}) toggle SILENTLY IGNORED — "
                               f"still {actual_word}  (mask=0x{features_after:016X})")
                        self._log(f"  \u2717 {msg}")
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

            r = feat_tbl.rowCount()
            feat_tbl.insertRow(r)

            bit_item = QTableWidgetItem(str(bit))
            bit_item.setFlags(bit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            feat_tbl.setItem(r, 0, bit_item)

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
            feat_tbl.setItem(r, 1, name_item)

            state_label = QLabel("—")
            state_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            feat_tbl.setCellWidget(r, 2, state_label)
            self._smu_feature_state_labels[bit] = state_label

            cb = QCheckBox()
            cb.setChecked(False)
            if bit == SMU_FEATURE.FW_CTF:
                cb.setEnabled(False)
                cb.setToolTip("Critical thermal fault handler — cannot be disabled")
            elif bit in _DANGEROUS_BITS:
                cb.setToolTip(f"\u26a0 DANGEROUS — Bit {bit}: enable/disable {fname}")
            elif bit in _CAUTION_BITS:
                cb.setToolTip(f"\u26a0 Caution — Bit {bit}: enable/disable {fname}")
            else:
                cb.setToolTip(f"Bit {bit}: enable/disable {fname}")
            feat_tbl.setCellWidget(r, 3, cb)
            self.param_widgets[f"SMU_FEAT_{bit}"] = cb

            _fn = _mk_feature_apply(bit, cb)
            set_btn = make_set_button(fname, _fn, self._run_with_hardware)
            feat_tbl.setCellWidget(r, 4, set_btn)

            result_label = QLabel("")
            result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            feat_tbl.setCellWidget(r, 5, result_label)
            self._smu_feature_result_labels[bit] = result_label

            ctrl_label = QLabel("—")
            ctrl_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if bit in _DEDICATED_MSG_FEATURES:
                enable_name, disable_name = _DEDICATED_MSG_FEATURES[bit]
                ctrl_label.setToolTip(
                    f"Dedicated messages: {enable_name} / {disable_name}\n"
                    f"These bypass the feature mask — use Controls tab to send them."
                )
            feat_tbl.setCellWidget(r, 6, ctrl_label)
            self._smu_feature_control_labels[bit] = ctrl_label

        feat_tbl.resizeRowsToContents()
        feat_lay.addWidget(feat_tbl)
        btn = QPushButton("Refresh")
        btn.setToolTip("Read all SMU state: DPM freq ranges, PPT, voltage, features")
        self._smu_refresh_buttons.append(btn)
        rfr = QHBoxLayout()
        rfr.addWidget(btn)
        rfr.addStretch()
        feat_lay.addLayout(rfr)
        feat_scroll = QScrollArea()
        feat_scroll.setWidgetResizable(True)
        feat_scroll.setWidget(feat_w)
        self._smu_inner_tabs.addTab(feat_scroll, "Features")

    # ------------------------------------------------------------------
    # Sub-tab 6: Metrics
    # ------------------------------------------------------------------

    def _build_metrics_subtab(self):
        metrics_w = QWidget()
        metrics_lay = QVBoxLayout(metrics_w)
        _, row = make_cheatsheet_button(self, "Metrics", METRICS_CHEATSHEET, self._show_cheatsheet)
        metrics_lay.addLayout(row)

        metrics_header = QLabel("Live Metrics (SmuMetrics_t)")
        metrics_header.setStyleSheet("font-weight: bold; font-size: 10pt;")
        metrics_lay.addWidget(metrics_header)

        metrics_ctrl_row = QHBoxLayout()
        self._smu_metrics_refresh_btn = QPushButton("Refresh Now")
        self._smu_metrics_refresh_btn.setToolTip("Read full SmuMetrics_t from SMU DMA buffer")
        self._smu_metrics_refresh_btn.clicked.connect(self._on_smu_metrics_refresh)
        metrics_ctrl_row.addWidget(self._smu_metrics_refresh_btn)

        self._smu_metrics_auto_cb = QCheckBox("Auto-refresh")
        self._smu_metrics_auto_cb.setToolTip("Periodically read metrics from the SMU")
        self._smu_metrics_auto_cb.toggled.connect(self._on_smu_metrics_auto_toggle)
        metrics_ctrl_row.addWidget(self._smu_metrics_auto_cb)

        self._smu_metrics_interval_spin = make_spinbox(1, 30, 2, " s")
        self._smu_metrics_interval_spin.setToolTip("Auto-refresh interval in seconds")
        self._smu_metrics_interval_spin.valueChanged.connect(self._on_smu_metrics_interval_changed)
        metrics_ctrl_row.addWidget(self._smu_metrics_interval_spin)

        self._smu_metrics_status_label = QLabel("")
        self._smu_metrics_status_label.setStyleSheet("color: #888;")
        metrics_ctrl_row.addWidget(self._smu_metrics_status_label)
        metrics_ctrl_row.addStretch()
        metrics_lay.addLayout(metrics_ctrl_row)

        self._smu_metrics_table = QTableWidget()
        self._smu_metrics_table.setColumnCount(2)
        self._smu_metrics_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self._smu_metrics_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._smu_metrics_table.horizontalHeader().setStretchLastSection(True)
        self._smu_metrics_table.verticalHeader().setVisible(False)
        self._smu_metrics_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
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

    def _init_metrics_table_rows(self):
        tbl = self._smu_metrics_table
        tbl.setRowCount(0)
        self._metrics_value_items.clear()
        for section_name, keys in _METRICS_DISPLAY_SECTIONS:
            r = tbl.rowCount()
            tbl.insertRow(r)
            hdr = QTableWidgetItem(section_name)
            hdr.setBackground(Qt.GlobalColor.darkGray)
            hdr.setForeground(Qt.GlobalColor.white)
            font = hdr.font()
            font.setBold(True)
            hdr.setFont(font)
            tbl.setItem(r, 0, hdr)
            spacer = QTableWidgetItem("")
            spacer.setBackground(Qt.GlobalColor.darkGray)
            tbl.setItem(r, 1, spacer)
            tbl.setSpan(r, 0, 1, 2)
            for key in keys:
                r = tbl.rowCount()
                tbl.insertRow(r)
                name_item = QTableWidgetItem("  " + key)
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(r, 0, name_item)
                val_item = QTableWidgetItem("—")
                val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(r, 1, val_item)
                self._metrics_value_items[key] = val_item

    def _populate_metrics_values(self, d: dict):
        for key, item in self._metrics_value_items.items():
            val = d.get(key)
            item.setText(str(val) if val is not None else "—")

    def _on_smu_metrics_refresh(self):
        if self._metrics_worker is not None and self._metrics_worker.isRunning():
            return
        self._smu_metrics_refresh_btn.setEnabled(False)
        self._smu_metrics_status_label.setText("Reading...")
        self._metrics_worker = MetricsRefreshWorker()
        self._metrics_worker.results_signal.connect(self._on_smu_metrics_results)
        self._metrics_worker.finished.connect(self._on_metrics_worker_done)
        self._metrics_worker.start()

    def _on_smu_metrics_auto_toggle(self, checked: bool):
        if checked:
            interval_ms = self._smu_metrics_interval_spin.value() * 1000
            self._metrics_auto_timer.start(interval_ms)
            self._smu_metrics_status_label.setText("Auto-refresh ON")
            self._on_smu_metrics_timer_tick()
        else:
            self._metrics_auto_timer.stop()
            self._smu_metrics_status_label.setText("Auto-refresh OFF")

    def _on_smu_metrics_interval_changed(self, val: int):
        if self._metrics_auto_timer.isActive():
            self._metrics_auto_timer.setInterval(val * 1000)

    def _on_smu_metrics_timer_tick(self):
        if self._metrics_worker is not None and self._metrics_worker.isRunning():
            return
        self._metrics_worker = MetricsRefreshWorker()
        self._metrics_worker.results_signal.connect(self._on_smu_metrics_results)
        self._metrics_worker.finished.connect(self._on_metrics_worker_done)
        self._metrics_worker.start()

    def _on_smu_metrics_results(self, result):
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
    # Sub-tab 7: Tables + PFE
    # ------------------------------------------------------------------

    def _build_tables_subtab(self):
        tables_w = QWidget()
        tables_lay = QVBoxLayout(tables_w)
        _, row = make_cheatsheet_button(self, "Tables", TABLES_CHEATSHEET, self._show_cheatsheet)
        tables_lay.addLayout(row)

        other_header = QLabel("Other SMU Tables (on demand)")
        other_header.setStyleSheet("font-weight: bold; font-size: 10pt;")
        tables_lay.addWidget(other_header)

        other_btn_row = QHBoxLayout()
        self._smu_read_pptable_btn = QPushButton("Read PPTable")
        self._smu_read_pptable_btn.setToolTip("TABLE_PPTABLE (id=0) — raw hex dump")
        self._smu_read_pptable_btn.clicked.connect(
            lambda: self._on_smu_read_other_table("PPTable", TABLE_PPTABLE))
        other_btn_row.addWidget(self._smu_read_pptable_btn)

        self._smu_read_driver_info_btn = QPushButton("Read Driver Info")
        self._smu_read_driver_info_btn.setToolTip("TABLE_DRIVER_INFO (id=10)")
        self._smu_read_driver_info_btn.clicked.connect(
            lambda: self._on_smu_read_other_table("DriverInfo", TABLE_DRIVER_INFO))
        other_btn_row.addWidget(self._smu_read_driver_info_btn)

        self._smu_read_ecc_btn = QPushButton("Read ECC Info")
        self._smu_read_ecc_btn.setToolTip("TABLE_ECCINFO (id=11)")
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
        self._smu_table_hex_view.setPlaceholderText("Click a button above to read a table...")
        tables_lay.addWidget(self._smu_table_hex_view)
        self._smu_table_worker = None

        # PFE Settings
        pfe_header = QLabel("PFE Settings (PPTable Header — FeaturesToRun / DebugOverrides)")
        pfe_header.setStyleSheet("font-weight: bold; font-size: 10pt; margin-top: 12px;")
        tables_lay.addWidget(pfe_header)

        pfe_btn_row = QHBoxLayout()
        self._pfe_read_btn = QPushButton("Read PFE Settings")
        self._pfe_read_btn.clicked.connect(self._on_pfe_read)
        pfe_btn_row.addWidget(self._pfe_read_btn)
        self._pfe_patch_features_btn = QPushButton("Patch FeaturesToRun")
        self._pfe_patch_features_btn.clicked.connect(self._on_pfe_patch_features)
        pfe_btn_row.addWidget(self._pfe_patch_features_btn)
        self._pfe_patch_debug_btn = QPushButton("Patch DebugOverrides")
        self._pfe_patch_debug_btn.clicked.connect(self._on_pfe_patch_debug)
        pfe_btn_row.addWidget(self._pfe_patch_debug_btn)
        self._pfe_check_caps_btn = QPushButton("Check OD Memory Caps")
        self._pfe_check_caps_btn.clicked.connect(self._on_pfe_check_caps)
        pfe_btn_row.addWidget(self._pfe_check_caps_btn)
        pfe_btn_row.addStretch()
        tables_lay.addLayout(pfe_btn_row)

        tools_lbl = QLabel("Tools DRAM Path (msg 0x53 — bypasses Driver path rejection)")
        tools_lbl.setStyleSheet("font-size: 8pt; color: #f93; margin-top: 6px;")
        tables_lay.addWidget(tools_lbl)

        pfe_tools_row = QHBoxLayout()
        self._pfe_patch_features_tools_btn = QPushButton("Patch FeaturesToRun (Tools Path)")
        self._pfe_patch_features_tools_btn.clicked.connect(self._on_pfe_patch_features_tools)
        pfe_tools_row.addWidget(self._pfe_patch_features_tools_btn)
        self._pfe_patch_debug_tools_btn = QPushButton("Patch DebugOverrides (Tools Path)")
        self._pfe_patch_debug_tools_btn.clicked.connect(self._on_pfe_patch_debug_tools)
        pfe_tools_row.addWidget(self._pfe_patch_debug_tools_btn)
        pfe_tools_row.addStretch()
        tables_lay.addLayout(pfe_tools_row)

        self._pfe_result_view = QPlainTextEdit()
        self._pfe_result_view.setReadOnly(True)
        self._pfe_result_view.setStyleSheet(
            "background: #1a1a2a; color: #cdf; padding: 6px; "
            "font-family: Consolas, monospace; font-size: 8pt;")
        self._pfe_result_view.setMaximumHeight(320)
        self._pfe_result_view.setPlaceholderText("Click a button above to read/patch PFE settings...")
        tables_lay.addWidget(self._pfe_result_view)
        self._pfe_worker = None

        tables_scroll = QScrollArea()
        tables_scroll.setWidgetResizable(True)
        tables_scroll.setWidget(tables_w)
        self._smu_inner_tabs.addTab(tables_scroll, "Tables")

    # -- Table read handlers --

    def _on_smu_read_other_table(self, table_name: str, table_id: int):
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
        if isinstance(result, dict) and "error" in result:
            self._smu_table_hex_view.setPlainText(f"{table_name}: Error — {result['error']}")
            self._log(f"Tables: {table_name} failed: {result['error']}")
            return
        raw = result
        lines = [f"{table_name} — {len(raw)} bytes\n"]
        for off in range(0, len(raw), 16):
            chunk = raw[off:off + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            lines.append(f"  {off:04X}: {hex_part:<48s}  {ascii_part}")
        self._smu_table_hex_view.setPlainText("\n".join(lines))
        self._log(f"Tables: {table_name} loaded ({len(raw)} bytes)")

    def _on_smu_table_worker_done(self):
        self._smu_table_worker = None
        self._smu_read_pptable_btn.setEnabled(True)
        self._smu_read_driver_info_btn.setEnabled(True)
        self._smu_read_ecc_btn.setEnabled(True)

    # -- PFE handlers --

    def _pfe_set_buttons_enabled(self, enabled: bool):
        self._pfe_read_btn.setEnabled(enabled)
        self._pfe_patch_features_btn.setEnabled(enabled)
        self._pfe_patch_debug_btn.setEnabled(enabled)
        self._pfe_check_caps_btn.setEnabled(enabled)
        self._pfe_patch_features_tools_btn.setEnabled(enabled)
        self._pfe_patch_debug_tools_btn.setEnabled(enabled)

    def _on_pfe_read(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_result_view.setPlainText("Reading PFE_Settings_t from TABLE_PPTABLE...")
        self._pfe_worker = PfeWorker("read_pfe")
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_features(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_worker = PfeWorker(
            "patch_features",
            extra_bits=[SMU_FEATURE.GFX_EDC, SMU_FEATURE.CLOCK_POWER_DOWN_BYPASS, SMU_FEATURE.EDC_PWRBRK],
        )
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_debug(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        from src.engine.overclock_engine import (
            DEBUG_OVERRIDE_DISABLE_FMAX_VMAX, DEBUG_OVERRIDE_ENABLE_PROFILING_MODE,
        )
        flags = DEBUG_OVERRIDE_DISABLE_FMAX_VMAX | DEBUG_OVERRIDE_ENABLE_PROFILING_MODE
        self._pfe_worker = PfeWorker("patch_debug", flags=flags)
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_check_caps(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_worker = PfeWorker("check_od_caps")
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_features_tools(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        self._pfe_worker = PfeWorker(
            "patch_features_tools",
            extra_bits=[SMU_FEATURE.GFX_EDC, SMU_FEATURE.CLOCK_POWER_DOWN_BYPASS, SMU_FEATURE.EDC_PWRBRK],
        )
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_patch_debug_tools(self):
        if self._pfe_worker is not None and self._pfe_worker.isRunning():
            return
        self._pfe_set_buttons_enabled(False)
        from src.engine.overclock_engine import (
            DEBUG_OVERRIDE_DISABLE_FMAX_VMAX, DEBUG_OVERRIDE_ENABLE_PROFILING_MODE,
        )
        flags = DEBUG_OVERRIDE_DISABLE_FMAX_VMAX | DEBUG_OVERRIDE_ENABLE_PROFILING_MODE
        self._pfe_worker = PfeWorker("patch_debug_tools", flags=flags)
        self._pfe_worker.result_signal.connect(self._on_pfe_result)
        self._pfe_worker.finished.connect(lambda: self._pfe_set_buttons_enabled(True))
        self._pfe_worker.start()

    def _on_pfe_result(self, action: str, result: dict):
        if isinstance(result, dict) and "error" in result:
            self._pfe_result_view.setPlainText(f"PFE [{action}]: Error — {result['error']}")
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
            lines.append(f"  FeaturesToRun before: lo=0x{result.get('old_lo', 0):08X} hi=0x{result.get('old_hi', 0):08X}")
            lines.append(f"  FeaturesToRun after:  lo=0x{result.get('new_lo', 0):08X} hi=0x{result.get('new_hi', 0):08X}")
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
                lines.append("\n  No new features became running — PMFW may ignore FeaturesToRun changes at runtime.")

        elif action == "patch_debug":
            from src.engine.smu import SMU_RESP_OK
            resp = result.get('smu_resp', -1)
            resp_str = "OK" if resp == SMU_RESP_OK else f"0x{resp:02X}"
            lines.append(f"  SMU response: {resp_str}")
            lines.append(f"  DebugOverrides before: 0x{result.get('old_debug_overrides', 0):08X}")
            lines.append(f"  DebugOverrides after:  0x{result.get('new_debug_overrides', 0):08X}")
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
                lines.append(f"\n  Verified DebugOverrides: 0x{after.get('debug_overrides', 0):08X}")

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
                lines.append(f"  {att.get('label', '?'):26s} -> resp={r_str} ret=0x{att.get('ret', 0):08X}")
            lines.append("")
            lines.append(f"  Result: {'SUCCESS' if success else 'FAILED — PMFW rejected all table IDs'}")

            if action == "patch_features_tools":
                lines.append("")
                lines.append(f"  FeaturesToRun before: lo=0x{result.get('old_lo', 0):08X} hi=0x{result.get('old_hi', 0):08X}")
                lines.append(f"  FeaturesToRun after:  lo=0x{result.get('new_lo', 0):08X} hi=0x{result.get('new_hi', 0):08X}")
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
                    lines.append("\n  No new features became running.")
            else:
                lines.append("")
                lines.append(f"  DebugOverrides before: 0x{result.get('old_debug_overrides', 0):08X}")
                lines.append(f"  DebugOverrides after:  0x{result.get('new_debug_overrides', 0):08X}")
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
                    lines.append(f"\n  Verified DebugOverrides: 0x{after.get('debug_overrides', 0):08X}")

        elif action == "check_od_caps":
            uclk = result.get('uclk', {})
            if 'dpm_min' in uclk:
                lines.append(f"  UCLK DPM range: {uclk['dpm_min']} - {uclk['dpm_max']} MHz")
            if 'od_UclkFmin' in uclk:
                lines.append(f"  OD table UCLK:  {uclk['od_UclkFmin']} - {uclk['od_UclkFmax']} MHz")
                lines.append(f"  OD table FCLK:  {uclk['od_FclkFmin']} - {uclk['od_FclkFmax']} MHz")
            od_feat = result.get('od_features', {})
            if 'FeatureCtrlMask' in od_feat:
                mask = od_feat['FeatureCtrlMask']
                lines.append(f"\n  OD FeatureCtrlMask: 0x{mask:08X}")
                lines.append(f"    UCLK OD:    {'YES' if od_feat.get('UCLK_bit') else 'NO'}")
                lines.append(f"    FCLK OD:    {'YES' if od_feat.get('FCLK_bit') else 'NO'}")
            caps = result.get('caps', {})
            if 'raw_at_0x105C' in caps:
                lines.append(f"\n  PPTable @0x105C (BasicMin FeatureCtrlMask): {caps['raw_at_0x105C']}")
                lines.append(f"    UCLK bit in PPTable: {'YES' if caps.get('UCLK_bit_in_pptable') else 'NO'}")
                lines.append(f"    FCLK bit in PPTable: {'YES' if caps.get('FCLK_bit_in_pptable') else 'NO'}")
            lines.append("")
            if od_feat.get('UCLK_bit'):
                lines.append("  UCLK OD is SUPPORTED — memory clock can be adjusted via OD table.")
            else:
                lines.append("  UCLK OD is NOT supported in the current OD FeatureCtrlMask.")
            lines.append(
                "\n  Note: ODCAP bits (AUTO_OC_MEMORY, MEMORY_TIMING_TUNE, MANUAL_AC_TIMING) are\n"
                "  exposed via D3DKMTEscape CN escape headers, not directly in the DMA PPTable.\n"
                "  Use the Escape OD tab to query those capabilities from the Windows driver.")

        self._pfe_result_view.setPlainText("\n".join(lines))
        self._log(f"PFE [{action}]: completed")
