"""
Adrenalift -- PySide6 Main Window
==========================================

Slim entry point: MainWindow with stacked widget (gate / main UI),
multiprocessing freeze_support, and main() function.
"""

from __future__ import annotations

# CRITICAL: On Windows + PyInstaller, multiprocessing child processes re-execute
# this entry-point script.  Call freeze_support() BEFORE any module-level side
# effects (logging, atexit, Qt) so child workers exit immediately instead of
# spawning duplicate GUI instances (fork bomb).
import multiprocessing as _mp
if __name__ == "__main__":
    _mp.freeze_support()

import os
import sys

from src.app.constants import _script_dir, DEFAULT_VBIOS_PATH

# logging_setup installs exception hooks and the file logger on import
from src.app.logging_setup import _log_to_file, _log_exception_to_file, set_atexit_clean

from src.io.vbios_parser import VbiosValues, parse_vbios_from_bytes, parse_vbios_or_defaults
from src.io.vbios_storage import read_vbios_decoded, write_vbios_encoded
from src.io.mmio import ensure_driver_files_copied

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QStackedWidget,
)

from src.app.vbios_gate import VbiosGateWidget
from src.app.main_widget import MainOverclockWidget


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Adrenalift")
        self.setMinimumSize(520, 480)
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w = min(1000, geom.width())
            h = min(1000, geom.height())
            self.resize(w, h)
        else:
            self.resize(1000, 1000)

        self.stacked = QStackedWidget()
        self.setCentralWidget(self.stacked)

        self.gate = VbiosGateWidget()
        self.gate.set_on_vbios_ready(self._on_vbios_ready)
        self.stacked.addWidget(self.gate)

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

    def _show_main_ui(self, vbios_values: VbiosValues, *, used_defaults: bool = False,
                      diagnostic_lines: list[str] | None = None):
        if self.stacked.count() < 2:
            main_ui = MainOverclockWidget(
                vbios_values, used_defaults=used_defaults,
                diagnostic_lines=diagnostic_lines,
            )
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
    _log_to_file("main(): starting application")
    try:
        if getattr(sys, "frozen", False):
            ensure_driver_files_copied()
        app = QApplication(sys.argv)
        app.setApplicationName("Adrenalift")
        win = MainWindow()
        win.show()
        _log_to_file("main(): window shown, entering event loop")
        ret = app.exec()
        _log_to_file(f"main(): event loop exited with code {ret}")
        set_atexit_clean()
        return ret
    except SystemExit:
        set_atexit_clean()
        raise
    except Exception:
        _log_exception_to_file("main()")
        raise


if __name__ == "__main__":
    sys.exit(main())
