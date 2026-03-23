"""
AMD WDDM escape interface structures and OD8 command definitions.

Reverse-engineered from amdkmdag.sys (RX 9060 XT, SMU v14.0.2 / RDNA 4).

Three layers of OD control:
  1. CWDDE escape protocol  — ATID-signed buffers via D3DKMTEscape
  2. OD8 settings interface — indexed setting array (per-feature values)
  3. OverDriveTable_t       — PMFW firmware table (TABLE_OVERDRIVE, id=8)

The CN escape cache (PP_CNEscapeInput) is the registry-persisted form of
layer 2.  On driver load, the blob is read and fed through the OD8 apply
pipeline which populates layer 3 and transfers to PMFW.
"""

from __future__ import annotations

import ctypes
import enum
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ── ATID Escape Buffer ──────────────────────────────────────────────────

ATID_SIGNATURE = 0x44495441  # "ATID" LE
CWDD_SIGNATURE = 0x44445743  # "CWDD" LE (legacy, not used on RDNA4)


# ── PP_OD Feature Bits (OverDriveTable_t.FeatureCtrlMask) ───────────────

class PpOdFeature(enum.IntFlag):
    """Bitmask in OverDriveTable_t.FeatureCtrlMask and OverDriveLimits_t.
    Source: smu14_driver_if_v14_0.h
    """
    GFX_VF_CURVE  = 1 << 0
    GFX_VMAX      = 1 << 1
    SOC_VMAX      = 1 << 2
    PPT           = 1 << 3
    FAN_CURVE     = 1 << 4
    FAN_LEGACY    = 1 << 5
    FULL_CTRL     = 1 << 6
    TDC           = 1 << 7
    GFXCLK        = 1 << 8
    UCLK          = 1 << 9
    FCLK          = 1 << 10
    ZERO_FAN      = 1 << 11
    TEMPERATURE   = 1 << 12
    EDC           = 1 << 13


# ── OD8 Setting ID Enumeration ──────────────────────────────────────────

class Od8Setting(enum.IntEnum):
    """OD8 setting indices for 0x00C000A1 escape (RDNA 4 / SMU v14.0.2).

    76 slots (0–75).  Names reflect **RDNA 4 actual semantics**, not legacy
    Vega 20.  Confidence tags in comments:
        [F] Frida-confirmed (Adrenalin capture ground truth)
        [G] Ghidra handler analysis (code references to this index)
        [I] Inferred (OverDriveTable_t structure + Linux kernel cross-ref)

    Legacy Vega 20 names are preserved in LEGACY_OD8_NAMES below.
    """
    # ── GFX V/F curve control (0–4) ─────────────────────────────────────
    # [G] SetGfxCurveVoltage, SetGfxClkLimits, SetGfxCurveFreq
    GFX_CURVE_VF_0           = 0
    GFX_CURVE_VF_1           = 1
    GFX_CURVE_VF_2           = 2
    GFX_CURVE_VF_3           = 3
    GFX_CURVE_VF_4           = 4

    # ── Core OC/power settings ──────────────────────────────────────────
    GFXCLK_FMAX              = 5   # [F] GfxclkFmaxVmax @ 0x54 (MHz)
    PPT                      = 6   # [F] Ppt @ 0x24 (% offset, signed)
    TDC                      = 7   # [G+I] Tdc @ 0x26 (TurnOffFeatures handler)
    UCLK_FMAX                = 8   # [G+I] UclkFmax @ 0x1E (TurnOffFeatures)
    UCLK_FMIN                = 9   # [I] UclkFmin @ 0x1C
    FCLK_FMIN                = 10  # [I] FclkFmin @ 0x20
    FCLK_FMAX                = 11  # [I] FclkFmax @ 0x22

    # ── Fan / thermal controls ──────────────────────────────────────────
    FAN_ZERO_RPM_ENABLE      = 12  # [F] FanZeroRpmEnable @ 0x3C
    OPERATING_TEMP_MAX       = 13  # [I] MaxOpTemp @ 0x3F
    AC_TIMING                = 14  # [G] GetGfxClockBase handler
    FAN_ZERO_RPM_STOP_TEMP   = 15  # [I] FanZeroRpmStopTemp @ 0x3D

    # ── Fan curve points (6 × PWM/temp pairs, 16–27) ───────────────────
    FAN_CURVE_PWM_0          = 16  # [F] FanLinearPwmPoints[0] @ 0x28
    FAN_CURVE_TEMP_0         = 17  # [F] FanLinearTempPoints[0] @ 0x2E
    FAN_CURVE_PWM_1          = 18  # [F] FanLinearPwmPoints[1] @ 0x29
    FAN_CURVE_TEMP_1         = 19  # [F] FanLinearTempPoints[1] @ 0x2F
    FAN_CURVE_PWM_2          = 20  # [F] FanLinearPwmPoints[2] @ 0x2A
    FAN_CURVE_TEMP_2         = 21  # [F] FanLinearTempPoints[2] @ 0x30
    FAN_CURVE_PWM_3          = 22  # [F] FanLinearPwmPoints[3] @ 0x2B
    FAN_CURVE_TEMP_3         = 23  # [F] FanLinearTempPoints[3] @ 0x31
    FAN_CURVE_PWM_4          = 24  # [F] FanLinearPwmPoints[4] @ 0x2C
    FAN_CURVE_TEMP_4         = 25  # [F] FanLinearTempPoints[4] @ 0x32
    FAN_CURVE_PWM_5          = 26  # [G] FanLinearPwmPoints[5] @ 0x2D (thermal check)
    FAN_CURVE_TEMP_5         = 27  # [G] FanLinearTempPoints[5] @ 0x33 (thermal check)

    # ── Fan legacy / acoustic controls (28–32) ─────────────────────────
    FAN_MINIMUM_PWM          = 28  # [I] FanMinimumPwm @ 0x34 (thermal check)
    FAN_ACOUSTIC_LIMIT       = 29  # [I] AcousticLimitRpmThreshold @ 0x38
    FAN_ACOUSTIC_TARGET      = 30  # [I] AcousticTargetRpmThreshold @ 0x36
    FAN_TARGET_TEMPERATURE   = 31  # [I] FanTargetTemperature @ 0x3A
    VDDGFX_VMAX              = 32  # [I] VddGfxVmax @ 0x10 (mV)

    # ── Confirmed RDNA4 extension slots (33–37) ────────────────────────
    FAN_MODE                 = 33  # [F] FanMode @ 0x3E
    GFXCLK_FOFFSET           = 34  # [F] GfxclkFoffset @ 0x18 (signed MHz)
    VF_CURVE_VOLTAGE_OFFSET  = 35  # [I] VoltageOffsetPerZoneBoundary @ 0x04
    ADVANCED_OD_MODE         = 36  # [I] AdvancedOdModeEnabled @ 0x40
    VDDSOC_VMAX              = 37  # [I] VddSocVmax @ 0x12 (mV)

    # ── Full control mode (39–43) ───────────────────────────────────────
    FULL_CTRL_GFX_VOLTAGE    = 39  # [I] GfxVoltageFullCtrlMode @ 0x44
    FULL_CTRL_SOC_VOLTAGE    = 40  # [I] SocVoltageFullCtrlMode @ 0x46
    FULL_CTRL_GFXCLK         = 41  # [I] GfxclkFullCtrlMode @ 0x48 (MHz)
    FULL_CTRL_UCLK           = 42  # [I] UclkFullCtrlMode @ 0x4A (MHz)
    FULL_CTRL_FCLK           = 43  # [I] FclkFullCtrlMode @ 0x4C (MHz)

    # ── EDC / PCC limits (44–45) ────────────────────────────────────────
    GFX_EDC                  = 44  # [I] GfxEdc @ 0x50
    GFX_PCC_LIMIT            = 45  # [I] GfxPccLimitControl @ 0x52

    # ── Table reset trigger (46) ─────────────────────────────────────────
    RESET_TABLE_TO_DEFAULTS  = 46  # [P] Resets OverDriveTable_t to VBIOS defaults

    # ── RDNA4 extension indices found in Ghidra ─────────────────────────
    RDNA4_EXT_48             = 48  # [G] SetGfxCurveFreq handler
    RDNA4_EXT_67             = 67  # [G] SetGfxCurveFreq handler
    RDNA4_EXT_68             = 68  # [G] SetGfxCurveFreq handler
    RDNA4_EXT_69             = 69  # [G] SetGfxCurveFreq handler

    # ── Driver-internal ─────────────────────────────────────────────────
    RESET_FLAG               = 71  # [F] ResetFlag (driver internal, not in OverDriveTable)


# Back-compat aliases for legacy Vega20 member names
Od8Setting.GFXCLK_FMIN = Od8Setting.GFX_CURVE_VF_0  # type: ignore[attr-defined]

LEGACY_OD8_NAMES: Dict[int, str] = {
    0:  "OD8_SETTING_GFXCLK_FMAX",
    1:  "OD8_SETTING_GFXCLK_FMIN",
    2:  "OD8_SETTING_GFXCLK_FREQ1",
    3:  "OD8_SETTING_GFXCLK_VOLTAGE1",
    4:  "OD8_SETTING_GFXCLK_FREQ2",
    5:  "OD8_SETTING_GFXCLK_VOLTAGE2",
    6:  "OD8_SETTING_GFXCLK_FREQ3",
    7:  "OD8_SETTING_GFXCLK_VOLTAGE3",
    8:  "OD8_SETTING_UCLK_FMAX",
    9:  "OD8_SETTING_POWER_PERCENTAGE",
    10: "OD8_SETTING_FAN_MIN_SPEED",
    11: "OD8_SETTING_FAN_ACOUSTIC_LIMIT",
    12: "OD8_SETTING_FAN_TARGET_TEMP",
    13: "OD8_SETTING_OPERATING_TEMP_MAX",
    14: "OD8_SETTING_AC_TIMING",
    15: "OD8_SETTING_FAN_ZERO_RPM_CONTROL",
    16: "OD8_SETTING_AUTO_UV_ENGINE",
    17: "OD8_SETTING_AUTO_OC_ENGINE",
    18: "OD8_SETTING_AUTO_OC_MEMORY",
    19: "OD8_SETTING_FAN_CURVE_TEMPERATURE_1",
    20: "OD8_SETTING_FAN_CURVE_SPEED_1",
    21: "OD8_SETTING_FAN_CURVE_TEMPERATURE_2",
    22: "OD8_SETTING_FAN_CURVE_SPEED_2",
    23: "OD8_SETTING_FAN_CURVE_TEMPERATURE_3",
    24: "OD8_SETTING_FAN_CURVE_SPEED_3",
    25: "OD8_SETTING_FAN_CURVE_TEMPERATURE_4",
    26: "OD8_SETTING_FAN_CURVE_SPEED_4",
    27: "OD8_SETTING_FAN_CURVE_TEMPERATURE_5",
    28: "OD8_SETTING_FAN_CURVE_SPEED_5",
    29: "OD8_SETTING_AUTO_FAN_ACOUSTIC_LIMIT",
}


@dataclass(frozen=True)
class Od8FieldMapping:
    """Maps an OD8 index to its OverDriveTable_t field and feature bit."""
    od_field: Optional[str]
    od_offset: Optional[int]
    feature_bit: Optional[int]
    unit: str
    confidence: str  # "F" / "G" / "I"

OD8_RDNA4_FIELD_MAP: Dict[int, Od8FieldMapping] = {
    # [F] Frida-confirmed
    5:  Od8FieldMapping("GfxclkFmaxVmax",               0x54, PpOdFeature.GFXCLK,       "MHz", "F"),
    6:  Od8FieldMapping("Ppt",                           0x24, PpOdFeature.PPT,           "%",  "F"),
    12: Od8FieldMapping("FanZeroRpmEnable",              0x3C, PpOdFeature.ZERO_FAN,      "",   "F"),
    16: Od8FieldMapping("FanLinearPwmPoints[0]",         0x28, PpOdFeature.FAN_CURVE,     "%",  "F"),
    17: Od8FieldMapping("FanLinearTempPoints[0]",        0x2E, PpOdFeature.FAN_CURVE,     "C",  "F"),
    18: Od8FieldMapping("FanLinearPwmPoints[1]",         0x29, PpOdFeature.FAN_CURVE,     "%",  "F"),
    19: Od8FieldMapping("FanLinearTempPoints[1]",        0x2F, PpOdFeature.FAN_CURVE,     "C",  "F"),
    20: Od8FieldMapping("FanLinearPwmPoints[2]",         0x2A, PpOdFeature.FAN_CURVE,     "%",  "F"),
    21: Od8FieldMapping("FanLinearTempPoints[2]",        0x30, PpOdFeature.FAN_CURVE,     "C",  "F"),
    22: Od8FieldMapping("FanLinearPwmPoints[3]",         0x2B, PpOdFeature.FAN_CURVE,     "%",  "F"),
    23: Od8FieldMapping("FanLinearTempPoints[3]",        0x31, PpOdFeature.FAN_CURVE,     "C",  "F"),
    24: Od8FieldMapping("FanLinearPwmPoints[4]",         0x2C, PpOdFeature.FAN_CURVE,     "%",  "F"),
    25: Od8FieldMapping("FanLinearTempPoints[4]",        0x32, PpOdFeature.FAN_CURVE,     "C",  "F"),
    33: Od8FieldMapping("FanMode",                       0x3E, PpOdFeature.FAN_CURVE,     "",   "F"),
    34: Od8FieldMapping("GfxclkFoffset",                 0x18, PpOdFeature.GFXCLK,        "MHz","F"),
    71: Od8FieldMapping(None,                            None, None,                       "",   "F"),
    # [G] Ghidra-confirmed (handler code references)
    0:  Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "G"),
    1:  Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "G"),
    2:  Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "G"),
    3:  Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "G"),
    4:  Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "G"),
    14: Od8FieldMapping(None,                            None, None,                       "",   "G"),
    26: Od8FieldMapping("FanLinearPwmPoints[5]",         0x2D, PpOdFeature.FAN_CURVE,     "%",  "G"),
    27: Od8FieldMapping("FanLinearTempPoints[5]",        0x33, PpOdFeature.FAN_CURVE,     "C",  "G"),
    48: Od8FieldMapping(None,                            None, None,                       "",   "G"),
    67: Od8FieldMapping(None,                            None, None,                       "",   "G"),
    68: Od8FieldMapping(None,                            None, None,                       "",   "G"),
    69: Od8FieldMapping(None,                            None, None,                       "",   "G"),
    # [I] Inferred (structural analysis + Linux kernel)
    7:  Od8FieldMapping("Tdc",                           0x26, PpOdFeature.TDC,            "",   "I"),
    8:  Od8FieldMapping("UclkFmax",                      0x1E, PpOdFeature.UCLK,          "MHz","I"),
    9:  Od8FieldMapping("UclkFmin",                      0x1C, PpOdFeature.UCLK,          "MHz","I"),
    10: Od8FieldMapping("FclkFmin",                      0x20, PpOdFeature.FCLK,          "MHz","I"),
    11: Od8FieldMapping("FclkFmax",                      0x22, PpOdFeature.FCLK,          "MHz","I"),
    13: Od8FieldMapping("MaxOpTemp",                     0x3F, PpOdFeature.TEMPERATURE,   "C",  "I"),
    15: Od8FieldMapping("FanZeroRpmStopTemp",            0x3D, PpOdFeature.ZERO_FAN,      "C",  "I"),
    28: Od8FieldMapping("FanMinimumPwm",                 0x34, PpOdFeature.FAN_LEGACY,    "%",  "I"),
    29: Od8FieldMapping("AcousticLimitRpmThreshold",     0x38, PpOdFeature.FAN_LEGACY,    "RPM","I"),
    30: Od8FieldMapping("AcousticTargetRpmThreshold",    0x36, PpOdFeature.FAN_LEGACY,    "RPM","I"),
    31: Od8FieldMapping("FanTargetTemperature",          0x3A, PpOdFeature.FAN_LEGACY,    "C",  "I"),
    32: Od8FieldMapping("VddGfxVmax",                    0x10, PpOdFeature.GFX_VMAX,      "mV", "I"),
    35: Od8FieldMapping("VoltageOffsetPerZoneBoundary",  0x04, PpOdFeature.GFX_VF_CURVE,  "mV", "I"),
    36: Od8FieldMapping("AdvancedOdModeEnabled",         0x40, None,                       "",   "I"),
    37: Od8FieldMapping("VddSocVmax",                    0x12, PpOdFeature.SOC_VMAX,      "mV", "I"),
    39: Od8FieldMapping("GfxVoltageFullCtrlMode",        0x44, PpOdFeature.FULL_CTRL,     "mV", "I"),
    40: Od8FieldMapping("SocVoltageFullCtrlMode",        0x46, PpOdFeature.FULL_CTRL,     "mV", "I"),
    41: Od8FieldMapping("GfxclkFullCtrlMode",            0x48, PpOdFeature.FULL_CTRL,     "MHz","I"),
    42: Od8FieldMapping("UclkFullCtrlMode",              0x4A, PpOdFeature.FULL_CTRL,     "MHz","I"),
    43: Od8FieldMapping("FclkFullCtrlMode",              0x4C, PpOdFeature.FULL_CTRL,     "MHz","I"),
    44: Od8FieldMapping("GfxEdc",                        0x50, PpOdFeature.EDC,            "",   "I"),
    45: Od8FieldMapping("GfxPccLimitControl",            0x52, PpOdFeature.EDC,            "",   "I"),
    # [P] Probe-confirmed
    46: Od8FieldMapping(None,                            None, None,                       "",   "P"),
}


class Od8Feature(enum.IntFlag):
    """OD8 feature capability bits. Source: vega20_hwmgr.h"""
    GFXCLK_LIMITS         = 1 << 0
    GFXCLK_CURVE          = 1 << 1
    UCLK_MAX              = 1 << 2
    POWER_LIMIT           = 1 << 3
    ACOUSTIC_LIMIT_SCLK   = 1 << 4
    FAN_SPEED_MIN         = 1 << 5
    TEMPERATURE_FAN       = 1 << 6
    TEMPERATURE_SYSTEM    = 1 << 7
    MEMORY_TIMING_TUNE    = 1 << 8
    FAN_ZERO_RPM_CONTROL  = 1 << 9


# ── OD Fail Codes (PMFW responses) ──────────────────────────────────────

class OdFail(enum.IntEnum):
    """PMFW OverDrive validation error codes.
    Source: smu14_driver_if_v14_0.h
    """
    NO_ERROR                      = 0
    REQUEST_ADVANCED_NOT_SUPPORTED = 1
    UNSUPPORTED_FEATURE           = 2
    INVALID_FEATURE_COMBO_ERROR   = 3
    GFXCLK_VF_CURVE_OFFSET_ERROR  = 4
    VDD_GFX_VMAX_ERROR            = 5
    VDD_SOC_VMAX_ERROR            = 6
    PPT_ERROR                     = 7
    FAN_MIN_PWM_ERROR             = 8
    FAN_ACOUSTIC_TARGET_ERROR     = 9
    FAN_ACOUSTIC_LIMIT_ERROR      = 10
    FAN_TARGET_TEMP_ERROR         = 11
    FAN_ZERO_RPM_STOP_TEMP_ERROR  = 12
    FAN_CURVE_PWM_ERROR           = 13
    FAN_CURVE_TEMP_ERROR          = 14
    FULL_CTRL_GFXCLK_ERROR        = 15
    FULL_CTRL_UCLK_ERROR          = 16
    FULL_CTRL_FCLK_ERROR          = 17
    FULL_CTRL_VDD_GFX_ERROR       = 18
    FULL_CTRL_VDD_SOC_ERROR       = 19
    TDC_ERROR                     = 20
    GFXCLK_ERROR                  = 21
    UCLK_ERROR                    = 22
    FCLK_ERROR                    = 23
    OP_TEMP_ERROR                 = 24
    OP_GFX_EDC_ERROR              = 25
    OP_GFX_PCC_ERROR              = 26
    POWER_FEATURE_CTRL_ERROR      = 27


# ── Fan Mode ─────────────────────────────────────────────────────────────

class FanMode(enum.IntEnum):
    AUTO           = 0
    MANUAL_LINEAR  = 1


# ── OverDriveTable_t (PMFW TABLE_OVERDRIVE, 156 bytes) ──────────────────

PP_NUM_OD_VF_CURVE_POINTS = 6
NUM_OD_FAN_MAX_POINTS = 6
SIZEOF_OVERDRIVE_TABLE = 0x9C  # 156 bytes

_OD_TABLE_FIELDS: List[Tuple[str, int, str, str]] = [
    # (name, offset, struct_fmt, description)
    ("FeatureCtrlMask",              0x00, "I",    "Active feature bitmask (PpOdFeature)"),
    ("VoltageOffsetPerZoneBoundary", 0x04, "6h",   "VF curve voltage offsets [6] (mV)"),
    ("VddGfxVmax",                   0x10, "H",    "GFX Vmax (mV)"),
    ("VddSocVmax",                   0x12, "H",    "SoC Vmax (mV)"),
    ("IdlePwrSavingFeaturesCtrl",    0x14, "B",    "Idle power saving control"),
    ("RuntimePwrSavingFeaturesCtrl", 0x15, "B",    "Runtime power saving control"),
    ("GfxclkFoffset",                0x18, "h",    "GFX clock frequency offset (MHz, signed)"),
    ("UclkFmin",                     0x1C, "H",    "Memory clock min (MHz)"),
    ("UclkFmax",                     0x1E, "H",    "Memory clock max (MHz)"),
    ("FclkFmin",                     0x20, "H",    "Fabric clock min (MHz)"),
    ("FclkFmax",                     0x22, "H",    "Fabric clock max (MHz)"),
    ("Ppt",                          0x24, "h",    "Power limit percentage (signed)"),
    ("Tdc",                          0x26, "h",    "Thermal design current limit (signed)"),
    ("FanLinearPwmPoints",           0x28, "6B",   "Fan curve PWM points [6] (%)"),
    ("FanLinearTempPoints",          0x2E, "6B",   "Fan curve temperature points [6] (C)"),
    ("FanMinimumPwm",                0x34, "H",    "Fan minimum PWM"),
    ("AcousticTargetRpmThreshold",   0x36, "H",    "Acoustic target RPM"),
    ("AcousticLimitRpmThreshold",    0x38, "H",    "Acoustic limit RPM"),
    ("FanTargetTemperature",         0x3A, "H",    "Fan target temperature (C)"),
    ("FanZeroRpmEnable",             0x3C, "B",    "Zero RPM fan enable"),
    ("FanZeroRpmStopTemp",           0x3D, "B",    "Zero RPM stop temperature (C)"),
    ("FanMode",                      0x3E, "B",    "Fan mode (0=auto, 1=manual)"),
    ("MaxOpTemp",                    0x3F, "B",    "Max operating temperature (C)"),
    ("AdvancedOdModeEnabled",        0x40, "B",    "Advanced OD mode enable"),
    ("GfxVoltageFullCtrlMode",       0x44, "H",    "GFX voltage full control mode"),
    ("SocVoltageFullCtrlMode",       0x46, "H",    "SoC voltage full control mode"),
    ("GfxclkFullCtrlMode",           0x48, "H",    "GFXCLK full control mode (MHz)"),
    ("UclkFullCtrlMode",             0x4A, "H",    "UCLK full control mode (MHz)"),
    ("FclkFullCtrlMode",             0x4C, "H",    "FCLK full control mode (MHz)"),
    ("GfxEdc",                       0x50, "h",    "GFX EDC limit (signed)"),
    ("GfxPccLimitControl",           0x52, "h",    "GFX PCC limit control (signed)"),
    ("GfxclkFmaxVmax",               0x54, "H",    "GFXCLK Fmax at Vmax"),
    ("GfxclkFmaxVmaxTemperature",    0x56, "B",    "GFXCLK Fmax Vmax temperature (C)"),
]


# ── OverDriveLimits_t (96 bytes, four in SkuTable) ──────────────────────

SIZEOF_OVERDRIVE_LIMITS = 0x60  # 96 bytes

_OD_LIMITS_FIELDS: List[Tuple[str, int, str, str]] = [
    ("FeatureCtrlMask",              0x00, "I",    "Supported feature bitmask"),
    ("VoltageOffsetPerZoneBoundary", 0x04, "6h",   "VF curve bounds [6] (mV)"),
    ("VddGfxVmax",                   0x10, "H",    "GFX Vmax bound (mV)"),
    ("VddSocVmax",                   0x12, "H",    "SoC Vmax bound (mV)"),
    ("GfxclkFoffset",                0x14, "h",    "GFX clock offset bound (MHz)"),
    ("UclkFmin",                     0x18, "H",    "UCLK min bound (MHz)"),
    ("UclkFmax",                     0x1A, "H",    "UCLK max bound (MHz)"),
    ("FclkFmin",                     0x1C, "H",    "FCLK min bound (MHz)"),
    ("FclkFmax",                     0x1E, "H",    "FCLK max bound (MHz)"),
    ("Ppt",                          0x20, "h",    "PPT bound (%)"),
    ("Tdc",                          0x22, "h",    "TDC bound"),
    ("FanLinearPwmPoints",           0x24, "6B",   "Fan PWM bounds [6]"),
    ("FanLinearTempPoints",          0x2A, "6B",   "Fan temp bounds [6]"),
    ("FanMinimumPwm",                0x30, "H",    "Fan min PWM bound"),
    ("AcousticTargetRpmThreshold",   0x32, "H",    "Acoustic target bound"),
    ("AcousticLimitRpmThreshold",    0x34, "H",    "Acoustic limit bound"),
    ("FanTargetTemperature",         0x36, "H",    "Fan target temp bound (C)"),
    ("FanZeroRpmEnable",             0x38, "B",    "Zero RPM bound"),
    ("MaxOpTemp",                    0x39, "B",    "Max op temp bound (C)"),
    ("GfxVoltageFullCtrlMode",       0x3C, "H",    "GFX voltage full ctrl bound"),
    ("SocVoltageFullCtrlMode",       0x3E, "H",    "SoC voltage full ctrl bound"),
    ("GfxclkFullCtrlMode",           0x40, "H",    "GFXCLK full ctrl bound"),
    ("UclkFullCtrlMode",             0x42, "H",    "UCLK full ctrl bound"),
    ("FclkFullCtrlMode",             0x44, "H",    "FCLK full ctrl bound"),
    ("GfxEdc",                       0x46, "h",    "GFX EDC bound"),
    ("GfxPccLimitControl",           0x48, "h",    "GFX PCC bound"),
]


# ── PP Table OverDrive Limits Offsets ────────────────────────────────────

PP_OD_LIMITS_BASIC_MIN_OFFSET    = 0x1000
PP_OD_LIMITS_BASIC_MAX_OFFSET    = 0x1060
PP_OD_LIMITS_ADVANCED_MIN_OFFSET = 0x10C0
PP_OD_LIMITS_ADVANCED_MAX_OFFSET = 0x1120
PP_OD_CAPABILITY_FLAGS_OFFSET    = 0x105C  # FeatureCtrlMask in BasicMin Spare area


# ── PMFW Table IDs ───────────────────────────────────────────────────────

class PmfwTableId(enum.IntEnum):
    """PMFW DMA table IDs for TransferTableDram2Smu.
    Source: smu14_driver_if_v14_0.h / decompiled FUN_14159190c
    """
    TABLE_PPTABLE              = 0
    TABLE_COMBO_PPTABLE        = 1
    TABLE_WATERMARKS           = 2   # unsupported on SMU 14.0.x
    TABLE_AVFS_PSM_DEBUG       = 3   # unsupported
    TABLE_SMU_METRICS          = 5   # unsupported
    TABLE_DRIVER_SMU_CONFIG    = 6
    TABLE_ACTIVITY_MONITOR     = 7
    TABLE_OVERDRIVE            = 8
    TABLE_I2C_COMMANDS         = 9
    TABLE_DRIVER_INFO          = 10  # read-only
    TABLE_CUSTOM_SKUTABLE      = 12


# ── CN Escape Cache ↔ OD8 ↔ OverDriveTable Mapping ─────────────────────

@dataclass(frozen=True)
class CnOdMapping:
    """Maps a CN escape record index to OD8 setting ID and OverDriveTable field."""
    cn_record: int
    cn_name: str
    od8_id: Optional[int]
    od_table_field: Optional[str]
    od_table_offset: Optional[int]
    pp_od_feature: Optional[int]
    unit: str


# NOTE: CN escape cache records use their OWN index numbering (cn_record),
# which is distinct from the OD8 escape (0x00C000A1) index numbering.
# The od8_id here is the LEGACY Vega20 OD8 index hint — on RDNA4, the
# actual OD8 indices have been remapped (e.g. OD8 idx 6 = Ppt, not idx 9).
CN_OD_MAPPINGS: List[CnOdMapping] = [
    CnOdMapping(0,  "GfxclkFoffset",   None,                              "GfxclkFoffset",                0x18, PpOdFeature.GFXCLK,     "MHz"),
    CnOdMapping(1,  "AutoUvEngine",    Od8Setting.FAN_CURVE_PWM_0,        None,                          None, None,                    ""),
    CnOdMapping(2,  "AutoOcEngine",    Od8Setting.FAN_CURVE_TEMP_0,       None,                          None, None,                    ""),
    CnOdMapping(8,  "UclkFmax",        Od8Setting.UCLK_FMAX,              "UclkFmax",                    0x1E, PpOdFeature.UCLK,        "MHz"),
    CnOdMapping(9,  "Ppt",             Od8Setting.UCLK_FMIN,              "Ppt",                         0x24, PpOdFeature.PPT,         "%"),
    CnOdMapping(10, "Tdc",             None,                               "Tdc",                         0x26, PpOdFeature.TDC,         ""),
    CnOdMapping(19, "FanTempPoint0",   Od8Setting.FAN_CURVE_TEMP_1,       "FanLinearTempPoints[0]",      0x2E, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(20, "FanPwmPoint0",    Od8Setting.FAN_CURVE_PWM_2,        "FanLinearPwmPoints[0]",       0x28, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(21, "FanTempPoint1",   Od8Setting.FAN_CURVE_TEMP_2,       "FanLinearTempPoints[1]",      0x2F, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(22, "FanPwmPoint1",    Od8Setting.FAN_CURVE_PWM_3,        "FanLinearPwmPoints[1]",       0x29, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(23, "FanTempPoint2",   Od8Setting.FAN_CURVE_TEMP_3,       "FanLinearTempPoints[2]",      0x30, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(24, "FanPwmPoint2",    Od8Setting.FAN_CURVE_PWM_4,        "FanLinearPwmPoints[2]",       0x2A, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(25, "FanTempPoint3",   Od8Setting.FAN_CURVE_TEMP_4,       "FanLinearTempPoints[3]",      0x31, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(26, "FanPwmPoint3",    Od8Setting.FAN_CURVE_PWM_5,        "FanLinearPwmPoints[3]",       0x2B, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(27, "FanTempPoint4",   Od8Setting.FAN_CURVE_TEMP_5,       "FanLinearTempPoints[4]",      0x32, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(28, "FanPwmPoint4",    Od8Setting.FAN_MINIMUM_PWM,        "FanLinearPwmPoints[4]",       0x2C, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(29, "FanTempPoint5",   Od8Setting.FAN_ACOUSTIC_LIMIT,     "FanLinearTempPoints[5]",      0x33, PpOdFeature.FAN_CURVE,   "C"),
    CnOdMapping(30, "FanPwmPoint5",    Od8Setting.FAN_ACOUSTIC_TARGET,    "FanLinearPwmPoints[5]",       0x2D, PpOdFeature.FAN_CURVE,   "%"),
    CnOdMapping(37, "VoltageOffset",   None,                               "VoltageOffsetPerZoneBoundary", 0x04, PpOdFeature.GFX_VF_CURVE, "mV"),
]

CN_RECORD_TO_MAPPING: Dict[int, CnOdMapping] = {m.cn_record: m for m in CN_OD_MAPPINGS}
CN_NAME_TO_MAPPING: Dict[str, CnOdMapping] = {m.cn_name: m for m in CN_OD_MAPPINGS}


# ── Helper: Parse OverDriveTable_t from bytes ────────────────────────────

def parse_overdrive_table(data: bytes, offset: int = 0) -> Dict[str, object]:
    """Parse a 156-byte OverDriveTable_t blob into a field dict."""
    result: Dict[str, object] = {}
    for name, off, fmt, _desc in _OD_TABLE_FIELDS:
        abs_off = offset + off
        sz = struct.calcsize(fmt)
        if abs_off + sz > len(data):
            continue
        val = struct.unpack_from(f"<{fmt}", data, abs_off)
        result[name] = val[0] if len(val) == 1 else list(val)
    return result


def parse_overdrive_limits(data: bytes, offset: int = 0) -> Dict[str, object]:
    """Parse a 96-byte OverDriveLimits_t blob into a field dict."""
    result: Dict[str, object] = {}
    for name, off, fmt, _desc in _OD_LIMITS_FIELDS:
        abs_off = offset + off
        sz = struct.calcsize(fmt)
        if abs_off + sz > len(data):
            continue
        val = struct.unpack_from(f"<{fmt}", data, abs_off)
        result[name] = val[0] if len(val) == 1 else list(val)
    return result


def parse_pptable_od_limits(
    pptable: bytes,
) -> Dict[str, Dict[str, object]]:
    """Extract all four OverDriveLimits from a PP table blob."""
    return {
        "BasicMin":     parse_overdrive_limits(pptable, PP_OD_LIMITS_BASIC_MIN_OFFSET),
        "BasicMax":     parse_overdrive_limits(pptable, PP_OD_LIMITS_BASIC_MAX_OFFSET),
        "AdvancedMin":  parse_overdrive_limits(pptable, PP_OD_LIMITS_ADVANCED_MIN_OFFSET),
        "AdvancedMax":  parse_overdrive_limits(pptable, PP_OD_LIMITS_ADVANCED_MAX_OFFSET),
    }


# ── Helper: Build OverDriveTable_t bytes ─────────────────────────────────

def build_overdrive_table(**kwargs) -> bytearray:
    """Construct a 156-byte OverDriveTable_t from keyword arguments.

    Unset fields default to zero.  Field names match _OD_TABLE_FIELDS.

    Example::

        buf = build_overdrive_table(
            FeatureCtrlMask=PpOdFeature.GFXCLK | PpOdFeature.PPT,
            GfxclkFoffset=100,
            Ppt=15,
        )
    """
    buf = bytearray(SIZEOF_OVERDRIVE_TABLE)
    for name, off, fmt, _desc in _OD_TABLE_FIELDS:
        if name not in kwargs:
            continue
        val = kwargs[name]
        if isinstance(val, (list, tuple)):
            struct.pack_into(f"<{fmt}", buf, off, *val)
        else:
            struct.pack_into(f"<{fmt}", buf, off, val)
    return buf


# ── Driver RVAs (for Ghidra cross-reference) ─────────────────────────────

DRIVER_RVAS = {
    "dxgk_ddi_escape":              0x00232300,
    "atid_validation_1":            0x00058246,
    "atid_validation_2":            0x000CDD76,
    "cn_escape_input_handler":      0x0146C740,
    "driver_entry":                 0x00230280,
    "handle_cwdde_legacy":          0x002691E8,
    "pp_iri_dispatch":              0x00269744,
    "cwddepm_cmd_dispatch":         0x00269BB8,
    "cwdde_command_lookup":         0x0026949C,
    "cwddepm_new_path":             0x01472A68,
    "cwddepm_func_table":           0x009513A0,
    "smu_set_pp_table":             0x015926B8,
    "overdrive_field_copy":         0x01592800,
    "smu14_apply_soft_pptable":     0x01592474,
    "smu_14_0_3_set_soft_table":    0x01592644,
    "smu14_transfer_table":         0x0159190C,
    "smu14_do_transfer":            0x01590958,
}


# ── CWDDEPM Function Table (8 entries at PTR_FUN_1409513a0) ──────────────
# Discovered in DecompilePPTable13.java (pass 13).
#
# The CWDDEPM dispatch table holds 8 function pointers (8 bytes each),
# indexed 0–7 by cwddepm_new_path (RVA 0x01472A68).  The dispatch guard
# is `param_3 < 8` and the logged command code is `param_3 + 0xC08001`.
#
# These are WRAPPER functions in PAGEPPLC.  The actual PEM-level OD8
# implementations (0x014582E0 SetSettings, 0x01457EF4 GetInitialParam,
# 0x014580F0 GetCurrentSettings) are called indirectly through vtable
# pointers stored in the PowerPlay context object.
#
# Entries [4] and [6] share the same handler (stub / unimplemented).
# Entries [1] and [5] are thunks too small for Ghidra to resolve.

CWDDEPM_FUNC_TABLE: Dict[int, Dict[str, object]] = {
    # idx  cmd_code   handler_rva   size   identified_name
    0: {"cmd": 0xC08001, "rva": 0x014721DC, "size":   99,
        "name": "GetState_or_Query",
        "notes": "Checks param_2+4 for values 1 (returns 2) or 2 (vtable call "
                 "via context+0x1048).  Simple state query."},
    1: {"cmd": 0xC08002, "rva": 0x01472248, "size": None,
        "name": "(thunk_unresolved)",
        "notes": "Ghidra found no function — likely a very small thunk/jump."},
    2: {"cmd": 0xC08003, "rva": 0x01472280, "size":  161,
        "name": "OD8_GetData",
        "notes": "Read handler.  Reads data via vtable (context+0x1048), copies "
                 "to output buffer.  Likely OD8_GetInitialParam or GetCurrentSettings."},
    3: {"cmd": 0xC08004, "rva": 0x01472328, "size":  163,
        "name": "OD8_SetSettings",
        "notes": "Write handler.  Calls FUN_1414566ac (1647B core logic) then "
                 "FUN_14146c694 (thermal policy change).  Checks lock at "
                 "context+0x1050.  The wrapper for PEM_CWDDEPM_OD8_SetSettings."},
    4: {"cmd": 0xC08005, "rva": 0x01456D28, "size": None,
        "name": "(stub_shared_with_6)",
        "notes": "Shared with entry [6].  Ghidra found no function — stub or "
                 "unimplemented handler."},
    5: {"cmd": 0xC08006, "rva": 0x014723D4, "size": None,
        "name": "(thunk_unresolved)",
        "notes": "Ghidra found no function — likely a very small thunk/jump."},
    6: {"cmd": 0xC08007, "rva": 0x01456D28, "size": None,
        "name": "(stub_shared_with_4)",
        "notes": "Same handler as entry [4]."},
    7: {"cmd": 0xC08008, "rva": 0x014723F0, "size": 1647,
        "name": "ActivateClient",
        "notes": "Most complex handler.  Calls cwddepm_cmd_dispatch, references "
                 "'CWDDEPM Function Table' and 'isPPLibActive' strings.  Sends "
                 "IPS notifications to IRQMGR, KMD, and DAL subsystems.  "
                 "Full PowerPlay client activation/initialization path."},
}

# PEM-level OD8 implementations (called indirectly via vtable from wrappers above)
CWDDEPM_PEM_RVAS = {
    "CWDDEPM_OD8_SetSettings":       0x014582E0,
    "CWDDEPM_OD8_GetInitialParam":   0x01457EF4,
    "CWDDEPM_OD8_GetCurrentSettings": 0x014580F0,
    "SmartShift_handler_1":          0x01458A00,
    "SmartShift_handler_2":          0x014588D0,
}


# ── PhwComm_OD8 Handler String RVAs (.rdata) ────────────────────────────
# NOTE: these are debug STRING addresses in .rdata (0x7FA000-0xD3E000),
# NOT code entry points.  To find the actual code, follow xrefs from
# code sections to these strings.  See DecompilePPTable11.java results.

OD8_HANDLER_STRING_RVAS = {
    "SetGfxClkLimits":       0x0096E199,
    "SetGfxCurveVoltage":    0x0096E231,
    "SetGfxCurveFreq":       0x00970CF8,
    "SetQuadraticGfx":       0x00970EC8,
    "SetUClockLimits":       0x00970F68,
    "SetPowerLimits":        0x00970FF8,
    "SetTDCLimits":          0x00971088,
    "SetAutoFanAcoustic":    0x009710C9,
    "SetFanCurve":           0x009717D8,
    "SetZeroFanRPM":         0x00970B58,
    "SetGpuPowerMode":       0x00970BD1,
    "SetAutoUvEngine":       0x00971598,
    "SetAutoOcEngine":       0x00971628,
    "SetAutoOcMemory":       0x009716B8,
    "SetMemoryTiming":       0x00971878,
    "SetGfxVoltageLimits":   0x00971958,
    "SetCurrentSettings":    0x00970FA9,
    "ApplySettings":         0x00970C11,
    "TurnOffFeatures":       0x00971B19,
    "ResetAllFeatures":      0x00971BD9,
    "GetGfxClockBase":       0x0094F8C9,
    "UpdateInternalSettings": 0x00954D09,
    "CheckRangeAdvanced":    0x00987329,
    "GetInitialDefaults":    0x0096D319,
    "GetCurrentSettings":    0x0096E149,
    "InitializeGfxDpm":      0x0096D3B9,
    "InitializeUCLKDpm":     0x0096D4C9,
    "InitializeFanOverride":  0x0096D579,
}

# Back-compat alias
OD8_HANDLER_RVAS = OD8_HANDLER_STRING_RVAS

# ── PhwComm_OD8 Handler Code RVAs (PAGEPPLC section) ────────────────────
# Resolved via string xref tracing in DecompilePPTable11.java (pass 11).
# Multiple code functions may reference the same handler string.

OD8_HANDLER_CODE_RVAS = {
    "SetGfxClkLimits":       [0x0149F68C, 0x01498910],
    "SetGfxCurveVoltage":    [0x0149FA68, 0x014AC354],
    "SetGfxCurveFreq":       [0x01494BC0],
    "SetQuadraticGfx":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetUClockLimits":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetPowerLimits":        [],   # xrefs resolved to SetGfxCurveFreq
    "SetTDCLimits":          [],   # xrefs resolved to SetGfxCurveFreq
    "SetAutoFanAcoustic":    [],   # xrefs resolved to SetGfxCurveFreq
    "SetFanCurve":           [],   # xrefs resolved to SetGfxCurveFreq
    "SetZeroFanRPM":         [],   # xrefs resolved to SetGfxCurveFreq
    "SetGpuPowerMode":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetAutoUvEngine":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetAutoOcEngine":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetAutoOcMemory":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetMemoryTiming":       [],   # xrefs resolved to SetGfxCurveFreq
    "SetGfxVoltageLimits":   [],   # xrefs resolved to SetGfxCurveFreq
    "SetCurrentSettings":    [],   # xrefs resolved to SetGfxCurveFreq
    "ApplySettings":         [],   # xrefs resolved to SetGfxCurveFreq
    "TurnOffFeatures":       [0x014A158C],
    "ResetAllFeatures":      [0x0149C40C],
    "GetGfxClockBase":       [0x01498B4C],
    "UpdateInternalSettings": [0x0147A9C0, 0x0149923C, 0x0147B2B8],
    "CheckRangeAdvanced":    [],   # no unique xrefs found
    "GetInitialDefaults":    [0x01498D14, 0x0149C06C, 0x014A1B38],
    "GetCurrentSettings":    [],   # xrefs resolved to already-decompiled funcs
    "InitializeGfxDpm":      [],   # xrefs resolved to already-decompiled funcs
    "InitializeUCLKDpm":     [],   # xrefs resolved to already-decompiled funcs
    "InitializeFanOverride":  [],   # xrefs resolved to already-decompiled funcs
}

# ── OD8 Index → Handler Mapping (from Ghidra pass 11) ───────────────────
# Extracted by finding stride-aligned hex constants (0x14-byte OD8Entry)
# in decompiled handler code.  Index 71 (0x058C) = ResetFlag, appears in
# nearly every handler.  Indices 48, 67-69 are unnamed RDNA4-era slots.
#
# Many Set* handlers (SetPowerLimits, SetTDCLimits, SetFanCurve, etc.)
# had their string xrefs resolve into the monolithic SetGfxCurveFreq
# function (0x01494BC0, 4864 bytes) — the actual index extraction for
# those handlers requires isolating their code blocks within that function,
# or using live probing (Track B).

OD8_INDEX_TO_HANDLER = {
    0:  ["SetGfxCurveVoltage"],              # GFX_CURVE_VF_0
    1:  ["SetGfxClkLimits",                  # GFX_CURVE_VF_1
         "SetGfxCurveFreq",
         "ResetAllFeatures"],
    2:  ["SetGfxClkLimits",                  # GFX_CURVE_VF_2
         "SetGfxCurveVoltage",
         "TurnOffFeatures"],
    3:  ["SetGfxClkLimits",                  # GFX_CURVE_VF_3
         "TurnOffFeatures",
         "ResetAllFeatures"],
    4:  ["ResetAllFeatures"],                # GFX_CURVE_VF_4
    5:  ["UpdateInternalSettings"],           # GFXCLK_FMAX
    6:  ["UpdateInternalSettings"],           # PPT
    7:  ["TurnOffFeatures"],                 # TDC
    8:  ["TurnOffFeatures"],                 # UCLK_FMAX
    12: ["SetGfxCurveFreq",                  # FAN_ZERO_RPM_ENABLE
         "TurnOffFeatures"],
    14: ["GetGfxClockBase"],                 # AC_TIMING
    16: ["SetGfxCurveVoltage"],              # FAN_CURVE_PWM_0
    22: ["SetGfxCurveFreq"],                 # FAN_CURVE_PWM_3
    23: ["SetGfxCurveFreq"],                 # FAN_CURVE_TEMP_3
    26: ["SetGfxCurveFreq"],                 # FAN_CURVE_PWM_5
    27: ["SetGfxCurveFreq"],                 # FAN_CURVE_TEMP_5
    46: ["ResetAllFeatures"],                # RESET_TABLE_TO_DEFAULTS (probe-confirmed)
    48: ["SetGfxCurveFreq"],                 # RDNA4_EXT_48
    67: ["SetGfxCurveFreq"],                 # RDNA4_EXT_67
    68: ["SetGfxCurveFreq"],                 # RDNA4_EXT_68
    69: ["SetGfxCurveFreq"],                 # RDNA4_EXT_69
    71: ["SetGfxClkLimits",                  # RESET_FLAG
         "SetGfxCurveVoltage",
         "SetGfxCurveFreq",
         "TurnOffFeatures",
         "ResetAllFeatures",
         "GetGfxClockBase",
         "UpdateInternalSettings"],
}
