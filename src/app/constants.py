"""
Adrenalift -- Shared Constants
==============================

Script directory, version info, display-order constants, VBIOS helpers.
"""

from __future__ import annotations

import json
import os
import sys

# ---------------------------------------------------------------------------
# Script directory -- resolves to exe dir when frozen, project root otherwise
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _script_dir = os.path.dirname(sys.executable)
else:
    _script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# ---------------------------------------------------------------------------
# Version info -- loaded from version.json (bundled in _MEIPASS or project root)
# ---------------------------------------------------------------------------

_VERSION_CANDIDATES = [
    os.path.join(getattr(sys, "_MEIPASS", ""), "version.json"),
    os.path.join(_script_dir, "version.json"),
]
APP_VERSION = "?"
APP_BUILD   = "?"
for _vp in _VERSION_CANDIDATES:
    if os.path.isfile(_vp):
        try:
            with open(_vp, "r", encoding="utf-8") as _vf:
                _version_data = json.load(_vf)
            APP_VERSION = str(_version_data.get("version", "?"))
            APP_BUILD   = str(_version_data.get("build", "?"))
        except Exception:
            pass
        break

# ---------------------------------------------------------------------------
# Heavy imports -- placed after lightweight constants so logging_setup can
# import _script_dir without pulling in the engine.
# ---------------------------------------------------------------------------

from src.io.vbios_parser import parse_vbios_from_bytes
from src.io.vbios_storage import read_vbios_decoded

from src.engine.smu_metrics import (
    PPCLK_NAMES, SVI_PLANE_NAMES, TEMP_NAMES, THROTTLER_COUNT,
    THROTTLER_NAMES, D3HOT_SEQUENCE_NAMES,
)

# ---------------------------------------------------------------------------
# Metrics display order (grouped sections for the Tables sub-tab)
# ---------------------------------------------------------------------------

_METRICS_DISPLAY_SECTIONS = [
    ("Current Clocks (MHz)", [f"CurrClock_{n}" for n in PPCLK_NAMES]),
    ("Power", [
        "AverageSocketPower", "AverageTotalBoardPower", "dGPU_W_MAX",
        "EnergyAccumulator",
    ]),
    ("Voltage (mV)", [f"AvgVoltage_{n}" for n in SVI_PLANE_NAMES]),
    ("Current (mA)", [f"AvgCurrent_{n}" for n in SVI_PLANE_NAMES]),
    ("Activity (%)", [
        "AverageGfxActivity", "AverageUclkActivity",
        "AverageVcn0ActivityPercentage", "Vcn1ActivityPercentage",
    ]),
    ("Fan", ["AvgFanPwm", "AvgFanRpm"]),
    ("Temperature", [f"AvgTemperature_{n}" for n in TEMP_NAMES]
     + ["AvgTemperatureFanIntake"]),
    ("PCIe", ["PcieRate", "PcieWidth"]),
    ("Throttling (%)", [f"Throttle_{THROTTLER_NAMES[i]}" for i in range(THROTTLER_COUNT)]
     + ["VmaxThrottlingPercentage"]),
    ("Average Frequencies (MHz)", [
        "AverageGfxclkFrequencyTarget",
        "AverageGfxclkFrequencyPreDs", "AverageGfxclkFrequencyPostDs",
        "AverageFclkFrequencyPreDs", "AverageFclkFrequencyPostDs",
        "AverageMemclkFrequencyPreDs", "AverageMemclkFrequencyPostDs",
        "AverageVclk0Frequency", "AverageDclk0Frequency",
        "AverageVclk1Frequency", "AverageDclk1Frequency",
        "AveragePCIeBusy",
    ]),
    ("Moving Averages", [
        "MovingAverageGfxclkFrequencyTarget",
        "MovingAverageGfxclkFrequencyPreDs", "MovingAverageGfxclkFrequencyPostDs",
        "MovingAverageFclkFrequencyPreDs", "MovingAverageFclkFrequencyPostDs",
        "MovingAverageMemclkFrequencyPreDs", "MovingAverageMemclkFrequencyPostDs",
        "MovingAverageVclk0Frequency", "MovingAverageDclk0Frequency",
        "MovingAverageGfxActivity", "MovingAverageUclkActivity",
        "MovingAverageVcn0ActivityPercentage", "MovingAveragePCIeBusy",
        "MovingAverageUclkActivity_MAX", "MovingAverageSocketPower",
    ]),
    ("D3Hot Counters", (
        [f"D3HotEntry_{n}" for n in D3HOT_SEQUENCE_NAMES]
        + [f"D3HotExit_{n}" for n in D3HOT_SEQUENCE_NAMES]
        + [f"ArmMsgReceived_{n}" for n in D3HOT_SEQUENCE_NAMES]
    )),
    ("Misc", [
        "MetricsCounter",
        "ApuSTAPMSmartShiftLimit", "ApuSTAPMLimit",
        "AvgApuSocketPower", "AverageUclkActivity_MAX",
        "PublicSerialNumberLower", "PublicSerialNumberUpper",
    ]),
]

# ---------------------------------------------------------------------------
# VBIOS helpers
# ---------------------------------------------------------------------------

DEFAULT_VBIOS_PATH = os.path.join(_script_dir, "bios", "vbios.rom")


def _get_vbios_values(path: str = DEFAULT_VBIOS_PATH):
    """Decode on demand: read from disk, decode, parse. Returns VbiosValues or None.
    Never keeps decoded bytes in memory longer than needed for parsing."""
    rom_bytes, _ = read_vbios_decoded(path)
    if rom_bytes is None:
        return None
    return parse_vbios_from_bytes(rom_bytes, rom_path=path)
