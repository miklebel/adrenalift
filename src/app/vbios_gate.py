"""
Adrenalift -- VBIOS Gate Screen
================================

Initial screen shown when no VBIOS ROM is present.
Offers a file picker and copies the selected ROM to ``bios/vbios.rom``.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import _script_dir
from src.io.vbios_parser import parse_vbios_from_bytes
from src.io.vbios_storage import write_vbios_encoded


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

        vals = parse_vbios_from_bytes(rom_bytes, rom_path=dest_path)
        if vals is None:
            self.status_label.setText("Failed to parse VBIOS. No valid PPTable structure found.")
            self.status_label.setStyleSheet("color: #c00;")
            return

        self.status_label.setText("")
        if self._on_vbios_ready:
            self._on_vbios_ready(vals)
