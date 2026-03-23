"""
Adrenalift -- Memory Tab
=========================

PPTable copy viewer with address table and refresh.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import _get_vbios_values
from src.app.workers import MemoryRefreshWorker


class MemoryTab(QWidget):
    """Memory tab — PPTable copies at scanned addresses."""

    def __init__(self, vbios_values, *, log_fn, get_scan_result_fn):
        super().__init__()
        self._log = log_fn
        self._get_scan_result = get_scan_result_fn
        self.vbios_values = vbios_values
        self._memory_worker = None

        self._build_ui()
        self.update_placeholder("Scanning...")

    def _build_ui(self):
        layout = QVBoxLayout(self)

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

    # ------------------------------------------------------------------
    # Public API for orchestrator
    # ------------------------------------------------------------------

    def update_placeholder(self, text: str):
        """Show placeholder text when no table data."""
        self.memory_table.setRowCount(0)
        self.memory_banner.setText(text)
        self.memory_banner.setVisible(bool(text))

    def set_refresh_enabled(self, enabled: bool):
        self.memory_refresh_btn.setEnabled(enabled)

    def start_refresh_if_ready(self):
        """Do initial memory read after scan completes."""
        scan_result = self._get_scan_result()
        if scan_result and getattr(scan_result, "valid_addrs", None):
            self.memory_refresh_btn.setEnabled(True)
            self._on_memory_refresh_click()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_memory_refresh_click(self):
        """Manual refresh: read PPTable data from all scanned addresses."""
        if self._memory_worker is not None and self._memory_worker.isRunning():
            return
        scan_result = self._get_scan_result()
        if scan_result is None:
            self.update_placeholder("Scanning...")
            return
        addrs = getattr(scan_result, "valid_addrs", []) or []
        if not addrs:
            self.update_placeholder("No addresses")
            return
        self.memory_refresh_btn.setEnabled(False)
        self._memory_worker = MemoryRefreshWorker(addrs, self)
        self._memory_worker.results_signal.connect(self._on_memory_refresh_results)
        self._memory_worker.finished.connect(self._enable_memory_refresh)
        self._memory_worker.start()

    def _enable_memory_refresh(self):
        self._memory_worker = None
        self.memory_refresh_btn.setEnabled(True)

    def _on_memory_refresh_results(self, results: list):
        """Update memory table from worker results."""
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
