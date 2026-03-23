"""
OverDrive Table -- GPU OD Parameter Control via SMU Table Transfer
===================================================================

Defines the OverDriveTable_t ctypes struct matching the SMU firmware's
layout (from smu14_driver_if_v14_0.h), the FeatureCtrlMask bit
constants, and the OdController class that orchestrates read-modify-write
cycles through the DMA buffer + SMU table transfer protocol.

This is the same mechanism used by the Linux kernel amdgpu driver and
MSI Afterburner to apply GPU overclocking settings that "stick" --
unlike raw SetSoftMin/Max messages which the Windows driver overrides.

Architecture:
    OdController  --->  DmaBuffer (physically-pinned RAM)
        |                   |
        |   set_dram_addr   |
        +----> SmuCmd ------+---> SMU Firmware
        |   Smu2Dram/Dram2Smu       reads/writes buffer via PCIe DMA

Source reference:
    linux/drivers/gpu/drm/amd/pm/swsmu/inc/pmfw_if/smu14_driver_if_v14_0.h
    linux/drivers/gpu/drm/amd/pm/swsmu/smu_cmn.c  (smu_cmn_update_table)
    linux/drivers/gpu/drm/amd/pm/swsmu/smu14/smu_v14_0.c  (set_driver_table_location)
"""

import ctypes


# ---------------------------------------------------------------------------
# Constants from smu14_driver_if_v14_0.h
# ---------------------------------------------------------------------------

PP_NUM_RTAVFS_PWL_ZONES   = 5
PP_NUM_OD_VF_CURVE_POINTS = PP_NUM_RTAVFS_PWL_ZONES + 1  # = 6
NUM_OD_FAN_MAX_POINTS     = 6

# Table IDs for TransferTableDram2Smu / TransferTableSmu2Dram
TABLE_PPTABLE              = 0
TABLE_COMBO_PPTABLE        = 1
TABLE_WATERMARKS           = 2
TABLE_CUSTOM_DPM           = 3
TABLE_BACO_PARAMS          = 4
TABLE_SMU_METRICS          = 5
TABLE_DRIVER_SMU_CONFIG    = 6
TABLE_ACTIVITY_MONITOR     = 7
TABLE_OVERDRIVE            = 8
TABLE_I2C_COMMANDS         = 9
TABLE_DRIVER_INFO          = 10
TABLE_ECCINFO              = 11  # ECC counters (RDNA3/RDNA4, smu14_driver_if_v14_0.h)
TABLE_CUSTOM_SKUTABLE      = 12  # Soft PP / custom SKU table (Windows driver extension)


# ---------------------------------------------------------------------------
# FeatureCtrlMask bit positions (PP_OD_FEATURE_*)
# ---------------------------------------------------------------------------

PP_OD_FEATURE_GFX_VF_CURVE_BIT  = 0
PP_OD_FEATURE_GFX_VMAX_BIT      = 1
PP_OD_FEATURE_SOC_VMAX_BIT      = 2
PP_OD_FEATURE_PPT_BIT           = 3
PP_OD_FEATURE_FAN_CURVE_BIT     = 4
PP_OD_FEATURE_FAN_LEGACY_BIT    = 5
PP_OD_FEATURE_FULL_CTRL_BIT     = 6
PP_OD_FEATURE_TDC_BIT           = 7
PP_OD_FEATURE_GFXCLK_BIT        = 8
PP_OD_FEATURE_UCLK_BIT          = 9
PP_OD_FEATURE_FCLK_BIT          = 10
PP_OD_FEATURE_ZERO_FAN_BIT      = 11
PP_OD_FEATURE_TEMPERATURE_BIT   = 12
PP_OD_FEATURE_EDC_BIT           = 13
PP_OD_FEATURE_COUNT              = 14

# OD_FAIL_e -- SMU rejection codes (smu14_driver_if_v14_0.h)
# When TransferTableDram2Smu returns FAIL, PARAM may contain this code.
OD_NO_ERROR = 0
OD_REQUEST_ADVANCED_NOT_SUPPORTED = 1
OD_UNSUPPORTED_FEATURE = 2
OD_INVALID_FEATURE_COMBO_ERROR = 3
OD_GFXCLK_VF_CURVE_OFFSET_ERROR = 4
OD_VDD_GFX_VMAX_ERROR = 5
OD_VDD_SOC_VMAX_ERROR = 6
OD_PPT_ERROR = 7
OD_FAN_MIN_PWM_ERROR = 8
OD_FAN_ACOUSTIC_TARGET_ERROR = 9
OD_FAN_ACOUSTIC_LIMIT_ERROR = 10
OD_FAN_TARGET_TEMP_ERROR = 11
OD_FAN_ZERO_RPM_STOP_TEMP_ERROR = 12
OD_FAN_CURVE_PWM_ERROR = 13
OD_FAN_CURVE_TEMP_ERROR = 14
OD_FULL_CTRL_GFXCLK_ERROR = 15
OD_FULL_CTRL_UCLK_ERROR = 16
OD_FULL_CTRL_FCLK_ERROR = 17
OD_FULL_CTRL_VDD_GFX_ERROR = 18
OD_FULL_CTRL_VDD_SOC_ERROR = 19
OD_TDC_ERROR = 20
OD_GFXCLK_ERROR = 21
OD_UCLK_ERROR = 22
OD_FCLK_ERROR = 23
OD_OP_TEMP_ERROR = 24
OD_OP_GFX_EDC_ERROR = 25
OD_OP_GFX_PCC_ERROR = 26
OD_POWER_FEATURE_CTRL_ERROR = 27

_OD_FAIL_NAMES = {
    OD_NO_ERROR: "No error",
    OD_REQUEST_ADVANCED_NOT_SUPPORTED: "Advanced OD mode not supported",
    OD_UNSUPPORTED_FEATURE: "Unsupported feature",
    OD_INVALID_FEATURE_COMBO_ERROR: "Invalid feature combination",
    OD_GFXCLK_VF_CURVE_OFFSET_ERROR: "GFX V/F curve offset invalid",
    OD_VDD_GFX_VMAX_ERROR: "VddGfx Vmax invalid",
    OD_VDD_SOC_VMAX_ERROR: "VddSoc Vmax invalid",
    OD_PPT_ERROR: "PPT invalid",
    OD_FAN_MIN_PWM_ERROR: "Fan min PWM invalid",
    OD_FAN_ACOUSTIC_TARGET_ERROR: "Fan acoustic target invalid",
    OD_FAN_ACOUSTIC_LIMIT_ERROR: "Fan acoustic limit invalid",
    OD_FAN_TARGET_TEMP_ERROR: "Fan target temp invalid",
    OD_FAN_ZERO_RPM_STOP_TEMP_ERROR: "Fan zero-RPM stop temp invalid",
    OD_FAN_CURVE_PWM_ERROR: "Fan curve PWM invalid",
    OD_FAN_CURVE_TEMP_ERROR: "Fan curve temp invalid",
    OD_FULL_CTRL_GFXCLK_ERROR: "Full-ctrl GFXCLK invalid",
    OD_FULL_CTRL_UCLK_ERROR: "Full-ctrl UCLK invalid",
    OD_FULL_CTRL_FCLK_ERROR: "Full-ctrl FCLK invalid",
    OD_FULL_CTRL_VDD_GFX_ERROR: "Full-ctrl VddGfx invalid",
    OD_FULL_CTRL_VDD_SOC_ERROR: "Full-ctrl VddSoc invalid",
    OD_TDC_ERROR: "TDC invalid",
    OD_GFXCLK_ERROR: "GFXCLK invalid",
    OD_UCLK_ERROR: "UCLK invalid (value out of DPM range or UclkFmin > UclkFmax)",
    OD_FCLK_ERROR: "FCLK invalid",
    OD_OP_TEMP_ERROR: "Max op temp invalid",
    OD_OP_GFX_EDC_ERROR: "Gfx EDC invalid",
    OD_OP_GFX_PCC_ERROR: "Gfx PCC limit invalid",
    OD_POWER_FEATURE_CTRL_ERROR: "Power feature ctrl error",
}


def decode_od_fail(param_value: int | None) -> str:
    """Decode PARAM register when TransferTableDram2Smu returns FAIL.
    SMU may put OD_FAIL_e in upper 16 bits; lower bits vary by firmware."""
    if param_value is None:
        return "SMU rejected OD table (no detail)"
    param_value = param_value & 0xFFFFFFFF  # ensure uint32
    code = (param_value >> 16) & 0xFF  # try high byte (e.g. 0x000200FF -> 2)
    if code in _OD_FAIL_NAMES:
        return _OD_FAIL_NAMES[code]
    code = param_value & 0xFF
    if code in _OD_FAIL_NAMES:
        return _OD_FAIL_NAMES[code]
    return f"SMU rejected OD table (PARAM=0x{param_value:08X}, try OD error code in high/low bits)"


_OD_FEATURE_NAMES = {
    PP_OD_FEATURE_GFX_VF_CURVE_BIT:  "GFX_VF_CURVE",
    PP_OD_FEATURE_GFX_VMAX_BIT:      "GFX_VMAX",
    PP_OD_FEATURE_SOC_VMAX_BIT:      "SOC_VMAX",
    PP_OD_FEATURE_PPT_BIT:           "PPT",
    PP_OD_FEATURE_FAN_CURVE_BIT:     "FAN_CURVE",
    PP_OD_FEATURE_FAN_LEGACY_BIT:    "FAN_LEGACY",
    PP_OD_FEATURE_FULL_CTRL_BIT:     "FULL_CTRL",
    PP_OD_FEATURE_TDC_BIT:           "TDC",
    PP_OD_FEATURE_GFXCLK_BIT:        "GFXCLK",
    PP_OD_FEATURE_UCLK_BIT:          "UCLK",
    PP_OD_FEATURE_FCLK_BIT:          "FCLK",
    PP_OD_FEATURE_ZERO_FAN_BIT:      "ZERO_FAN",
    PP_OD_FEATURE_TEMPERATURE_BIT:    "TEMPERATURE",
    PP_OD_FEATURE_EDC_BIT:           "EDC",
}


# ---------------------------------------------------------------------------
# OverDriveTable_t -- matches the C struct exactly
# ---------------------------------------------------------------------------

class OverDriveTable_t(ctypes.Structure):
    """
    OverDrive parameter table -- firmware struct layout.

    This must match the C struct in smu14_driver_if_v14_0.h lines 751-807
    exactly, including field order, types, and padding.  The SMU firmware
    reads/writes this struct directly via DMA.

    _pack_ = 1 ensures no compiler padding is added.
    """
    _pack_ = 1
    _fields_ = [
        # Feature control bitmask -- tells SMU which fields to apply
        ("FeatureCtrlMask",              ctypes.c_uint32),

        # Voltage control
        ("VoltageOffsetPerZoneBoundary", ctypes.c_int16 * PP_NUM_OD_VF_CURVE_POINTS),
        ("VddGfxVmax",                   ctypes.c_uint16),   # mV
        ("VddSocVmax",                   ctypes.c_uint16),

        ("IdlePwrSavingFeaturesCtrl",    ctypes.c_uint8),
        ("RuntimePwrSavingFeaturesCtrl", ctypes.c_uint8),
        ("Padding",                      ctypes.c_uint16),

        # Frequency changes
        ("GfxclkFoffset",               ctypes.c_int16),    # MHz offset (signed)
        ("Padding1",                     ctypes.c_uint16),
        ("UclkFmin",                     ctypes.c_uint16),   # MHz
        ("UclkFmax",                     ctypes.c_uint16),   # MHz
        ("FclkFmin",                     ctypes.c_uint16),   # MHz
        ("FclkFmax",                     ctypes.c_uint16),   # MHz

        # PPT (Package Power Tracking)
        ("Ppt",                          ctypes.c_int16),    # % over default
        ("Tdc",                          ctypes.c_int16),    # % over default

        # Fan control
        ("FanLinearPwmPoints",           ctypes.c_uint8 * NUM_OD_FAN_MAX_POINTS),
        ("FanLinearTempPoints",          ctypes.c_uint8 * NUM_OD_FAN_MAX_POINTS),
        ("FanMinimumPwm",               ctypes.c_uint16),
        ("AcousticTargetRpmThreshold",   ctypes.c_uint16),
        ("AcousticLimitRpmThreshold",    ctypes.c_uint16),
        ("FanTargetTemperature",         ctypes.c_uint16),   # Celsius
        ("FanZeroRpmEnable",             ctypes.c_uint8),
        ("FanZeroRpmStopTemp",           ctypes.c_uint8),
        ("FanMode",                      ctypes.c_uint8),
        ("MaxOpTemp",                    ctypes.c_uint8),

        ("AdvancedOdModeEnabled",        ctypes.c_uint8),
        ("Padding2",                     ctypes.c_uint8 * 3),

        # Full control mode fields
        ("GfxVoltageFullCtrlMode",       ctypes.c_uint16),
        ("SocVoltageFullCtrlMode",       ctypes.c_uint16),
        ("GfxclkFullCtrlMode",           ctypes.c_uint16),
        ("UclkFullCtrlMode",             ctypes.c_uint16),
        ("FclkFullCtrlMode",             ctypes.c_uint16),
        ("Padding3",                     ctypes.c_uint16),

        # EDC / PCC
        ("GfxEdc",                       ctypes.c_int16),
        ("GfxPccLimitControl",           ctypes.c_int16),

        # Fmax/Vmax
        ("GfxclkFmaxVmax",              ctypes.c_uint16),
        ("GfxclkFmaxVmaxTemperature",    ctypes.c_uint8),
        ("Padding4",                     ctypes.c_uint8 * 1),

        # Spare / reserved
        ("Spare",                        ctypes.c_uint32 * 9),

        # MmHub padding (SMU internal use)
        ("MmHubPadding",                ctypes.c_uint32 * 8),
    ]


class OverDriveTableExternal_t(ctypes.Structure):
    """
    External wrapper -- this is what gets transferred via DMA.

    In the kernel header this is just a wrapper around OverDriveTable_t
    (no extra fields), but the struct is what SMU reads/writes.
    """
    _pack_ = 1
    _fields_ = [
        ("OverDriveTable", OverDriveTable_t),
    ]


# Verify struct size is reasonable (should be < 4096 bytes to fit in one page)
_OD_TABLE_SIZE = ctypes.sizeof(OverDriveTable_t)
_OD_TABLE_EXT_SIZE = ctypes.sizeof(OverDriveTableExternal_t)


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def decode_feature_mask(mask):
    """Decode FeatureCtrlMask into list of feature names."""
    features = []
    for bit in range(PP_OD_FEATURE_COUNT):
        if mask & (1 << bit):
            name = _OD_FEATURE_NAMES.get(bit, f"BIT_{bit}")
            features.append((bit, name))
    return features


def dump_od_table(table):
    """
    Print all fields of an OverDriveTable_t in human-readable form.

    Args:
        table: OverDriveTable_t instance.
    """
    print(f"\n{'=' * 60}")
    print(f"  OverDriveTable_t  (size={ctypes.sizeof(table)} bytes)")
    print(f"{'=' * 60}")

    mask = table.FeatureCtrlMask
    features = decode_feature_mask(mask)
    feat_str = ", ".join(n for _, n in features) if features else "(none)"
    print(f"\n  FeatureCtrlMask:   0x{mask:08X}  [{feat_str}]")

    # Voltage
    vf = list(table.VoltageOffsetPerZoneBoundary)
    print(f"\n  --- Voltage ---")
    print(f"  VoltageOffsetPerZone: {vf}")
    print(f"  VddGfxVmax:          {table.VddGfxVmax} mV")
    print(f"  VddSocVmax:          {table.VddSocVmax} mV")

    # Power saving
    print(f"  IdlePwrSavingCtrl:   {table.IdlePwrSavingFeaturesCtrl}")
    print(f"  RuntimePwrSavingCtrl:{table.RuntimePwrSavingFeaturesCtrl}")

    # Frequency
    print(f"\n  --- Frequency ---")
    print(f"  GfxclkFoffset:       {table.GfxclkFoffset} MHz")
    print(f"  UclkFmin:            {table.UclkFmin} MHz")
    print(f"  UclkFmax:            {table.UclkFmax} MHz")
    print(f"  FclkFmin:            {table.FclkFmin} MHz")
    print(f"  FclkFmax:            {table.FclkFmax} MHz")

    # PPT
    print(f"\n  --- PPT ---")
    print(f"  Ppt:                 {table.Ppt}%")
    print(f"  Tdc:                 {table.Tdc}%")

    # Fan
    print(f"\n  --- Fan ---")
    pwm_pts = list(table.FanLinearPwmPoints)
    temp_pts = list(table.FanLinearTempPoints)
    print(f"  FanLinearPwmPoints:  {pwm_pts}")
    print(f"  FanLinearTempPoints: {temp_pts}")
    print(f"  FanMinimumPwm:       {table.FanMinimumPwm}")
    print(f"  AcousticTargetRpm:   {table.AcousticTargetRpmThreshold}")
    print(f"  AcousticLimitRpm:    {table.AcousticLimitRpmThreshold}")
    print(f"  FanTargetTemp:       {table.FanTargetTemperature} C")
    print(f"  FanZeroRpmEnable:    {table.FanZeroRpmEnable}")
    print(f"  FanZeroRpmStopTemp:  {table.FanZeroRpmStopTemp}")
    print(f"  FanMode:             {table.FanMode}")
    print(f"  MaxOpTemp:           {table.MaxOpTemp}")

    # Advanced OD
    print(f"\n  --- Advanced OD ---")
    print(f"  AdvancedOdModeEnabled: {table.AdvancedOdModeEnabled}")

    # Full control
    print(f"\n  --- Full Control Mode ---")
    print(f"  GfxVoltageFullCtrl:  {table.GfxVoltageFullCtrlMode}")
    print(f"  SocVoltageFullCtrl:  {table.SocVoltageFullCtrlMode}")
    print(f"  GfxclkFullCtrl:      {table.GfxclkFullCtrlMode}")
    print(f"  UclkFullCtrl:        {table.UclkFullCtrlMode}")
    print(f"  FclkFullCtrl:        {table.FclkFullCtrlMode}")

    # EDC
    print(f"\n  --- EDC ---")
    print(f"  GfxEdc:              {table.GfxEdc}")
    print(f"  GfxPccLimitControl:  {table.GfxPccLimitControl}")
    print(f"  GfxclkFmaxVmax:      {table.GfxclkFmaxVmax}")
    print(f"  GfxclkFmaxVmaxTemp:  {table.GfxclkFmaxVmaxTemperature}")

    # Spare
    spare = list(table.Spare)
    non_zero = [(i, v) for i, v in enumerate(spare) if v]
    if non_zero:
        print(f"\n  Spare (non-zero):    {non_zero}")

    print(f"\n{'=' * 60}")


# ---------------------------------------------------------------------------
# OdController -- high-level read/modify/write interface
# ---------------------------------------------------------------------------

class OdController:
    """
    High-level OverDrive table controller.

    Reads the current OD table from SMU, modifies fields, and writes
    it back.  Uses the DMA buffer + SMU table transfer protocol.

    Usage:
        from dma_buf import DmaBuffer
        from smu import SmuCmd, create_smu
        from od_table import OdController

        wr0, inpout, mmio, smu = create_smu()
        buf = DmaBuffer()
        od = OdController(smu, buf)

        # Read current table
        table = od.read_table()
        dump_od_table(table)

        # Set GFX clock offset
        od.set_gfxclk_offset(100)   # +100 MHz

        od.close()
    """

    def __init__(self, smu, dma_buf):
        """
        Args:
            smu:     SmuCmd instance.
            dma_buf: DmaBuffer instance (at least PAGE_SIZE bytes).
        """
        self._smu = smu
        self._buf = dma_buf
        self._addr_set = False

    def _ensure_dram_addr(self):
        """Set the SMU's DRAM address to our DMA buffer (if not already done)."""
        if not self._addr_set:
            self._smu.set_dram_addr(self._buf.phys_addr)
            self._addr_set = True

    def read_table(self):
        """
        Read the current OverDrive table from SMU firmware.

        Returns:
            OverDriveTable_t instance with current values.
        """
        self._ensure_dram_addr()

        # Zero the buffer first so we can detect if SMU actually wrote
        self._buf.zero()

        # Ask SMU to write its OD table into our buffer
        self._smu.transfer_table_from_smu(TABLE_OVERDRIVE)

        # Parse the struct from the buffer
        table = self._buf.read_struct(OverDriveTable_t)
        return table

    def write_table(self, table):
        """
        Write a modified OverDrive table to SMU firmware.

        Args:
            table: OverDriveTable_t instance with desired values.
                   FeatureCtrlMask must have the appropriate bits set
                   for the fields you want the SMU to apply.
        """
        self._ensure_dram_addr()

        # Write struct to DMA buffer
        self._buf.write_struct(table)

        # Tell SMU to read from our buffer and apply
        self._smu.transfer_table_to_smu(TABLE_OVERDRIVE)

    def _read_modify_write(self, modify_fn):
        """
        Read current table, apply modifications, write back.

        Args:
            modify_fn: Callable(table) that modifies the table in-place
                       and sets appropriate FeatureCtrlMask bits.
        """
        table = self.read_table()
        modify_fn(table)
        self.write_table(table)

    # ---- Convenience setters ----

    def set_gfxclk_offset(self, offset_mhz):
        """
        Set GFX clock frequency offset.

        Args:
            offset_mhz: Signed offset in MHz (e.g. +100 or -50).
        """
        def modify(t):
            t.GfxclkFoffset = offset_mhz
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFXCLK_BIT)
        self._read_modify_write(modify)
        print(f"[OD] GfxclkFoffset set to {offset_mhz} MHz")

    def set_uclk_range(self, fmin=None, fmax=None):
        """
        Set memory clock (UCLK) frequency range.

        Args:
            fmin: Minimum UCLK in MHz (None = don't change).
            fmax: Maximum UCLK in MHz (None = don't change).
        """
        def modify(t):
            if fmin is not None:
                t.UclkFmin = fmin
            if fmax is not None:
                t.UclkFmax = fmax
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_UCLK_BIT)
        self._read_modify_write(modify)
        parts = []
        if fmin is not None:
            parts.append(f"min={fmin}")
        if fmax is not None:
            parts.append(f"max={fmax}")
        print(f"[OD] UCLK range set: {', '.join(parts)} MHz")

    def set_fclk_range(self, fmin=None, fmax=None):
        """
        Set fabric clock (FCLK) frequency range.

        Args:
            fmin: Minimum FCLK in MHz (None = don't change).
            fmax: Maximum FCLK in MHz (None = don't change).
        """
        def modify(t):
            if fmin is not None:
                t.FclkFmin = fmin
            if fmax is not None:
                t.FclkFmax = fmax
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_FCLK_BIT)
        self._read_modify_write(modify)
        parts = []
        if fmin is not None:
            parts.append(f"min={fmin}")
        if fmax is not None:
            parts.append(f"max={fmax}")
        print(f"[OD] FCLK range set: {', '.join(parts)} MHz")

    def set_ppt(self, pct):
        """
        Set PPT (Package Power Tracking) percentage over default.

        Args:
            pct: Percentage over default (e.g. 10 = +10%, -5 = -5%).
        """
        def modify(t):
            t.Ppt = pct
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_PPT_BIT)
        self._read_modify_write(modify)
        print(f"[OD] PPT set to {pct}% over default")

    def set_tdc(self, pct):
        """
        Set TDC (Thermal Design Current) percentage over default.

        Args:
            pct: Percentage over default.
        """
        def modify(t):
            t.Tdc = pct
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_TDC_BIT)
        self._read_modify_write(modify)
        print(f"[OD] TDC set to {pct}% over default")

    def set_fan_curve(self, temp_points, pwm_points):
        """
        Set custom fan curve.

        Args:
            temp_points: List of 6 temperature points (C, uint8).
            pwm_points:  List of 6 PWM duty cycle points (0-255, uint8).
        """
        if len(temp_points) != NUM_OD_FAN_MAX_POINTS:
            raise ValueError(f"Need {NUM_OD_FAN_MAX_POINTS} temp points")
        if len(pwm_points) != NUM_OD_FAN_MAX_POINTS:
            raise ValueError(f"Need {NUM_OD_FAN_MAX_POINTS} pwm points")

        def modify(t):
            for i in range(NUM_OD_FAN_MAX_POINTS):
                t.FanLinearTempPoints[i] = temp_points[i]
                t.FanLinearPwmPoints[i] = pwm_points[i]
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_FAN_CURVE_BIT)
        self._read_modify_write(modify)
        print(f"[OD] Fan curve set: temps={temp_points}, pwm={pwm_points}")

    def set_voltage_offset(self, offsets):
        """
        Set per-zone voltage offsets on the V/F curve.

        Args:
            offsets: List of 6 signed int16 voltage offsets (mV).
        """
        if len(offsets) != PP_NUM_OD_VF_CURVE_POINTS:
            raise ValueError(f"Need {PP_NUM_OD_VF_CURVE_POINTS} offsets")

        def modify(t):
            for i in range(PP_NUM_OD_VF_CURVE_POINTS):
                t.VoltageOffsetPerZoneBoundary[i] = offsets[i]
            t.FeatureCtrlMask |= (1 << PP_OD_FEATURE_GFX_VF_CURVE_BIT)
        self._read_modify_write(modify)
        print(f"[OD] Voltage offsets set: {offsets}")

    def restore_defaults(self):
        """
        Write a zeroed OD table to reset all overrides.

        A zeroed FeatureCtrlMask means "no changes requested", but
        uploading the zero table should reset the SMU's OD state.
        """
        table = OverDriveTable_t()  # all zeros
        self.write_table(table)
        print("[OD] Defaults restored (zeroed OD table uploaded)")

    def close(self):
        """Release resources (does NOT close the SMU or DMA buffer)."""
        self._addr_set = False


# ---------------------------------------------------------------------------
# Module info
# ---------------------------------------------------------------------------

print(f"[od_table] OverDriveTable_t size: {_OD_TABLE_SIZE} bytes "
      f"(external: {_OD_TABLE_EXT_SIZE})")
