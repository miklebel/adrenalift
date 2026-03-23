"""
Adrenalift -- Registry Patch Tab
==================================

Registry patch table with apply/restore.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QHeaderView

from src.app.help_texts import REG_CHEATSHEET_HTML
from src.app.ui_helpers import make_spinbox, make_cheatsheet_button
from src.app.workers import RegistryPatchWorker

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


class RegistryTab(QWidget):
    """Registry Patch tab — table with Name, Current, Custom + apply/restore."""

    def __init__(self, *, log_fn, show_cheatsheet_fn):
        super().__init__()
        self._log = log_fn
        self._show_cheatsheet = show_cheatsheet_fn
        self._reg_worker = None
        self._reg_widgets: dict = {}
        self._reg_patch = None
        self._reg_report = None

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        if RegistryPatch is None:
            msg = QLabel(
                "Registry patch is not available (Windows only). "
                "The winreg module is required for AMD GPU registry anti-clock-gating patches."
            )
            msg.setWordWrap(True)
            msg.setStyleSheet("color: #888; padding: 16px;")
            layout.addWidget(msg)
            return

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
            self._show_cheatsheet, label="",
        )
        layout.addLayout(hint_row)

        self.reg_table = QTableWidget()
        self.reg_table.setColumnCount(3)
        self.reg_table.setHorizontalHeaderLabels(["Name", "Current", "Custom"])
        self.reg_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.reg_table.horizontalHeader().setStretchLastSection(True)

        self._populate_reg_table(self._reg_report)
        layout.addWidget(self.reg_table)

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

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------

    def _make_reg_name_cell(self, display_name: str, original_name: str) -> QWidget:
        cell = QWidget()
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(4)
        label = QLabel(display_name)
        lay.addWidget(label)
        hint_btn = QToolButton()
        hint_btn.setText("?")
        hint_btn.setToolTip(original_name)
        hint_btn.setFixedSize(18, 18)
        hint_btn.setStyleSheet("font-size: 10pt; font-weight: bold;")
        lay.addWidget(hint_btn)
        lay.addStretch()
        return cell

    def _populate_reg_table(self, report: dict):
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

    def _update_reg_table(self, report: dict):
        self._populate_reg_table(report)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_reg_select_recommended(self):
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

    def _on_reg_refresh(self):
        if self._reg_patch is None:
            return
        if self._reg_worker is not None and self._reg_worker.isRunning():
            return
        self._reg_worker = RegistryPatchWorker("read", self._reg_patch.read_current, self)
        self._reg_worker.finished_signal.connect(self._on_reg_worker_finished)
        self._reg_worker.finished.connect(lambda: setattr(self, "_reg_worker", None))
        self._reg_worker.start()
        self.reg_refresh_btn.setEnabled(False)
        self.reg_apply_btn.setEnabled(False)
        self.reg_restore_btn.setEnabled(False)

    def _on_reg_apply(self):
        if self._reg_patch is None:
            return
        if self._reg_worker is not None and self._reg_worker.isRunning():
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
        if self._reg_patch is None:
            return
        if self._reg_worker is not None and self._reg_worker.isRunning():
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
