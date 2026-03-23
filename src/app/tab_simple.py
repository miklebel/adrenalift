"""
Adrenalift -- Simple Settings Tab
==================================

Clock spinbox, startup automation checkboxes.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.app.settings import settings
from src.app.startup_task import (
    is_startup_enabled,
    enable_startup,
    disable_startup,
)
from src.app.help_texts import SIMPLE_HOW_IT_WORKS_HTML
from src.app.ui_helpers import make_spinbox, make_cheatsheet_button
from src.engine.overclock_engine import OverclockSettings


class SimpleTab(QWidget):
    """Simple Settings tab — clock spinbox + startup automation."""

    def __init__(self, vbios_values, *, log_fn, run_with_hardware_fn, show_cheatsheet_fn):
        super().__init__()
        self._log = log_fn
        self._run_with_hardware = run_with_hardware_fn
        self._show_cheatsheet = show_cheatsheet_fn
        self.vbios_values = vbios_values

        outer = QVBoxLayout(self)

        _, hint_row = make_cheatsheet_button(
            self, "How It Works", SIMPLE_HOW_IT_WORKS_HTML,
            self._show_cheatsheet,
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

    # ------------------------------------------------------------------

    def get_settings(self) -> OverclockSettings:
        """Return OverclockSettings from Simple tab (clock only, no offset)."""
        return OverclockSettings(
            clock=self.clock_spin.value(),
            offset=0,
            od_ppt=0,
            od_tdc=0,
        )

    def set_apply_enabled(self, enabled: bool):
        self.simple_apply_btn.setEnabled(enabled)

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
