"""
smu_metrics.py -- SmuMetrics_t ctypes struct for SMU v14.0 (RDNA4 / gfx1200)
=============================================================================

Mirrors the C struct from smu14_driver_if_v14_0.h exactly, including array
sizes and alignment.  Use read_smu_metrics_full() in overclock_engine.py to
populate an instance from the SMU DMA buffer.

Array-size constants (from smu14_driver_if_v14_0.h):
  PPCLK_COUNT          = 11  (GFXCLK..DTBCLK)
  SVI_PLANE_COUNT      = 4   (VDD_GFX, VDD_SOC, VDDCI_MEM, VDDIO_MEM)
  TEMP_COUNT           = 12  (EDGE..PLX)
  THROTTLER_COUNT      = 21
  D3HOT_SEQUENCE_COUNT = 4   (BACO, MSR, BAMACO, ULPS)

PPCLK index -> name mapping (matches PPCLK_e enum order):
  0  GFXCLK
  1  SOCCLK
  2  UCLK
  3  FCLK
  4  DCLK0
  5  VCLK0
  6  DISPCLK
  7  DPPCLK
  8  DPREFCLK
  9  DCFCLK
  10 DTBCLK

SVI_PLANE index -> name:
  0  VDD_GFX
  1  VDD_SOC
  2  VDDCI_MEM
  3  VDDIO_MEM

TEMP index -> name:
  0  Edge
  1  Hotspot
  2  Hotspot_GFX
  3  Hotspot_SOC
  4  Mem
  5  VR_GFX
  6  VR_SOC
  7  VR_Mem0
  8  VR_Mem1
  9  Liquid0
  10 Liquid1
  11 PLX
"""

import ctypes

# ---------------------------------------------------------------------------
# Array sizes (from smu14_driver_if_v14_0.h)
# ---------------------------------------------------------------------------

PPCLK_COUNT          = 11
SVI_PLANE_COUNT      = 4
TEMP_COUNT           = 12
THROTTLER_COUNT      = 21
D3HOT_SEQUENCE_COUNT = 4

# ---------------------------------------------------------------------------
# Human-readable label tables
# ---------------------------------------------------------------------------

PPCLK_NAMES = [
    "GFXCLK", "SOCCLK", "UCLK", "FCLK",
    "DCLK0", "VCLK0", "DISPCLK", "DPPCLK",
    "DPREFCLK", "DCFCLK", "DTBCLK",
]

SVI_PLANE_NAMES = ["VDD_GFX", "VDD_SOC", "VDDCI_MEM", "VDDIO_MEM"]

TEMP_NAMES = [
    "Edge", "Hotspot", "Hotspot_GFX", "Hotspot_SOC",
    "Mem", "VR_GFX", "VR_SOC", "VR_Mem0", "VR_Mem1",
    "Liquid0", "Liquid1", "PLX",
]

D3HOT_SEQUENCE_NAMES = ["BACO", "MSR", "BAMACO", "ULPS"]


# ---------------------------------------------------------------------------
# ctypes struct -- mirrors SmuMetrics_t from smu14_driver_if_v14_0.h
# ---------------------------------------------------------------------------

class SmuMetrics_t(ctypes.LittleEndianStructure):
    """SMU metrics table for SMU v14.0 (RDNA4 / gfx1200).

    Field layout follows the C struct exactly.  All multi-element arrays use
    ctypes Array types so that from_buffer_copy() works without padding issues.
    """
    _pack_ = 1
    _fields_ = [
        # uint32_t CurrClock[PPCLK_COUNT]
        ("CurrClock",                          ctypes.c_uint32 * PPCLK_COUNT),

        # Average frequencies (uint16_t each)
        ("AverageGfxclkFrequencyTarget",       ctypes.c_uint16),
        ("AverageGfxclkFrequencyPreDs",        ctypes.c_uint16),
        ("AverageGfxclkFrequencyPostDs",       ctypes.c_uint16),
        ("AverageFclkFrequencyPreDs",          ctypes.c_uint16),
        ("AverageFclkFrequencyPostDs",         ctypes.c_uint16),
        ("AverageMemclkFrequencyPreDs",        ctypes.c_uint16),
        ("AverageMemclkFrequencyPostDs",       ctypes.c_uint16),
        ("AverageVclk0Frequency",              ctypes.c_uint16),
        ("AverageDclk0Frequency",              ctypes.c_uint16),
        ("AverageVclk1Frequency",              ctypes.c_uint16),
        ("AverageDclk1Frequency",              ctypes.c_uint16),
        ("AveragePCIeBusy",                    ctypes.c_uint16),
        ("dGPU_W_MAX",                         ctypes.c_uint16),
        ("padding",                            ctypes.c_uint16),

        # Moving averages (uint16_t each)
        ("MovingAverageGfxclkFrequencyTarget", ctypes.c_uint16),
        ("MovingAverageGfxclkFrequencyPreDs",  ctypes.c_uint16),
        ("MovingAverageGfxclkFrequencyPostDs", ctypes.c_uint16),
        ("MovingAverageFclkFrequencyPreDs",    ctypes.c_uint16),
        ("MovingAverageFclkFrequencyPostDs",   ctypes.c_uint16),
        ("MovingAverageMemclkFrequencyPreDs",  ctypes.c_uint16),
        ("MovingAverageMemclkFrequencyPostDs", ctypes.c_uint16),
        ("MovingAverageVclk0Frequency",        ctypes.c_uint16),
        ("MovingAverageDclk0Frequency",        ctypes.c_uint16),
        ("MovingAverageGfxActivity",           ctypes.c_uint16),
        ("MovingAverageUclkActivity",          ctypes.c_uint16),
        ("MovingAverageVcn0ActivityPercentage",ctypes.c_uint16),
        ("MovingAveragePCIeBusy",              ctypes.c_uint16),
        ("MovingAverageUclkActivity_MAX",      ctypes.c_uint16),
        ("MovingAverageSocketPower",           ctypes.c_uint16),
        ("MovingAveragePadding",               ctypes.c_uint16),

        # uint32_t MetricsCounter
        ("MetricsCounter",                     ctypes.c_uint32),

        # uint16_t AvgVoltage[SVI_PLANE_COUNT], uint16_t AvgCurrent[SVI_PLANE_COUNT]
        ("AvgVoltage",                         ctypes.c_uint16 * SVI_PLANE_COUNT),
        ("AvgCurrent",                         ctypes.c_uint16 * SVI_PLANE_COUNT),

        # Activity / utilisation
        ("AverageGfxActivity",                 ctypes.c_uint16),
        ("AverageUclkActivity",                ctypes.c_uint16),
        ("AverageVcn0ActivityPercentage",      ctypes.c_uint16),
        ("Vcn1ActivityPercentage",             ctypes.c_uint16),

        # Power
        ("EnergyAccumulator",                  ctypes.c_uint32),
        ("AverageSocketPower",                 ctypes.c_uint16),
        ("AverageTotalBoardPower",             ctypes.c_uint16),

        # uint16_t AvgTemperature[TEMP_COUNT]
        ("AvgTemperature",                     ctypes.c_uint16 * TEMP_COUNT),
        ("AvgTemperatureFanIntake",            ctypes.c_uint16),

        # PCIe
        ("PcieRate",                           ctypes.c_uint8),
        ("PcieWidth",                          ctypes.c_uint8),

        # Fan
        ("AvgFanPwm",                          ctypes.c_uint8),
        ("Padding_fan",                        ctypes.c_uint8),
        ("AvgFanRpm",                          ctypes.c_uint16),

        # uint8_t ThrottlingPercentage[THROTTLER_COUNT]
        ("ThrottlingPercentage",               ctypes.c_uint8 * THROTTLER_COUNT),
        ("VmaxThrottlingPercentage",           ctypes.c_uint8),
        ("padding1",                           ctypes.c_uint8 * 2),

        # D3Hot entry/exit counters
        ("D3HotEntryCountPerMode",             ctypes.c_uint32 * D3HOT_SEQUENCE_COUNT),
        ("D3HotExitCountPerMode",              ctypes.c_uint32 * D3HOT_SEQUENCE_COUNT),
        ("ArmMsgReceivedCountPerMode",         ctypes.c_uint32 * D3HOT_SEQUENCE_COUNT),

        # APU / STAPM
        ("ApuSTAPMSmartShiftLimit",            ctypes.c_uint16),
        ("ApuSTAPMLimit",                      ctypes.c_uint16),
        ("AvgApuSocketPower",                  ctypes.c_uint16),
        ("AverageUclkActivity_MAX",            ctypes.c_uint16),

        # Serial number
        ("PublicSerialNumberLower",            ctypes.c_uint32),
        ("PublicSerialNumberUpper",            ctypes.c_uint32),
    ]


# Convenience: size in bytes (used to guard the DMA read)
SMU_METRICS_SIZE = ctypes.sizeof(SmuMetrics_t)


def parse_metrics(raw: bytes) -> SmuMetrics_t:
    """Parse raw bytes into a SmuMetrics_t.

    Raises ValueError if the buffer is too short.
    """
    if len(raw) < SMU_METRICS_SIZE:
        raise ValueError(
            f"Buffer too short: got {len(raw)}, need {SMU_METRICS_SIZE}"
        )
    return SmuMetrics_t.from_buffer_copy(raw[:SMU_METRICS_SIZE])


def metrics_to_dict(m: SmuMetrics_t) -> dict:
    """Flatten a SmuMetrics_t into a plain dict of scalar values.

    Array fields are expanded with per-element suffixes so callers can look
    up values by a single string key.  Example: CurrClock[0] -> 'CurrClock_GFXCLK'.
    """
    d = {}

    for i, name in enumerate(PPCLK_NAMES):
        d[f"CurrClock_{name}"] = m.CurrClock[i]

    d["AverageGfxclkFrequencyTarget"]        = m.AverageGfxclkFrequencyTarget
    d["AverageGfxclkFrequencyPreDs"]         = m.AverageGfxclkFrequencyPreDs
    d["AverageGfxclkFrequencyPostDs"]        = m.AverageGfxclkFrequencyPostDs
    d["AverageFclkFrequencyPreDs"]           = m.AverageFclkFrequencyPreDs
    d["AverageFclkFrequencyPostDs"]          = m.AverageFclkFrequencyPostDs
    d["AverageMemclkFrequencyPreDs"]         = m.AverageMemclkFrequencyPreDs
    d["AverageMemclkFrequencyPostDs"]        = m.AverageMemclkFrequencyPostDs
    d["AverageVclk0Frequency"]               = m.AverageVclk0Frequency
    d["AverageDclk0Frequency"]               = m.AverageDclk0Frequency
    d["AverageVclk1Frequency"]               = m.AverageVclk1Frequency
    d["AverageDclk1Frequency"]               = m.AverageDclk1Frequency
    d["AveragePCIeBusy"]                     = m.AveragePCIeBusy
    d["dGPU_W_MAX"]                          = m.dGPU_W_MAX

    d["MovingAverageGfxclkFrequencyTarget"]  = m.MovingAverageGfxclkFrequencyTarget
    d["MovingAverageGfxclkFrequencyPreDs"]   = m.MovingAverageGfxclkFrequencyPreDs
    d["MovingAverageGfxclkFrequencyPostDs"]  = m.MovingAverageGfxclkFrequencyPostDs
    d["MovingAverageFclkFrequencyPreDs"]     = m.MovingAverageFclkFrequencyPreDs
    d["MovingAverageFclkFrequencyPostDs"]    = m.MovingAverageFclkFrequencyPostDs
    d["MovingAverageMemclkFrequencyPreDs"]   = m.MovingAverageMemclkFrequencyPreDs
    d["MovingAverageMemclkFrequencyPostDs"]  = m.MovingAverageMemclkFrequencyPostDs
    d["MovingAverageVclk0Frequency"]         = m.MovingAverageVclk0Frequency
    d["MovingAverageDclk0Frequency"]         = m.MovingAverageDclk0Frequency
    d["MovingAverageGfxActivity"]            = m.MovingAverageGfxActivity
    d["MovingAverageUclkActivity"]           = m.MovingAverageUclkActivity
    d["MovingAverageVcn0ActivityPercentage"] = m.MovingAverageVcn0ActivityPercentage
    d["MovingAveragePCIeBusy"]               = m.MovingAveragePCIeBusy
    d["MovingAverageUclkActivity_MAX"]       = m.MovingAverageUclkActivity_MAX
    d["MovingAverageSocketPower"]            = m.MovingAverageSocketPower

    d["MetricsCounter"]                      = m.MetricsCounter

    for i, name in enumerate(SVI_PLANE_NAMES):
        d[f"AvgVoltage_{name}"] = m.AvgVoltage[i]
        d[f"AvgCurrent_{name}"] = m.AvgCurrent[i]

    d["AverageGfxActivity"]                  = m.AverageGfxActivity
    d["AverageUclkActivity"]                 = m.AverageUclkActivity
    d["AverageVcn0ActivityPercentage"]       = m.AverageVcn0ActivityPercentage
    d["Vcn1ActivityPercentage"]              = m.Vcn1ActivityPercentage

    d["EnergyAccumulator"]                   = m.EnergyAccumulator
    d["AverageSocketPower"]                  = m.AverageSocketPower
    d["AverageTotalBoardPower"]              = m.AverageTotalBoardPower

    for i, name in enumerate(TEMP_NAMES):
        d[f"AvgTemperature_{name}"] = m.AvgTemperature[i]
    d["AvgTemperatureFanIntake"]             = m.AvgTemperatureFanIntake

    d["PcieRate"]                            = m.PcieRate
    d["PcieWidth"]                           = m.PcieWidth
    d["AvgFanPwm"]                           = m.AvgFanPwm
    d["AvgFanRpm"]                           = m.AvgFanRpm

    for i in range(THROTTLER_COUNT):
        d[f"ThrottlingPercentage_{i}"]       = m.ThrottlingPercentage[i]
    d["VmaxThrottlingPercentage"]            = m.VmaxThrottlingPercentage

    for i, name in enumerate(D3HOT_SEQUENCE_NAMES):
        d[f"D3HotEntry_{name}"]              = m.D3HotEntryCountPerMode[i]
        d[f"D3HotExit_{name}"]               = m.D3HotExitCountPerMode[i]
        d[f"ArmMsgReceived_{name}"]          = m.ArmMsgReceivedCountPerMode[i]

    d["ApuSTAPMSmartShiftLimit"]             = m.ApuSTAPMSmartShiftLimit
    d["ApuSTAPMLimit"]                       = m.ApuSTAPMLimit
    d["AvgApuSocketPower"]                   = m.AvgApuSocketPower
    d["AverageUclkActivity_MAX"]             = m.AverageUclkActivity_MAX

    d["PublicSerialNumberLower"]             = m.PublicSerialNumberLower
    d["PublicSerialNumberUpper"]             = m.PublicSerialNumberUpper

    return d
