"""
Adrenalift -- Escape OD Tab
=============================

D3DKMTEscape OD8 write interface (no admin required).
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.help_texts import ESCAPE_OD_HELP_HTML
from src.app.ui_helpers import make_spinbox, make_cheatsheet_button
from src.app.workers import EscapeWorker
from src.io.escape_structures import Od8Setting, OD8_RDNA4_FIELD_MAP, OdFail


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


class EscapeTab(QWidget):
    """Escape OD tab — D3DKMTEscape OD8 interface."""

    def __init__(self, *, log_fn, show_cheatsheet_fn):
        super().__init__()
        self._log = log_fn
        self._show_cheatsheet = show_cheatsheet_fn
        self._escape_worker = None
        self._escape_od_widgets: dict = {}
        self._escape_od_current_values: dict = {}

        self._build_ui()

    @staticmethod
    def _escape_spin_params(idx):
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

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)

        _, hint_row = make_cheatsheet_button(
            self, "Escape OD", ESCAPE_OD_HELP_HTML, self._show_cheatsheet,
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

        for group_name, indices in _ESCAPE_OD_GROUPS:
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

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_escape_read(self):
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
