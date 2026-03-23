"""
SMU Command Module -- Direct SMU Message Passing via SMN Bus
=============================================================

Sends commands directly to the SMU firmware through the C2PMSG mailbox
registers, bypassing the Windows AMD driver entirely.

Architecture:
    SmuCmd  -->  GpuMMIO.smn_read32 / smn_write32  -->  SMN Bus  -->  SMU FW

SMU Message Protocol (from amdgpu smu_cmn.c):
    1. Write 0 to RESP register  (clear previous response)
    2. Write parameter to PARAM register
    3. Write message ID to MSG register  (triggers SMU processing)
    4. Poll RESP register until non-zero  (with timeout)
    5. Check response: 0x01=OK, 0xFF=fail, 0xFE=unknown cmd
    6. For queries: read PARAM register to get return value

Registers (SMU v14.0.2 / RDNA4, from mp_14_0_2_offset.h):
    MSG   = C2PMSG_66 = 0x03B10A08  (MP1 base[1]=0x00EC4200 via IP discovery)
    PARAM = C2PMSG_82 = 0x03B10A48
    RESP  = C2PMSG_90 = 0x03B10A68

Requirements:
    - overclocking.mmio (GpuMMIO with working smn_read32/smn_write32)
    - Administrator privileges (WinRing0 driver)
"""

import logging
import threading
import time

from src.io.mmio import WinRing0, GpuMMIO, InpOut32

_smu_log = logging.getLogger("overclock.smu")


# ---------------------------------------------------------------------------
# C2PMSG Register Addresses (SMU v14.0.2 / RDNA4)
# ---------------------------------------------------------------------------

SMN_C2PMSG_MSG   = 0x03B10A08   # C2PMSG_66 -- message ID register
SMN_C2PMSG_PARAM = 0x03B10A48   # C2PMSG_82 -- parameter / return value
SMN_C2PMSG_RESP  = 0x03B10A68   # C2PMSG_90 -- response code


# ---------------------------------------------------------------------------
# SMU Response Codes
# ---------------------------------------------------------------------------

SMU_RESP_NONE    = 0x00  # No response yet (polling in progress)
SMU_RESP_OK      = 0x01  # Command succeeded
SMU_RESP_FAIL    = 0xFF  # Command failed
SMU_RESP_UNKNOWN = 0xFE  # Unknown / unsupported command
SMU_RESP_BUSY    = 0xFC  # SMU is busy (some firmware versions)
SMU_RESP_PREREQ  = 0xFD  # Prerequisite not met (e.g. feature not in allowed mask)

_RESP_NAMES = {
    SMU_RESP_NONE:    "NO_RESPONSE",
    SMU_RESP_OK:      "OK",
    SMU_RESP_FAIL:    "FAIL",
    SMU_RESP_UNKNOWN: "UNKNOWN_CMD",
    SMU_RESP_PREREQ:  "PREREQ_FAILED",
    SMU_RESP_BUSY:    "BUSY",
}


# ---------------------------------------------------------------------------
# PPSMC Message IDs (from smu_v14_0_2_ppsmc.h)
# ---------------------------------------------------------------------------

class PPSMC:
    """SMU Power/Performance message constants for SMU v14.0.2.

    Values from smu_v14_0_2_ppsmc.h in the Linux kernel.
    """

    # System / debug
    TestMessage                = 0x01
    GetSmuVersion              = 0x02
    GetDriverIfVersion         = 0x03

    # Feature control
    SetAllowedFeaturesMaskLow  = 0x04
    SetAllowedFeaturesMaskHigh = 0x05
    EnableAllSmuFeatures       = 0x06
    DisableAllSmuFeatures      = 0x07
    EnableSmuFeaturesLow       = 0x08
    EnableSmuFeaturesHigh      = 0x09
    DisableSmuFeaturesLow      = 0x0A
    DisableSmuFeaturesHigh     = 0x0B
    GetRunningSmuFeaturesLow   = 0x0C
    GetRunningSmuFeaturesHigh  = 0x0D

    # DRAM table transfer -- Driver path (used by AMD driver, may race)
    SetDriverDramAddrHigh      = 0x0E
    SetDriverDramAddrLow       = 0x0F
    SetToolsDramAddrHigh       = 0x10   # Tools path (separate from driver)
    SetToolsDramAddrLow        = 0x11
    TransferTableSmu2Dram      = 0x12   # Uses Driver DRAM addr
    TransferTableDram2Smu      = 0x13   # Uses Driver DRAM addr

    # DRAM table transfer -- Tools path (not touched by AMD driver)
    SetDriverDramAddr          = 0x50   # Combined high+low (single msg)
    SetToolsDramAddr           = 0x51   # Combined high+low (single msg)
    TransferTableSmu2DramWithAddr = 0x52  # Uses Tools DRAM addr
    TransferTableDram2SmuWithAddr = 0x53  # Uses Tools DRAM addr

    # DPM frequency control
    SetSoftMinByFreq           = 0x19
    SetSoftMaxByFreq           = 0x1A
    SetHardMinByFreq           = 0x1B
    SetHardMaxByFreq           = 0x1C
    GetMinDpmFreq              = 0x1D
    GetMaxDpmFreq              = 0x1E
    GetDpmFreqByIndex          = 0x1F

    # PCIe
    OverridePcieParameters     = 0x20

    # Workload / performance profiles
    SetWorkloadMask            = 0x24

    # Voltage
    GetVoltageByDpm            = 0x25

    # Video
    SetVideoFps                = 0x26

    # DC mode
    GetDcModeMaxDpmFreq        = 0x27

    # GFX power states
    AllowGfxOff                = 0x28
    DisallowGfxOff             = 0x29

    # Power limit
    SetPptLimit                = 0x32
    GetPptLimit                = 0x33

    # Notifications
    ReenableAcDcInterrupt      = 0x34
    NotifyPowerSource          = 0x35

    # Throttler
    SetTemperatureInputSelect  = 0x38
    SetThrottlerMask           = 0x3A

    # Fan
    SetMGpuFanBoostLimitRpm    = 0x3C

    # DCS
    AllowGfxDcs                = 0x43
    DisallowGfxDcs             = 0x44

    # Misc control
    EnableAudioStutterWA       = 0x45
    PowerUpUmsch               = 0x46
    PowerDownUmsch             = 0x47
    SetDcsArch                 = 0x48
    SetNumBadMemoryPagesRetired = 0x4A
    SetBadMemoryPagesRetiredFlagsPerChannel = 0x4B
    SetPriorityDeltaGain       = 0x4C
    AllowIHHostInterrupt       = 0x4D
    EnableShadowDpm            = 0x4E

    # 64-bit features in one call
    GetAllRunningSmuFeatures   = 0x54

    # Voltage readback
    GetSvi3Voltage             = 0x55

    # Policy / power connector
    UpdatePolicy               = 0x56
    ExtPwrConnSupport          = 0x57
    PreloadSwPstateForUclkOverDrive = 0x58

    # FW Dstate
    SetFwDstatesMask           = 0x39


# Human-readable names for message IDs (for logging)
_MSG_NAMES = {}
for _name in dir(PPSMC):
    if not _name.startswith("_"):
        _val = getattr(PPSMC, _name)
        if isinstance(_val, int):
            _MSG_NAMES[_val] = _name


# ---------------------------------------------------------------------------
# Clock IDs (PPCLK_*)
# ---------------------------------------------------------------------------

class PPCLK:
    """Power Play clock domain identifiers (PPCLK_e from smu14_driver_if_v14_0.h)."""
    GFXCLK   = 0   # Graphics core clock
    SOCCLK   = 1   # System-on-chip clock
    UCLK     = 2   # Memory (unified) clock
    FCLK     = 3   # Fabric / infinity fabric clock
    DCLK0    = 4   # Video decode clock 0
    VCLK0    = 5   # Video encode clock 0
    DISPCLK  = 6   # Display clock
    DPPCLK   = 7   # Display pipe/plane clock
    DPREFCLK = 8   # Display reference clock
    DCFCLK   = 9   # Display controller fabric clock
    DTBCLK   = 10  # Display transport block clock
    COUNT    = 11

_CLK_NAMES = {
    PPCLK.GFXCLK:   "GFXCLK",
    PPCLK.SOCCLK:   "SOCCLK",
    PPCLK.UCLK:     "UCLK",
    PPCLK.FCLK:     "FCLK",
    PPCLK.DCLK0:    "DCLK0",
    PPCLK.VCLK0:    "VCLK0",
    PPCLK.DISPCLK:  "DISPCLK",
    PPCLK.DPPCLK:   "DPPCLK",
    PPCLK.DPREFCLK: "DPREFCLK",
    PPCLK.DCFCLK:   "DCFCLK",
    PPCLK.DTBCLK:   "DTBCLK",
}


# ---------------------------------------------------------------------------
# SMU Feature Bits (from smu_v14_0_2_ppsmc.h / smu14_driver_if_v14_0.h)
# ---------------------------------------------------------------------------

class SMU_FEATURE:
    """SMU feature bitmask positions (from smu14_driver_if_v14_0.h FEATURE_*_BIT).

    These are bit positions, not masks.  Use (1 << bit) to get the mask.
    Full 64-bit bitmask: bits 0-31 = low word, bits 32-63 = high word.
    """
    # Low word (bits 0-31)
    FW_DATA_READ              = 0
    DPM_GFXCLK                = 1
    DPM_GFX_POWER_OPTIMIZER   = 2
    DPM_UCLK                  = 3
    DPM_FCLK                  = 4
    DPM_SOCCLK                = 5
    DPM_LINK                  = 6
    DPM_DCN                   = 7
    VMEMP_SCALING             = 8
    VDDIO_MEM_SCALING         = 9
    DS_GFXCLK                 = 10
    DS_SOCCLK                 = 11
    DS_FCLK                   = 12
    DS_LCLK                   = 13
    DS_DCFCLK                 = 14
    DS_UCLK                   = 15
    GFX_ULV                   = 16
    FW_DSTATE                 = 17
    GFXOFF                    = 18
    BACO                      = 19
    MM_DPM                    = 20
    SOC_MPCLK_DS              = 21
    BACO_MPCLK_DS             = 22
    THROTTLERS                = 23
    SMARTSHIFT                = 24
    GTHR                      = 25
    ACDC                      = 26
    VR0HOT                    = 27
    FW_CTF                    = 28  # Firmware critical thermal fault (NEVER disable!)
    FAN_CONTROL               = 29
    GFX_DCS                   = 30
    GFX_READ_MARGIN           = 31

    # High word (bits 32-63)
    LED_DISPLAY               = 32
    GFXCLK_SPREAD_SPECTRUM    = 33
    OUT_OF_BAND_MONITOR       = 34
    OPTIMIZED_VMIN            = 35
    GFX_IMU                   = 36
    BOOT_TIME_CAL             = 37
    GFX_PCC_DFLL              = 38
    SOC_CG                    = 39
    DF_CSTATE                 = 40
    GFX_EDC                   = 41
    BOOT_POWER_OPT            = 42
    CLOCK_POWER_DOWN_BYPASS   = 43
    DS_VCN                    = 44
    BACO_CG                   = 45
    MEM_TEMP_READ             = 46
    ATHUB_MMHUB_PG            = 47
    SOC_PCC                   = 48
    EDC_PWRBRK                = 49
    SOC_EDC_XVMIN             = 50
    GFX_PSM_DIDT              = 51
    APT_ALL_ENABLE            = 52
    APT_SQ_THROTTLE           = 53
    APT_PF_DCS                = 54
    GFX_EDC_XVMIN             = 55
    GFX_DIDT_XVMIN            = 56
    FAN_ABNORMAL              = 57
    CLOCK_STRETCH_COMPENSATOR = 58

_FEATURE_NAMES = {}
for _name in dir(SMU_FEATURE):
    if not _name.startswith("_"):
        _val = getattr(SMU_FEATURE, _name)
        if isinstance(_val, int):
            _FEATURE_NAMES[_val] = _name

# Backward-compat alias
_FEATURE_NAMES_LOW = _FEATURE_NAMES


# ---------------------------------------------------------------------------
# Global SMU mailbox lock -- the C2PMSG registers (MSG, PARAM, RESP) are a
# single shared mailbox.  Concurrent send_msg calls from different threads
# interleave the 5-step protocol and corrupt each other's transactions,
# producing UNKNOWN_CMD / FAIL / garbled return values.
# ---------------------------------------------------------------------------

_smu_mailbox_lock = threading.Lock()

# ---------------------------------------------------------------------------
# SmuCmd Class
# ---------------------------------------------------------------------------

class SmuCmd:
    """
    SMU command interface -- send messages to SMU firmware via SMN bus.

    Usage:
        mmio = GpuMMIO(wr0, bar, pci_addr=pci, io_bar_port=io_port)
        smu = SmuCmd(mmio)

        # Read-only query
        version = smu.get_smu_version()
        max_gfx = smu.get_max_freq(PPCLK.GFXCLK)

        # Write command
        smu.set_soft_max_freq(PPCLK.GFXCLK, 3150)
    """

    def __init__(self, mmio, verbose=True):
        """
        Args:
            mmio:    GpuMMIO instance with working smn_read32/smn_write32.
            verbose: If True, print each SMU command and response.
        """
        self._mmio = mmio
        self._verbose = verbose
        self._msg_count = 0  # Total messages sent
        self.transfer_read  = PPSMC.TransferTableSmu2Dram       # 0x12 default (driver path)
        self.transfer_write = PPSMC.TransferTableDram2Smu       # 0x13 default (driver path)

    # ---- Core message protocol ----

    def send_msg(self, msg_id, param=0, timeout_ms=2000):
        """
        Send an SMU message and wait for the response.

        Protocol (from smu_cmn.c __smu_cmn_send_msg):
          1. Write 0 to RESP  (acknowledge / clear previous response)
          2. Write param to PARAM
          3. Write msg_id to MSG  (triggers SMU processing)
          4. Poll RESP until non-zero  (with timeout)
          5. Read PARAM for return value (if RESP == OK)

        Args:
            msg_id:     PPSMC message ID (e.g. PPSMC.GetSmuVersion)
            param:      32-bit parameter value (default 0)
            timeout_ms: Polling timeout in milliseconds (default 2000)

        Returns:
            (resp_code, return_value)
            - resp_code:    SMU response (SMU_RESP_OK, SMU_RESP_FAIL, etc.)
            - return_value: Value read from PARAM register after response.
                            Only meaningful when resp_code == SMU_RESP_OK
                            and the message is a query.

        Raises:
            TimeoutError: If SMU doesn't respond within timeout_ms.
            IOError:      If SMN read/write fails.
        """
        msg_name = _MSG_NAMES.get(msg_id, f"0x{msg_id:02X}")
        self._msg_count += 1

        try:
            _smu_log.info(f"[SMU] #{self._msg_count} SEND {msg_name} msg=0x{msg_id:02X} param=0x{param:08X}")
        except Exception:
            pass

        if self._verbose:
            print(f"[SMU] #{self._msg_count} {msg_name}"
                  f" (msg=0x{msg_id:02X}, param=0x{param:08X})")

        mmio = self._mmio

        with _smu_mailbox_lock:
            # Step 1: Clear RESP (write 0)
            mmio.smn_write32(SMN_C2PMSG_RESP, 0x00000000)

            # Step 2: Write parameter
            mmio.smn_write32(SMN_C2PMSG_PARAM, param & 0xFFFFFFFF)

            # Step 3: Write message ID (triggers SMU)
            mmio.smn_write32(SMN_C2PMSG_MSG, msg_id & 0xFFFFFFFF)

            # Step 4: Poll RESP until non-zero
            deadline = time.perf_counter() + timeout_ms / 1000.0
            poll_interval = 0.0001  # Start at 100us
            max_interval = 0.010   # Cap at 10ms

            resp = SMU_RESP_NONE
            while time.perf_counter() < deadline:
                resp = mmio.smn_read32(SMN_C2PMSG_RESP)
                if resp != SMU_RESP_NONE:
                    break
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, max_interval)

            if resp == SMU_RESP_NONE:
                try:
                    _smu_log.warning(f"[SMU] #{self._msg_count} TIMEOUT after {timeout_ms}ms waiting for {msg_name}")
                except Exception:
                    pass
                if self._verbose:
                    print(f"[SMU] TIMEOUT after {timeout_ms}ms waiting for response"
                          f" to {msg_name}")
                raise TimeoutError(
                    f"SMU did not respond to {msg_name} (0x{msg_id:02X}) "
                    f"within {timeout_ms}ms"
                )

            # Step 5: Read return value from PARAM
            return_value = mmio.smn_read32(SMN_C2PMSG_PARAM)

        resp_name = _RESP_NAMES.get(resp, f"0x{resp:02X}")
        try:
            _smu_log.info(f"[SMU] #{self._msg_count} RESP {resp_name} (0x{resp:02X}) return=0x{return_value:08X}")
        except Exception:
            pass

        if self._verbose:
            if resp == SMU_RESP_OK:
                print(f"[SMU]   -> {resp_name}, return=0x{return_value:08X}")
            else:
                print(f"[SMU]   -> {resp_name} (resp=0x{resp:02X})")

        return resp, return_value

    def send_msg_ok(self, msg_id, param=0, timeout_ms=2000):
        """
        Send an SMU message and return the value, or raise on failure.

        Convenience wrapper around send_msg() that raises RuntimeError
        if the response is not OK.

        Returns:
            The return value (from PARAM register) on success.
        """
        resp, value = self.send_msg(msg_id, param, timeout_ms)
        if resp != SMU_RESP_OK:
            msg_name = _MSG_NAMES.get(msg_id, f"0x{msg_id:02X}")
            resp_name = _RESP_NAMES.get(resp, f"0x{resp:02X}")
            raise RuntimeError(
                f"SMU command {msg_name} failed: {resp_name} (0x{resp:02X})"
            )
        return value

    # ---- Read-only query helpers ----

    def get_smu_version(self):
        """
        Query SMU firmware version.

        Returns:
            (major, minor, revision, debug) tuple, or raw 32-bit value
            if the format is unexpected.
        """
        raw = self.send_msg_ok(PPSMC.GetSmuVersion)
        # Common format: major.minor.rev.debug in bytes
        major = (raw >> 24) & 0xFF
        minor = (raw >> 16) & 0xFF
        rev   = (raw >> 8) & 0xFF
        debug = raw & 0xFF
        return (major, minor, rev, debug)

    def get_driver_if_version(self):
        """Query SMU driver interface version."""
        return self.send_msg_ok(PPSMC.GetDriverIfVersion)

    def get_min_freq(self, clk_id):
        """
        Query minimum DPM frequency for a clock domain.

        Args:
            clk_id: PPCLK.GFXCLK, PPCLK.UCLK, etc.

        Returns:
            Frequency in MHz.
        """
        param = (clk_id & 0xFFFF) << 16
        return self.send_msg_ok(PPSMC.GetMinDpmFreq, param)

    def get_max_freq(self, clk_id):
        """
        Query maximum DPM frequency for a clock domain.

        Args:
            clk_id: PPCLK.GFXCLK, PPCLK.UCLK, etc.

        Returns:
            Frequency in MHz.
        """
        param = (clk_id & 0xFFFF) << 16
        return self.send_msg_ok(PPSMC.GetMaxDpmFreq, param)

    def get_ppt_limit(self):
        """
        Query current PPT (Package Power Tracking) limit.

        Returns:
            Power limit in watts (or milliwatts, depending on FW).
        """
        return self.send_msg_ok(PPSMC.GetPptLimit)

    def get_running_features(self):
        """
        Query the bitmask of currently enabled SMU features.

        Returns:
            64-bit bitmask (low | high << 32).
        """
        low = self.send_msg_ok(PPSMC.GetRunningSmuFeaturesLow)
        try:
            high = self.send_msg_ok(PPSMC.GetRunningSmuFeaturesHigh)
        except (RuntimeError, TimeoutError):
            high = 0
        return low | (high << 32)

    def decode_features(self, bitmask):
        """
        Decode a feature bitmask into human-readable names.

        Args:
            bitmask: 64-bit feature bitmask from get_running_features().

        Returns:
            List of (bit_position, feature_name, enabled) tuples for enabled bits.
        """
        result = []
        for bit in range(64):
            enabled = bool(bitmask & (1 << bit))
            name = _FEATURE_NAMES.get(bit, f"BIT_{bit}")
            if enabled:
                result.append((bit, name, True))
        return result

    def get_dc_mode_max_freq(self, clk_id):
        """Query maximum DPM frequency in DC power mode for a clock domain."""
        param = (clk_id & 0xFFFF) << 16
        return self.send_msg_ok(PPSMC.GetDcModeMaxDpmFreq, param)

    # ---- Clock / power control helpers ----

    def set_soft_min_freq(self, clk_id, freq_mhz):
        """
        Set soft minimum frequency for a clock domain.

        Args:
            clk_id:   PPCLK.GFXCLK, etc.
            freq_mhz: Frequency in MHz.
        """
        param = ((clk_id & 0xFFFF) << 16) | (freq_mhz & 0xFFFF)
        return self.send_msg_ok(PPSMC.SetSoftMinByFreq, param)

    def set_soft_max_freq(self, clk_id, freq_mhz):
        """
        Set soft maximum frequency for a clock domain.

        Args:
            clk_id:   PPCLK.GFXCLK, etc.
            freq_mhz: Frequency in MHz.
        """
        param = ((clk_id & 0xFFFF) << 16) | (freq_mhz & 0xFFFF)
        return self.send_msg_ok(PPSMC.SetSoftMaxByFreq, param)

    def set_hard_max_freq(self, clk_id, freq_mhz):
        """
        Set hard maximum frequency for a clock domain.

        The hard max is the absolute upper limit; the soft max cannot
        exceed it.

        Args:
            clk_id:   PPCLK.GFXCLK, etc.
            freq_mhz: Frequency in MHz.
        """
        param = ((clk_id & 0xFFFF) << 16) | (freq_mhz & 0xFFFF)
        return self.send_msg_ok(PPSMC.SetHardMaxByFreq, param)

    def set_hard_min_freq(self, clk_id, freq_mhz):
        """
        Set hard minimum frequency for a clock domain.

        Args:
            clk_id:   PPCLK.GFXCLK, etc.
            freq_mhz: Frequency in MHz.
        """
        param = ((clk_id & 0xFFFF) << 16) | (freq_mhz & 0xFFFF)
        return self.send_msg_ok(PPSMC.SetHardMinByFreq, param)

    def set_ppt_limit(self, watts):
        """
        Set PPT (Package Power Tracking) limit.

        Args:
            watts: Power limit in watts.
        """
        return self.send_msg_ok(PPSMC.SetPptLimit, watts & 0xFFFFFFFF)

    def disallow_gfx_off(self):
        """Prevent GPU from entering GFXOFF idle state."""
        return self.send_msg_ok(PPSMC.DisallowGfxOff)

    def allow_gfx_off(self):
        """Re-enable GFXOFF idle state."""
        return self.send_msg_ok(PPSMC.AllowGfxOff)

    def allow_gfx_dcs(self):
        """Allow GFX Dynamic Clock Spreading."""
        return self.send_msg_ok(PPSMC.AllowGfxDcs)

    def disallow_gfx_dcs(self):
        """Disallow GFX Dynamic Clock Spreading."""
        return self.send_msg_ok(PPSMC.DisallowGfxDcs)

    def enable_all_features(self):
        """Send EnableAllSmuFeatures — activates all features the allowed mask permits.

        Returns:
            (resp_code, return_value) tuple from send_msg.
        """
        return self.send_msg(PPSMC.EnableAllSmuFeatures, 0)

    def disable_all_features(self):
        """Send DisableAllSmuFeatures — disables all features the allowed mask permits.

        Returns:
            (resp_code, return_value) tuple from send_msg.
        """
        return self.send_msg(PPSMC.DisableAllSmuFeatures, 0)

    def set_allowed_features_mask(self, low=0xFFFFFFFF, high=0xFFFFFFFF):
        """
        Set the SMU Allowed Features Mask (low and high 32-bit words).

        The allowed mask controls which features the SMU will permit
        enable/disable commands to change.  The Windows driver sets a
        restrictive mask during init; calling this with 0xFFFFFFFF for
        both halves unlocks all 64 feature bits (matching the Linux
        driver's behaviour for SMU v14.0.2).

        Must be called before enable_features_*/disable_features_* if
        the target feature bit is not already in the allowed mask.

        Args:
            low:  Allowed mask for bits 0-31  (default 0xFFFFFFFF = all).
            high: Allowed mask for bits 32-63 (default 0xFFFFFFFF = all).

        Returns:
            (resp_low, resp_high) tuple of raw SMU responses.

        Raises:
            RuntimeError: If either message is rejected by the SMU.
        """
        resp_lo = self.send_msg_ok(PPSMC.SetAllowedFeaturesMaskLow,
                                   low & 0xFFFFFFFF)
        resp_hi = self.send_msg_ok(PPSMC.SetAllowedFeaturesMaskHigh,
                                   high & 0xFFFFFFFF)
        _smu_log.info(
            f"[SMU] Allowed features mask set: "
            f"low=0x{low & 0xFFFFFFFF:08X} high=0x{high & 0xFFFFFFFF:08X}"
        )
        return resp_lo, resp_hi

    def disable_features_low(self, bitmask):
        """
        Disable SMU features by bitmask (low 32 bits).

        Args:
            bitmask: Bits to disable (e.g. (1 << SMU_FEATURE.DS_GFXCLK))
        """
        return self.send_msg_ok(PPSMC.DisableSmuFeaturesLow,
                                bitmask & 0xFFFFFFFF)

    def enable_features_low(self, bitmask):
        """
        Enable SMU features by bitmask (low 32 bits).

        Args:
            bitmask: Bits to enable
        """
        return self.send_msg_ok(PPSMC.EnableSmuFeaturesLow,
                                bitmask & 0xFFFFFFFF)

    def disable_features_high(self, bitmask):
        """Disable SMU features by bitmask (high 32 bits, bits 32-63)."""
        return self.send_msg_ok(PPSMC.DisableSmuFeaturesHigh,
                                bitmask & 0xFFFFFFFF)

    def enable_features_high(self, bitmask):
        """Enable SMU features by bitmask (high 32 bits, bits 32-63)."""
        return self.send_msg_ok(PPSMC.EnableSmuFeaturesHigh,
                                bitmask & 0xFFFFFFFF)

    # ---- Workload profile ----

    def set_workload_mask(self, profile_index):
        """
        Set active workload/performance profile.

        Args:
            profile_index: WORKLOAD_PROFILE index (0-7).
                0=Default, 1=3D Fullscreen, 2=Power Saving,
                3=Video, 4=VR, 5=Compute, 6=Custom, 7=Window 3D
        """
        mask = 1 << profile_index
        return self.send_msg_ok(PPSMC.SetWorkloadMask, mask)

    # ---- Voltage readback ----

    def get_voltage(self):
        """
        Read current GFX voltage via SVI3 telemetry.

        Returns:
            Voltage in mV, or None if unsupported.
        """
        try:
            # SVI_PLANE_VDD_GFX = 0
            raw = self.send_msg_ok(PPSMC.GetSvi3Voltage, 0)
            return raw
        except (RuntimeError, TimeoutError):
            return None

    # ---- HDP cache coherency ----

    def hdp_flush(self):
        """Flush+invalidate the HDP cache to ensure CPU/GPU coherency around DMA transfers."""
        self._mmio.hdp_flush()

    # ---- Tools DRAM path (separate from Windows driver's address) ----

    def setup_tools_dram(self, mc_addr):
        """Set the Tools DRAM address and configure transfer_read/write to use Tools msgs.

        The Tools DRAM address (0x10/0x11) is independent of the Driver DRAM
        address (0x0E/0x0F).  The Windows driver never touches the Tools address,
        so we can freely set it and use 0x52/0x53 without racing the driver.

        Args:
            mc_addr: 64-bit GPU MC address of the DMA buffer.

        Returns:
            (old_read, old_write) — the previous transfer_read/write msg IDs
            so the caller can restore them if needed.
        """
        old_read = self.transfer_read
        old_write = self.transfer_write

        self.set_dram_addr(mc_addr, use_tools=True)

        self.transfer_read = PPSMC.TransferTableSmu2DramWithAddr
        self.transfer_write = PPSMC.TransferTableDram2SmuWithAddr

        if self._verbose:
            print(f"[SMU] Tools DRAM path active: read=0x{self.transfer_read:02X} "
                  f"write=0x{self.transfer_write:02X}")

        return old_read, old_write

    def restore_transfer_msgs(self, old_read, old_write):
        """Restore transfer_read/write to previously saved msg IDs."""
        self.transfer_read = old_read
        self.transfer_write = old_write

    def transfer_table_tools_to_smu(self, table_id):
        """Write a table from DRAM to SMU using the Tools path (msg 0x53).

        Must call setup_tools_dram() first to set the address.
        Does NOT modify self.transfer_write — uses the Tools msg directly.
        """
        return self.send_msg(PPSMC.TransferTableDram2SmuWithAddr, table_id & 0xFFFF)

    def transfer_table_tools_from_smu(self, table_id):
        """Read a table from SMU to DRAM using the Tools path (msg 0x52).

        Must call setup_tools_dram() first to set the address.
        Does NOT modify self.transfer_read — uses the Tools msg directly.
        """
        return self.send_msg(PPSMC.TransferTableSmu2DramWithAddr, table_id & 0xFFFF)

    # ---- DRAM table transfer ----

    def set_dram_addr(self, mc_addr, use_tools=True):
        """
        Set the DRAM address for table transfers.

        Tells the SMU firmware where to read/write table data via DMA.
        The address is a GPU MC address (VRAM offset), sent as two
        32-bit halves.

        Args:
            mc_addr:   64-bit GPU MC address (VRAM offset for dGPUs).
            use_tools: If True (default), set the Tools DRAM address
                       (0x10/0x11) which is separate from the Windows
                       driver's address and won't be overwritten.
                       If False, set the Driver DRAM address (0x0E/0x0F).

        Raises:
            RuntimeError: If either message fails.
        """
        hi = (mc_addr >> 32) & 0xFFFFFFFF
        lo = mc_addr & 0xFFFFFFFF

        if use_tools:
            self.send_msg_ok(PPSMC.SetToolsDramAddrHigh, hi)
            self.send_msg_ok(PPSMC.SetToolsDramAddrLow, lo)
            path = "Tools"
        else:
            self.send_msg_ok(PPSMC.SetDriverDramAddrHigh, hi)
            self.send_msg_ok(PPSMC.SetDriverDramAddrLow, lo)
            path = "Driver"

        if self._verbose:
            print(f"[SMU] {path} DRAM addr set: 0x{mc_addr:016X} "
                  f"(hi=0x{hi:08X}, lo=0x{lo:08X})")

    def transfer_table_to_smu(self, table_id, use_tools=None):
        """
        Transfer a table from DRAM to SMU firmware.

        The buffer at the previously set DRAM address must contain the
        serialized table data.  The SMU reads from the buffer and applies
        the settings.

        Args:
            table_id:  Table identifier (e.g. TABLE_OVERDRIVE = 8).
            use_tools: Deprecated; ignored.  Always uses self.transfer_write
                       (0x13 TransferTableDram2Smu by default, set during
                       DMA buffer discovery).

        Raises:
            RuntimeError: If the transfer fails.
        """
        return self.send_msg_ok(self.transfer_write, table_id & 0xFFFF)

    def transfer_table_from_smu(self, table_id, use_tools=None):
        """
        Transfer a table from SMU firmware to DRAM.

        The SMU writes the current table data to the buffer at the
        previously set DRAM address.

        Args:
            table_id:  Table identifier (e.g. TABLE_OVERDRIVE = 8).
            use_tools: Deprecated; ignored.  Always uses self.transfer_read
                       (0x12 TransferTableSmu2Dram by default, set during
                       DMA buffer discovery).

        Raises:
            RuntimeError: If the transfer fails.
        """
        return self.send_msg_ok(self.transfer_read, table_id & 0xFFFF)

    # ---- Diagnostic / info dump ----

    def dump_state(self):
        """
        Query and print all readable SMU state.  Read-only, safe.

        Returns:
            Dict with all queried values.
        """
        state = {}
        print("\n" + "=" * 60)
        print("SMU State Dump (read-only)")
        print("=" * 60)

        # SMU Version
        try:
            ver = self.get_smu_version()
            state["smu_version"] = ver
            print(f"\n  SMU Version: {ver[0]}.{ver[1]}.{ver[2]}.{ver[3]}")
        except Exception as e:
            print(f"\n  SMU Version: FAILED ({e})")

        # Driver Interface Version
        try:
            drv_if = self.get_driver_if_version()
            state["driver_if_version"] = drv_if
            print(f"  Driver IF:   0x{drv_if:08X}")
        except Exception as e:
            print(f"  Driver IF:   FAILED ({e})")

        # DPM frequency ranges
        print(f"\n  --- Clock Frequency Ranges (MHz) ---")
        for clk_id, clk_name in sorted(_CLK_NAMES.items()):
            if clk_id > PPCLK.FCLK:
                continue  # Skip display/video clocks for brevity
            try:
                fmin = self.get_min_freq(clk_id)
                fmax = self.get_max_freq(clk_id)
                state[f"{clk_name}_min"] = fmin
                state[f"{clk_name}_max"] = fmax
                print(f"  {clk_name:10s}  min={fmin:5d}  max={fmax:5d}")
            except Exception as e:
                print(f"  {clk_name:10s}  FAILED ({e})")

        # PPT limit
        try:
            ppt = self.get_ppt_limit()
            state["ppt_limit"] = ppt
            print(f"\n  PPT Limit:   {ppt} W")
        except Exception as e:
            print(f"\n  PPT Limit:   FAILED ({e})")

        # Running features
        try:
            features = self.get_running_features()
            state["features"] = features
            print(f"\n  --- Running SMU Features (0x{features:016X}) ---")
            enabled = self.decode_features(features)
            for bit, name, _ in enabled:
                print(f"    [{bit:2d}] {name}")
            if not enabled:
                print(f"    (none enabled -- may indicate query failure)")
        except Exception as e:
            print(f"\n  Running Features: FAILED ({e})")

        print(f"\n{'=' * 60}")
        return state

    # ---- Register peek (diagnostic) ----

    def peek_registers(self):
        """
        Read and print the current values of all three C2PMSG registers.
        Useful for debugging without sending any commands.
        """
        mmio = self._mmio
        msg  = mmio.smn_read32(SMN_C2PMSG_MSG)
        par  = mmio.smn_read32(SMN_C2PMSG_PARAM)
        resp = mmio.smn_read32(SMN_C2PMSG_RESP)

        msg_name = _MSG_NAMES.get(msg, f"?")
        resp_name = _RESP_NAMES.get(resp, f"0x{resp:02X}")

        print(f"[SMU] Registers:")
        print(f"  MSG   (0x{SMN_C2PMSG_MSG:08X}) = 0x{msg:08X}  ({msg_name})")
        print(f"  PARAM (0x{SMN_C2PMSG_PARAM:08X}) = 0x{par:08X}")
        print(f"  RESP  (0x{SMN_C2PMSG_RESP:08X}) = 0x{resp:08X}  ({resp_name})")
        return msg, par, resp


# ---------------------------------------------------------------------------
# Convenience: auto-detect GPU and create SmuCmd instance
# ---------------------------------------------------------------------------

def create_smu(verbose=True):
    """
    Auto-detect GPU, initialize MMIO, and return (wr0, inpout, mmio, smu, vram_bar).

    Tries two backends:
      1. WinRing0 (patched) + InpOut32  -- needs test signing enabled
      2. InpOut32 only                  -- works with signature enforcement

    InpOut32-only mode uses I/O ports 0xCF8/0xCFC for PCI config space
    access and GetPhysLong/SetPhysLong for physical memory.  This works
    with the signed inpoutx64.dll even without test signing mode.

    Usage:
        wr0, inpout, mmio, smu, vram_bar = create_smu()
        try:
            smu.dump_state()
        finally:
            mmio.close()
            if inpout:
                inpout.close()
            if wr0:
                wr0.close()
    """
    wr0 = None
    inpout = None

    # Always load InpOut32 (signed driver, always works)
    try:
        inpout = InpOut32()
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[InpOut32] Not available: {e}")

    # Try WinRing0 (may fail if signature enforcement is on)
    try:
        wr0 = WinRing0(prefer_patched=True)
    except (FileNotFoundError, RuntimeError, OSError) as e:
        if inpout is not None:
            print(f"[WR0] Not available ({e})")
            print(f"[WR0] Using InpOut32-only mode (signed driver, no test signing needed)")
        else:
            raise RuntimeError(
                "Neither WinRing0 nor InpOut32 available.\n"
                f"WinRing0: {e}\n"
                "Place inpoutx64.dll in the drivers/ directory."
            )

    # Find GPU and create MMIO
    # Use WinRing0 for PCI enumeration if available, else InpOut32
    pci_scanner = wr0 if wr0 is not None else inpout
    pci_addr, bar, io_port, vram_bar = GpuMMIO.find_gpu_bar(pci_scanner)
    mmio = GpuMMIO(wr0, bar, pci_addr=pci_addr, io_bar_port=io_port,
                   inpout=inpout)
    smu = SmuCmd(mmio, verbose=verbose)
    return wr0, inpout, mmio, smu, vram_bar
