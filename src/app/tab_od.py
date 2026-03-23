"""
Adrenalift -- OD (OverDrive) Tab
=================================

OD table with per-field Set buttons and limits from decoded PP.
Returns a QScrollArea to be embedded in the SMU inner-tabs.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.help_texts import OD_HELP_HTML
from src.app.ui_helpers import make_spinbox, add_param_row, make_cheatsheet_button
from src.io.vbios_parser import extract_od_limits_from_decoded
from src.engine.overclock_engine import apply_od_single_field
from src.engine.od_table import (
    PP_OD_FEATURE_GFX_VF_CURVE_BIT,
    PP_OD_FEATURE_GFX_VMAX_BIT,
    PP_OD_FEATURE_SOC_VMAX_BIT,
    PP_OD_FEATURE_PPT_BIT,
    PP_OD_FEATURE_TDC_BIT,
    PP_OD_FEATURE_GFXCLK_BIT,
    PP_OD_FEATURE_UCLK_BIT,
    PP_OD_FEATURE_FCLK_BIT,
    PP_OD_FEATURE_FAN_CURVE_BIT,
    PP_OD_FEATURE_ZERO_FAN_BIT,
    PP_OD_FEATURE_TEMPERATURE_BIT,
    PP_OD_FEATURE_EDC_BIT,
    PP_OD_FEATURE_FULL_CTRL_BIT,
    PP_NUM_OD_VF_CURVE_POINTS,
    NUM_OD_FAN_MAX_POINTS,
)


class ODTab(QWidget):
    """OD tab — OverDrive table with per-field Set buttons.

    Exposes a ``scroll`` attribute (QScrollArea) suitable for embedding
    as a sub-tab of the SMU tab widget.
    """

    def __init__(self, decoded_pp, *, log_fn, run_with_hardware_fn, show_cheatsheet_fn):
        super().__init__()
        self._log = log_fn
        self._run_with_hardware = run_with_hardware_fn
        self._show_cheatsheet = show_cheatsheet_fn

        self.param_widgets: dict[str, object] = {}
        self.param_current_value_widget: dict = {}
        self.param_smu_key: dict[str, str] = {}
        self.param_unit: dict[str, str] = {}
        self.param_od_array_spec: dict[str, tuple] = {}

        self.scroll = self._build_ui(decoded_pp)

    def _build_ui(self, decoded) -> QScrollArea:
        od_limits = extract_od_limits_from_decoded(decoded)

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
            self.param_smu_key[key] = smu_key
            self.param_current_value_widget[key] = info["cv_label"]
            self.param_unit[key] = info["unit_str"]
            self.param_widgets[key] = widget
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
            self, "OD", OD_HELP_HTML, self._show_cheatsheet,
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
        self.od_table_widget = od_table

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
            self.param_od_array_spec[key] = ("VoltageOffsetPerZoneBoundary", i)
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
            self.param_od_array_spec[key_pwm] = ("FanLinearPwmPoints", i)
            spin = make_spinbox(0, 255, 0, "", "no change")
            _add_od_row(od_table, f"Fan PWM pt {i}", key_pwm, "%", None, "od", spin,
                        row_apply_fn=_mk_od_apply_array("FanLinearPwmPoints", i, PP_OD_FEATURE_FAN_CURVE_BIT, spin, f"Fan PWM pt {i}"),
                        feature_bit=PP_OD_FEATURE_FAN_CURVE_BIT, limits_key="FanLinearPwmPoints")
            key_temp = f"FanLinearTemp{i}"
            self.param_od_array_spec[key_temp] = ("FanLinearTempPoints", i)
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
        self.od_refresh_btn.setEnabled(True)
        od_btn_row.addWidget(self.od_refresh_btn)
        self.od_apply_btn = QPushButton("Apply OD")
        self.od_apply_btn.setToolTip("Sends OD table (offset, PPT%, TDC%, UCLK/FCLK) to SMU via table transfer")
        od_btn_row.addWidget(self.od_apply_btn)
        od_top_layout.addLayout(od_btn_row)

        od_scroll = QScrollArea()
        od_scroll.setWidgetResizable(True)
        od_scroll.setWidget(od_w)
        return od_scroll
