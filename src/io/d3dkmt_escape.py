"""
D3DKMTEscape Client — AMD WDDM Escape Interface (v2 Protocol)
==============================================================

Sends vendor-private escape commands to amdkmdag.sys via the WDDM
D3DKMTEscape API.  This is the same mechanism AMD Adrenalin uses for
OverDrive control at runtime.

v2 protocol call chain (confirmed via Frida capture 2026-03-19)::

    Python (this module)
      -> gdi32!D3DKMTEscape(D3DKMT_ESCAPE_DRIVERPRIVATE)
        -> dxgkrnl.sys
          -> amdkmdag.sys!DxgkDdiEscape (FUN_140232300)
            -> vtable[5] adapter escape handler
              -> v2 envelope parser (version=2, CWDDE at +0x0CC)
                -> CWDDE/CWDDEPM command dispatch
                  -> OD8 handlers, display queries, etc.

v2 escape buffer layout (replaces the legacy ATID protocol)::

    +0x000  Protocol header (72 B)   {version=2, module=0x00010002, reserved}
    +0x048  Context block  (132 B)   {buf_size, 0x80, 0x10000, adapter_id, 5}
    +0x0CC  CWDDE command  (var)     {block_size, block_size, command_code, ...}
    after   Response area  (var)     driver writes output here (in-place)

The old ATID signature (0x44495441) is NOT used on RDNA 4.  Legacy ATID
functions are retained for reference but marked deprecated.

Requirements:
    - Windows 10+ (WDDM 2.x)
    - AMD GPU with amdkmdag.sys loaded
    - No admin privileges required for D3DKMTEscape
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.io.escape_structures import ATID_SIGNATURE, PpOdFeature

_log = logging.getLogger("overclock.d3dkmt")


# ── Win32 Constants ──────────────────────────────────────────────────────

NTSTATUS = ctypes.c_long
STATUS_SUCCESS = 0

D3DKMT_ESCAPE_DRIVERPRIVATE = 0

CR_SUCCESS = 0
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0

_NTSTATUS_NAMES: Dict[int, str] = {
    0xC0000001: "STATUS_UNSUCCESSFUL",
    0xC0000002: "STATUS_NOT_IMPLEMENTED",
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0xC000000D: "STATUS_INVALID_PARAMETER",
    0xC0000023: "STATUS_BUFFER_TOO_SMALL",
    0xC0000034: "STATUS_OBJECT_NAME_NOT_FOUND",
    0xC000009A: "STATUS_INSUFFICIENT_RESOURCES",
    0xC00000BB: "STATUS_NOT_SUPPORTED",
    0xC0000225: "STATUS_NOT_FOUND",
}


# ── GUID ─────────────────────────────────────────────────────────────────

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

# {5b45201d-f2f2-4f3b-85bb-30ff1f953599}
GUID_DISPLAY_DEVICE_ARRIVAL = _GUID(
    0x5b45201d, 0xf2f2, 0x4f3b,
    (ctypes.c_ubyte * 8)(0x85, 0xbb, 0x30, 0xff, 0x1f, 0x95, 0x35, 0x99),
)


# ── D3DKMT Structures (x64 layout, from d3dkmthk.h) ─────────────────────

class _LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", wt.DWORD),
        ("HighPart", wt.LONG),
    ]


class _D3DKMT_OPENADAPTERFROMDEVICENAME(ctypes.Structure):
    """D3DKMT_OPENADAPTERFROMDEVICENAME — open adapter by device interface path.

    x64 layout:
        +0x00  PCWSTR          pDeviceName  (8 bytes, in)
        +0x08  D3DKMT_HANDLE   hAdapter     (4 bytes, out)
        +0x0C  LUID            AdapterLuid  (8 bytes, out)

    NOTE: On some Windows 10/11 builds this API rejects PnP device
    interface paths (STATUS_INVALID_PARAMETER).  Prefer
    D3DKMTOpenAdapterFromGdiDisplayName or D3DKMTOpenAdapterFromLuid.
    """
    _fields_ = [
        ("pDeviceName", ctypes.c_wchar_p),
        ("hAdapter", wt.UINT),
        ("AdapterLuid", _LUID),
    ]


class _D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME(ctypes.Structure):
    r"""D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME — open adapter by GDI name.

    Takes a GDI display name like ``\\.\DISPLAY1`` (from
    EnumDisplayDevices).  This is the most reliable adapter-open path
    on modern Windows.

    x64 layout:
        +0x00  WCHAR[32]       DeviceName   (64 bytes, in)
        +0x40  D3DKMT_HANDLE   hAdapter     (4 bytes, out)
        +0x44  LUID            AdapterLuid  (8 bytes, out)
        +0x4C  UINT            VidPnSourceId (4 bytes, out)
    """
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32),
        ("hAdapter", wt.UINT),
        ("AdapterLuid", _LUID),
        ("VidPnSourceId", wt.UINT),
    ]


class _D3DKMT_OPENADAPTERFROMLUID(ctypes.Structure):
    """D3DKMT_OPENADAPTERFROMLUID — open adapter by LUID.

    x64 layout:
        +0x00  LUID            AdapterLuid  (8 bytes, in)
        +0x08  D3DKMT_HANDLE   hAdapter     (4 bytes, out)
    """
    _fields_ = [
        ("AdapterLuid", _LUID),
        ("hAdapter", wt.UINT),
    ]


class _D3DKMT_CLOSEADAPTER(ctypes.Structure):
    _fields_ = [
        ("hAdapter", wt.UINT),
    ]


class _D3DKMT_CREATEDEVICE(ctypes.Structure):
    """D3DKMT_CREATEDEVICE — create a device on an adapter.

    x64 layout::

        +0x00  union { D3DKMT_HANDLE hAdapter | VOID *pAdapter }  (8 bytes, in)
        +0x08  D3DKMT_CREATEDEVICEFLAGS Flags                     (4 bytes, in)
        +0x0C  D3DKMT_HANDLE hDevice                              (4 bytes, out)
        +0x10  VOID *pCommandBuffer                               (8 bytes, out)
        +0x18  UINT  CommandBufferSize                             (4 bytes, out)
        +0x20  D3DDDI_ALLOCATIONLIST *pAllocationList              (8 bytes, out)
        +0x28  UINT  AllocationListSize                            (4 bytes, out)
        +0x30  D3DDDI_PATCHLOCATIONLIST *pPatchLocationList        (8 bytes, out)
        +0x38  UINT  PatchLocationListSize                         (4 bytes, out)

    The union hAdapter/pAdapter is pointer-sized on x64.  We use c_void_p
    to get the right 8-byte width and write the adapter handle into it.
    """
    _fields_ = [
        ("hAdapter", ctypes.c_void_p),
        ("Flags", wt.UINT),
        ("hDevice", wt.UINT),
        ("pCommandBuffer", ctypes.c_void_p),
        ("CommandBufferSize", wt.UINT),
        ("pAllocationList", ctypes.c_void_p),
        ("AllocationListSize", wt.UINT),
        ("pPatchLocationList", ctypes.c_void_p),
        ("PatchLocationListSize", wt.UINT),
    ]


class _D3DKMT_DESTROYDEVICE(ctypes.Structure):
    """D3DKMT_DESTROYDEVICE — destroy a previously created device."""
    _fields_ = [
        ("hDevice", wt.UINT),
    ]


class _D3DKMT_ESCAPE(ctypes.Structure):
    """D3DKMT_ESCAPE — send vendor-private escape to display miniport.

    x64 layout:
        +0x00  D3DKMT_HANDLE      hAdapter               (4 bytes)
        +0x04  D3DKMT_HANDLE      hDevice                (4 bytes)
        +0x08  D3DKMT_ESCAPETYPE  Type                   (4 bytes)
        +0x0C  D3DDDI_ESCAPEFLAGS Flags                  (4 bytes)
        +0x10  VOID*              pPrivateDriverData     (8 bytes)
        +0x18  UINT               PrivateDriverDataSize  (4 bytes)
        +0x1C  D3DKMT_HANDLE      hContext               (4 bytes)
    Total: 0x20 (32 bytes)
    """
    _fields_ = [
        ("hAdapter", wt.UINT),
        ("hDevice", wt.UINT),
        ("Type", wt.UINT),
        ("Flags", wt.UINT),
        ("pPrivateDriverData", ctypes.c_void_p),
        ("PrivateDriverDataSize", wt.UINT),
        ("hContext", wt.UINT),
    ]


class _D3DKMT_ADAPTERINFO(ctypes.Structure):
    _fields_ = [
        ("hAdapter", wt.UINT),
        ("AdapterLuid", _LUID),
        ("NumOfSources", wt.ULONG),
        ("bPrecisePresentRegionsPreferred", wt.BOOL),
    ]


class _D3DKMT_ENUMADAPTERS3(ctypes.Structure):
    _fields_ = [
        ("Filter", ctypes.c_uint64),
        ("NumAdapters", wt.UINT),
        ("pAdapters", ctypes.POINTER(_D3DKMT_ADAPTERINFO)),
    ]


# ── GDI32 D3DKMT API Bindings ───────────────────────────────────────────

_gdi32 = ctypes.windll.gdi32


def _bind(name, restype, argtypes):
    """Bind a gdi32 export with type annotations."""
    fn = getattr(_gdi32, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_D3DKMTOpenAdapterFromDeviceName = _bind(
    "D3DKMTOpenAdapterFromDeviceName", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_OPENADAPTERFROMDEVICENAME)])

_D3DKMTOpenAdapterFromGdiDisplayName = _bind(
    "D3DKMTOpenAdapterFromGdiDisplayName", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME)])

_D3DKMTOpenAdapterFromLuid = _bind(
    "D3DKMTOpenAdapterFromLuid", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_OPENADAPTERFROMLUID)])

_D3DKMTCloseAdapter = _bind(
    "D3DKMTCloseAdapter", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_CLOSEADAPTER)])

_D3DKMTCreateDevice = _bind(
    "D3DKMTCreateDevice", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_CREATEDEVICE)])

_D3DKMTDestroyDevice = _bind(
    "D3DKMTDestroyDevice", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_DESTROYDEVICE)])

_D3DKMTEscape = _bind(
    "D3DKMTEscape", NTSTATUS,
    [ctypes.POINTER(_D3DKMT_ESCAPE)])

try:
    _D3DKMTEnumAdapters3 = _bind(
        "D3DKMTEnumAdapters3", NTSTATUS,
        [ctypes.POINTER(_D3DKMT_ENUMADAPTERS3)])
except AttributeError:
    _D3DKMTEnumAdapters3 = None


# ── Error Handling ───────────────────────────────────────────────────────

class D3DKMTError(OSError):
    """Error from a D3DKMT API call with NTSTATUS code."""

    def __init__(self, api_name: str, ntstatus: int):
        self.api_name = api_name
        self.ntstatus = ntstatus & 0xFFFFFFFF
        name = _NTSTATUS_NAMES.get(self.ntstatus, "")
        detail = f" ({name})" if name else ""
        super().__init__(
            f"{api_name} failed: NTSTATUS 0x{self.ntstatus:08X}{detail}")


def _check(status: int, name: str) -> None:
    """Raise D3DKMTError if status is not STATUS_SUCCESS."""
    if status != STATUS_SUCCESS:
        raise D3DKMTError(name, status)


# ── Device Interface Enumeration (cfgmgr32) ─────────────────────────────

def find_display_device_interfaces() -> List[str]:
    """Enumerate all WDDM display adapter device interface paths.

    Uses cfgmgr32 CM_Get_Device_Interface_List with
    GUID_DISPLAY_DEVICE_ARRIVAL to find every display adapter registered
    with the system.

    Returns:
        Device interface paths, e.g.::

            \\\\?\\PCI#VEN_1002&DEV_7590&SUBSYS_...#{5b45201d-...}
    """
    cfgmgr32 = ctypes.windll.cfgmgr32

    buf_len = ctypes.c_ulong(0)
    ret = cfgmgr32.CM_Get_Device_Interface_List_SizeW(
        ctypes.byref(buf_len),
        ctypes.byref(GUID_DISPLAY_DEVICE_ARRIVAL),
        None,
        CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
    )
    if ret != CR_SUCCESS or buf_len.value == 0:
        return []

    buf = (ctypes.c_wchar * buf_len.value)()
    ret = cfgmgr32.CM_Get_Device_Interface_ListW(
        ctypes.byref(GUID_DISPLAY_DEVICE_ARRIVAL),
        None,
        buf,
        buf_len,
        CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
    )
    if ret != CR_SUCCESS:
        return []

    # REG_MULTI_SZ: null-separated strings, double-null terminated
    paths: List[str] = []
    current: List[str] = []
    for i in range(buf_len.value):
        ch = buf[i]
        if ch == "\0":
            if current:
                paths.append("".join(current))
                current = []
            else:
                break
        else:
            current.append(ch)
    return paths


def find_amd_display_devices() -> List[str]:
    """Find AMD GPU device interface paths (VEN_1002).

    Returns device interface paths suitable for D3DKMTClient.open().
    """
    return [p for p in find_display_device_interfaces()
            if "VEN_1002" in p.upper()]


# ── GDI Display Name Enumeration ────────────────────────────────────────

@dataclass
class DisplayDevice:
    """Display device from EnumDisplayDevicesW."""
    name: str
    description: str
    device_id: str
    is_active: bool
    is_primary: bool


def enumerate_display_devices() -> List[DisplayDevice]:
    r"""Enumerate display devices via Win32 EnumDisplayDevicesW.

    Returns display devices with GDI names like ``\\.\DISPLAY1``.
    The ``name`` field can be passed to D3DKMTClient.open_gdi().
    """
    user32 = ctypes.windll.user32

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", wt.DWORD),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    devices: List[DisplayDevice] = []
    for i in range(32):
        dd = _DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        devices.append(DisplayDevice(
            name=dd.DeviceName,
            description=dd.DeviceString,
            device_id=dd.DeviceID,
            is_active=bool(dd.StateFlags & 0x01),
            is_primary=bool(dd.StateFlags & 0x04),
        ))
    return devices


def find_amd_gdi_display_name() -> Optional[str]:
    r"""Find the GDI display name for the primary AMD GPU.

    Returns a GDI name like ``\\.\DISPLAY1`` for the first active AMD
    display device, or None if not found.
    """
    for dd in enumerate_display_devices():
        if "VEN_1002" in dd.device_id.upper() and dd.is_active:
            return dd.name
    for dd in enumerate_display_devices():
        if "VEN_1002" in dd.device_id.upper():
            return dd.name
    return None


# ── ATID Escape Buffer Protocol (DEPRECATED) ────────────────────────────
#
# DEPRECATED: The ATID protocol (0x44495441) is NOT used on RDNA 4.
# The v2 protocol (version=2 at +0x00) has replaced it entirely.
# These functions are retained for reference only — all new code should
# use the v2 builders below.
#
# Original notes:
# The first 4 bytes of the vendor-private buffer must be the ATID
# signature (0x44495441).  The remaining header fields are inferred from
# validation code patterns — see dist/research/driver_escape_research.md
# section 2.2.

ATID_HEADER_SIZE = 0x18  # 24 bytes (inferred)


def build_atid_escape(
    escape_code: int,
    sub_code: int = 0,
    payload: bytes = b"",
    output_size: int = 0,
) -> bytearray:
    """Build an ATID-signed escape buffer.

    Inferred header layout::

        +0x00  u32  Signature   = 0x44495441 ("ATID")
        +0x04  u32  EscapeSize  = total buffer size
        +0x08  u32  EscapeCode  = primary command (module selector)
        +0x0C  u32  SubCode     = sub-command within module
        +0x10  u32  InputSize   = payload byte count
        +0x14  u32  OutputSize  = expected output byte count
        +0x18  ...  Payload / output area

    The driver writes output back into the same buffer (in-place), so the
    returned bytearray is large enough for both input payload and output.

    Args:
        escape_code: Primary command code (selects CWDDE module).
        sub_code:    Sub-command within the module.
        payload:     Input data appended after the header.
        output_size: Minimum output area size.

    Returns:
        Mutable bytearray ready for D3DKMTClient.escape_raw().
    """
    payload_area = max(len(payload), output_size)
    total = ATID_HEADER_SIZE + payload_area
    buf = bytearray(total)
    struct.pack_into("<IIIIII", buf, 0,
                     ATID_SIGNATURE,
                     total,
                     escape_code,
                     sub_code,
                     len(payload),
                     output_size)
    if payload:
        buf[ATID_HEADER_SIZE:ATID_HEADER_SIZE + len(payload)] = payload
    return buf


def parse_atid_response(data: bytes) -> Tuple[Dict[str, int], bytes]:
    """Parse an ATID escape response buffer.

    Returns:
        (header_fields, payload_bytes) tuple.  If the buffer does not
        start with the ATID signature (driver may overwrite entirely),
        the header dict contains only ``Signature`` and the full buffer
        is returned as payload.
    """
    if len(data) < 4:
        return {}, bytes(data)
    sig = struct.unpack_from("<I", data, 0)[0]
    if sig != ATID_SIGNATURE or len(data) < ATID_HEADER_SIZE:
        return {"Signature": sig}, bytes(data)

    _, size, ecode, sub, in_sz, out_sz = struct.unpack_from("<IIIIII", data, 0)
    header = {
        "Signature": sig,
        "EscapeSize": size,
        "EscapeCode": ecode,
        "SubCode": sub,
        "InputSize": in_sz,
        "OutputSize": out_sz,
    }
    return header, bytes(data[ATID_HEADER_SIZE:])


# ── v2 Escape Buffer Protocol ───────────────────────────────────────────
#
# The RDNA 4 driver uses a version-2 escape protocol.  The ATID signature
# (0x44495441) is NOT recognized.  Buffer layout confirmed via Frida
# capture of AMD Adrenalin's cncmd.exe (2026-03-19).
#
# Layout:
#   +0x000  Protocol header (72 B)   version=2, module=0x00010002, 64B zeros
#   +0x048  Context block  (132 B)   buf_size, 0x80, 0x10000, adapter_id, 5
#   +0x0CC  CWDDE command  (var)     block_size, block_size, command_code, ...
#   after   Response area  (var)     driver writes output here

V2_PROTOCOL_VERSION  = 0x00000002
V2_MODULE_VERSION    = 0x00010002
V2_CWDDE_OFFSET      = 0x0CC
V2_CTX_SUB_HEADER    = 0x00000080
V2_CTX_CAPABILITY    = 0x00010000
V2_CTX_ADAPTER_ID    = 0x03000000  # default adapter id (no hDevice)
V2_CTX_IFACE_VER     = 0x00000005

# CWDDE command codes observed in Frida capture
CWDDE_CMD_SESSION_QUERY   = 0x00C00001  # session/capability query (256 B)
CWDDE_CMD_OD_LIMITS_READ  = 0x00C0009B  # OD limits read (996 B → 3×256 B)
CWDDE_CMD_DISPLAY_QUERY   = 0x00400103  # display/adapter query (292 B)
CWDDE_CMD_DISPLAY_CONFIG  = 0x00400146  # display config (356 B)
CWDDE_CMD_FEATURE_STATE   = 0x00400132  # feature state (420 B)
CWDDE_CMD_OD_WRITE        = 0x00C000A1  # OD8 settings apply (2076 B)
CWDDE_CMD_METRICS_READ    = 0x00C000A6  # large metrics read (2280 B)
CWDDE_CMD_EXTENDED_READ   = 0x00C000AB  # extended data read (544 B)
CWDDE_CMD_LARGE_READ      = 0x00C000A0  # very large data read (2296 B)

# SmartShift / GameMode commands (decompiled in Ghidra pass 12)
CWDDE_CMD_SMARTSHIFT_GET  = 0x00C000AF  # SmartShift_GetCurrentSettings (read)
CWDDE_CMD_SMARTSHIFT_SET  = 0x00C000B0  # SmartShift_SetDeltaGainControl (write)
CWDDE_CMD_GAMEMODE_GET    = 0x00C000B8  # GameMode_GetCurrentSettings (read)
CWDDE_CMD_GAMEMODE_SET    = 0x00C000B9  # GameMode_SetPolicyControl (write)

# CWDDEPM commands (command > 0xC08000 → cwddepm_new_path dispatch)
# Function table at RVA 0x009513A0, indexed by (command - 0xC08001).
CWDDEPM_CMD_ACTIVATE_CLIENT = 0x00C08008  # CWDDEPM entry [7]: BACO power state ctrl

# BACO control flags for CWDDEPM ActivateClient (0xC08008).
# Decompiled from FUN_14014723F0 (RVA 0x014723F0, 1647 bytes).
# The handler reads flags from param_2+4 (= CWDDE+12, buf offset 0x0D8).
BACO_FLAG_EXIT     = 0x10000   # exit BACO (power on GPU)  — DANGEROUS
BACO_FLAG_ENTER    = 0x20000   # enter BACO (power off GPU) — DANGEROUS
BACO_FLAG_EXTENDED = 0x200000  # extended mode flag (safe, no state transition)
_BACO_UNSAFE_FLAGS = BACO_FLAG_EXIT | BACO_FLAG_ENTER


def build_v2_escape(
    command_code: int,
    total_size: int,
    cwdde_block_size: int = 16,
    cwdde_params: bytes = b"",
    adapter_id: int = V2_CTX_ADAPTER_ID,
) -> bytearray:
    """Build a v2 protocol escape buffer.

    The v2 envelope has a fixed layout up to the CWDDE block at +0x0CC.
    After the CWDDE block, the format depends on the command — the caller
    is responsible for populating the response area.

    Args:
        command_code:     CWDDE command code (e.g. 0x00C0009B).
        total_size:       Total buffer size in bytes.
        cwdde_block_size: Size of the CWDDE block (minimum 12).
        cwdde_params:     Extra bytes after the 12-byte CWDDE header
                          {block_size, block_size, command_code}.
        adapter_id:       Adapter ID in context block.
    """
    min_size = V2_CWDDE_OFFSET + cwdde_block_size
    if total_size < min_size:
        raise ValueError(
            f"total_size ({total_size}) must be >= {min_size}")
    if cwdde_block_size < 12:
        raise ValueError("cwdde_block_size must be >= 12")
    if len(cwdde_params) > cwdde_block_size - 12:
        raise ValueError(
            f"cwdde_params ({len(cwdde_params)} B) exceeds "
            f"available space ({cwdde_block_size - 12} B)")

    buf = bytearray(total_size)

    struct.pack_into("<II", buf, 0x000,
                     V2_PROTOCOL_VERSION, V2_MODULE_VERSION)

    struct.pack_into("<IIIII", buf, 0x048,
                     total_size,
                     V2_CTX_SUB_HEADER,
                     V2_CTX_CAPABILITY,
                     adapter_id,
                     V2_CTX_IFACE_VER)

    struct.pack_into("<III", buf, V2_CWDDE_OFFSET,
                     cwdde_block_size,
                     cwdde_block_size,
                     command_code)
    if cwdde_params:
        off = V2_CWDDE_OFFSET + 12
        buf[off:off + len(cwdde_params)] = cwdde_params

    return buf


def build_v2_od_limits_read() -> bytearray:
    """Build a 996-byte v2 escape buffer for OD limits read (0x00C0009B).

    Response area layout (after 16-byte CWDDE block)::

        +0x0DC: u32 = 0          (reserved)
        +0x0E0: u32 = 0x300      (response data size = 768 bytes)
        +0x0E4: [768 bytes]      3 × 256-byte OD limit blocks (filled by driver)

    Total: 204 + 16 + 8 + 768 = 996 bytes.
    """
    OD_RESPONSE_DATA = 768
    CWDDE_SIZE = 16
    TOTAL = V2_CWDDE_OFFSET + CWDDE_SIZE + 8 + OD_RESPONSE_DATA

    buf = build_v2_escape(
        command_code=CWDDE_CMD_OD_LIMITS_READ,
        total_size=TOTAL,
        cwdde_block_size=CWDDE_SIZE,
        cwdde_params=struct.pack("<I", 0),
    )

    resp_hdr = V2_CWDDE_OFFSET + CWDDE_SIZE
    struct.pack_into("<II", buf, resp_hdr, 0, OD_RESPONSE_DATA)

    return buf


def build_v2_session_query(sub_query_id: int = 0) -> bytearray:
    """Build a 256-byte v2 escape buffer for session query (0x00C00001).

    The session query is actually GetFeatureStatus — each sub_query_id
    selects a different driver feature whose status is returned.  The
    handler writes a 20-byte output struct::

        param_3[0] = 0x14 (struct size)
        param_3[1] = FeatureSupported  (u32, from byte)
        param_3[2] = FeatureEnabled    (u32, from byte)
        param_3[3] = FeatureEnabledByDefault (u32, from byte)
        param_3[4] = FeatureVersion    (u32)

    CWDDE block layout (24 bytes)::

        +0x0CC: u32 = 24      (block_size)
        +0x0D0: u32 = 24      (block_size)
        +0x0D4: u32 = 0x00C00001 (command)
        +0x0D8: u32 = sub_query_id  (feature selector)
        +0x0DC: u32 = 0
        +0x0E0: u32 = 8       (output_size hint)

    Response area starts at +0x0E4 (CWDDE + 24), 20 bytes of feature
    status data.

    Args:
        sub_query_id: Feature selector (0x00–0x2A).  Adrenalin queries
                      all valid IDs at startup.  See
                      ``SESSION_QUERY_FEATURE_MAP`` for the mapping.
    """
    CWDDE_SIZE = 24
    TOTAL = 256

    buf = build_v2_escape(
        command_code=CWDDE_CMD_SESSION_QUERY,
        total_size=TOTAL,
        cwdde_block_size=CWDDE_SIZE,
        cwdde_params=struct.pack("<III", sub_query_id, 0, 8),
    )

    # Pre-populate response area to match Adrenalin's captured format.
    # The v2 envelope parser validates the response header before
    # dispatching — +0x0E8 and +0x0EC MUST be 0x14 (struct_size = 20)
    # or STATUS_INVALID_PARAMETER is returned.
    #
    # The handler then:
    #   1. Copies 20 bytes from context+0x20 to param_3 (overwrites
    #      our hints with context data containing {?, 0x14, 0x14, 0, 0})
    #   2. Sets param_3[0] = 0x14 (struct_size)
    #   3. IF feature lookup succeeds: writes param_3[1..4] with
    #      {FeatureSupported, FeatureEnabled, EnabledByDefault, Version}
    #   4. IF feature lookup fails: param_3[1..4] retain context copy
    #      values (typically 0x14 for [1] and [2], 0 for [3] and [4])
    #
    # We set param_3[0] = 0 as sentinel: if handler runs, it sets 0x14;
    # if handler doesn't run, it stays 0.
    resp_off = V2_CWDDE_OFFSET + CWDDE_SIZE  # 0x0E4
    struct.pack_into("<IIIII", buf, resp_off,
                     0,       # param_3[0]: handler sets to 0x14
                     0x14,    # param_3[1]: required by validator
                     0x14,    # param_3[2]: required by validator
                     0,       # param_3[3]
                     0)       # param_3[4]

    return buf


# ── Session Query / GetFeatureStatus Response ─────────────────────────────
#
# Decompiled from FUN_14145a044 (RVA 0x0145A044, 829 bytes).
#
# The 0x00C00001 handler is GetFeatureStatus — it takes a sub_query_id
# at param_2+4 (CWDDE+12 = buffer offset 0x0D8) and returns a 20-byte
# output struct with {struct_size, supported, enabled, enabled_by_default,
# version} for the requested feature.
#
# Adrenalin sends this 61 times at startup, querying sub_query_ids 0x00
# through 0x2A.  The handler maps each sub_query to an internal feature
# ID via a large switch statement, then calls FUN_14146c05c to retrieve
# the status from the driver's feature table.
#
# Debug string from handler (confirms field meanings):
#   "<GetFeatureStatus> Feature %u, FeatureSupported %u,
#    FeatureEnabled %u, FeatureEnabledByDefault %u, FeatureVersion %u\n"
#
# Special sub-queries:
#   0x02: checks isPPLibActive via FUN_14146cda4 + FUN_141461c80
#   0x23: checks WiFi band table support via FUN_141467fac
#   0x28: checks BxMx co-support via FUN_141467ab4

# The response area after the CWDDE block has an 8-byte format header
# {reserved(4), struct_size_hint(4)} validated by the dispatcher, then
# the handler output (param_3) starts at +8.
_SESSION_RESP_HDR_OFF = V2_CWDDE_OFFSET + 24  # 0x0E4 (format header)
_SESSION_RESP_OFF = _SESSION_RESP_HDR_OFF + 8  # 0x0EC (handler output)


@dataclass
class SessionQueryResponse:
    """Parsed response from GetFeatureStatus (0x00C00001)."""
    sub_query_id: int
    status_byte: int
    struct_size: int
    feature_supported: int
    feature_enabled: int
    feature_enabled_by_default: int
    feature_version: int
    raw: bytes

    @property
    def active(self) -> bool:
        """Feature is both supported and enabled."""
        return self.feature_supported != 0 and self.feature_enabled != 0


# Known context-copy baseline: when FUN_14146c05c fails, the handler
# does NOT overwrite param_3[1..4] — the 20-byte context copy values
# {struct_size=0x14, 1, 0, 1, 4} persist.  Verified by probing
# sub_query 0x03 (not in switch) which returns the same values.
_CTX_COPY_SIGNATURE = (0x14, 1, 0, 1, 4)


def _is_likely_context_copy(r: SessionQueryResponse) -> bool:
    """Detect if the response contains context-copy template values
    rather than real per-feature status.  Returns True if the response
    matches the known context-copy signature exactly."""
    return (r.struct_size, r.feature_supported, r.feature_enabled,
            r.feature_enabled_by_default, r.feature_version
            ) == _CTX_COPY_SIGNATURE


def parse_v2_session_query_response(
    buf: bytes,
    sub_query_id: int = 0,
) -> SessionQueryResponse:
    """Parse the GetFeatureStatus response from a 0x00C00001 escape buffer.

    The response area after the CWDDE block has an 8-byte format header
    validated by the dispatcher, then the handler writes a 20-byte struct
    at param_3::

        +0x0E4: u32 (format reserved)
        +0x0E8: u32 (struct_size hint, must be 0x14 for validation)
        +0x0EC: u32 struct_size          (0x14 = 20, set by handler)
        +0x0F0: u32 FeatureSupported     (0 or 1)
        +0x0F4: u32 FeatureEnabled       (0 or 1)
        +0x0F8: u32 FeatureEnabledByDefault (0 or 1)
        +0x0FC: u32 FeatureVersion       (integer, e.g. 4)

    Args:
        buf: The full 256-byte escape buffer after D3DKMTEscape.
        sub_query_id: The sub_query_id that was sent (for annotation).

    Returns:
        Parsed :class:`SessionQueryResponse`.
    """
    status_byte = buf[0x0094] if len(buf) > 0x0094 else 0

    raw = bytes(buf[_SESSION_RESP_OFF:_SESSION_RESP_OFF + 20])
    if len(raw) < 20:
        return SessionQueryResponse(
            sub_query_id=sub_query_id, status_byte=status_byte,
            struct_size=0, feature_supported=0, feature_enabled=0,
            feature_enabled_by_default=0, feature_version=0, raw=raw,
        )

    sz, sup, en, en_def, ver = struct.unpack_from("<IIIII", raw, 0)

    return SessionQueryResponse(
        sub_query_id=sub_query_id,
        status_byte=status_byte,
        struct_size=sz,
        feature_supported=sup,
        feature_enabled=en,
        feature_enabled_by_default=en_def,
        feature_version=ver,
        raw=raw,
    )


# ── Feature ID Mapping ──────────────────────────────────────────────────
#
# The handler maps sub_query_id → internal feature ID via a switch.
# Internal feature IDs are passed to FUN_14146c05c for status lookup.
# Sub-queries 0x02, 0x23, 0x28 have special handling (not a simple
# feature ID lookup).
#
# Feature names are inferred from: ADL SDK headers, driver debug strings,
# CWDDE command table analysis, and AMD documentation.

SESSION_QUERY_SUB_IDS = list(range(0x00, 0x2B))

_SUB_QUERY_TO_INTERNAL: Dict[int, Optional[int]] = {
    0x00: 0x00,  0x01: 0x02,  0x02: None,
    0x07: 0x03,  0x09: 0x06,  0x0B: 0x07,
    0x0C: 0x08,  0x0D: 0x04,  0x0E: 0x0A,
    0x0F: 0x0B,  0x10: 0x05,  0x11: 0x0C,
    0x12: 0x0D,  0x13: 0x0E,  0x14: 0x0F,
    0x15: 0x10,  0x16: 0x11,  0x17: 0x12,
    0x19: 0x14,  0x1A: 0x15,  0x1B: 0x16,
    0x1C: 0x13,  0x1D: 0x17,  0x1E: 0x18,
    0x1F: 0x19,  0x20: 0x1A,  0x21: 0x1B,
    0x22: 0x1C,  0x23: None,  0x25: 0x1D,
    0x26: 0x1E,  0x27: 0x1F,  0x28: None,
    0x29: 0x20,  0x2A: 0x21,
}

SESSION_QUERY_FEATURE_MAP: Dict[int, str] = {
    0x00: "PowerPlay",
    0x01: "OverDrive",
    0x02: "isPPLibActive",
    0x07: "PowerExpress",
    0x09: "FanControl",
    0x0B: "ThermalProtection",
    0x0C: "ThermalMonitor",
    0x0D: "GfxActivity",
    0x0E: "LEDControl",
    0x0F: "VirtualBattery",
    0x10: "PPThermalControl",
    0x11: "MacroPerformance",
    0x12: "AutoPerformance",
    0x13: "GfxVoltageOffset",
    0x14: "GfxClockVmax",
    0x15: "FanZeroRPM",
    0x16: "FanCurve",
    0x17: "SmartShift",
    0x18: "INVALID_0x18",
    0x19: "TDCLimit",
    0x1A: "EDCLimit",
    0x1B: "FullCtrl",
    0x1C: "MemoryTiming",
    0x1D: "UltraLowVoltage",
    0x1E: "StablePState",
    0x1F: "GfxOff",
    0x20: "SmartAccess",
    0x21: "RSR",
    0x22: "GameMode",
    0x23: "WiFiBandNotify",
    0x25: "PMFNotify",
    0x26: "BxMxCoSupport",
    0x27: "SmartAlloc",
    0x28: "BxMxCoQuery",
    0x29: "GfxOffControl",
    0x2A: "PMLogCapabilities",
}

_UNSUPPORTED_SUB_QUERIES = {0x03, 0x04, 0x05, 0x06, 0x08, 0x0A, 0x18, 0x24}


def session_query_feature_name(sub_query_id: int) -> str:
    """Get the inferred feature name for a session query sub_query_id."""
    name = SESSION_QUERY_FEATURE_MAP.get(sub_query_id)
    if name:
        return name
    if sub_query_id in _UNSUPPORTED_SUB_QUERIES:
        return f"UNSUPPORTED_0x{sub_query_id:02X}"
    internal = _SUB_QUERY_TO_INTERNAL.get(sub_query_id)
    if internal is not None:
        return f"Feature_{internal}"
    return f"Unknown_0x{sub_query_id:02X}"


# ── OD Write (0x00C000A1) ────────────────────────────────────────────────
#
# Buffer layout confirmed via Frida capture of Adrenalin OD settings apply.
# See dist/research/od_write_command_analysis.md for full field analysis.
#
#   +0x0CC  CWDDE header      {0x060C, 0x060C, 0x00C000A1}
#   +0x0D8  CWDDE params      response_size, sub_command, sub_flag, interface flags
#   +0x0120 OD entry table    73 × 20-byte entries {i32 value, u32 is_set, u32[3]}
#   +0x06D8 Response area     324 bytes (driver fills on return)

_OD_WRITE_TOTAL       = 2076       # 0x081C
_OD_WRITE_CWDDE_SIZE  = 0x060C     # 1548-byte CWDDE block
_OD_WRITE_NUM_ENTRIES = 73
_OD_WRITE_ENTRY_SIZE  = 20
_OD_WRITE_TABLE_OFF   = 0x0120
_OD_WRITE_RESP_OFF    = 0x06D8
_OD_WRITE_RESP_SIZE   = 324


def build_v2_od_write(
    entries: Dict[int, Tuple[int, int]],
    *,
    sub_command: int = 0,
    sub_flag: int = 0,
) -> bytearray:
    """Build a 2076-byte v2 escape buffer for OD write (0x00C000A1).

    Constructs the full escape buffer matching the Frida-captured format
    for OD8 settings apply.  Each entry in the OD parameter table is a
    20-byte struct::

        struct OD8Entry {
            i32 value;          // parameter value (signed)
            u32 is_set;         // 1 = apply this entry, 0 = skip
            u32 reserved[3];    // always zero
        };

    Only entries with ``is_set = 1`` are applied by the driver.  Unset
    entries are left zeroed (no-op).

    Args:
        entries:     Dict mapping OD8 index (0-72) to ``(value, is_set)``
                     tuples.  ``value`` is a signed 32-bit parameter value;
                     ``is_set`` should be 1 to apply, 0 to leave unchanged.
        sub_command: Sub-command at +0xE4.  0 for normal apply,
                     0x02B2 for initial apply after Adrenalin startup.
        sub_flag:    Sub-flag at +0xE8.  0 for normal apply,
                     1 for initial apply or reset-to-defaults.

    Returns:
        Mutable 2076-byte bytearray ready for ``D3DKMTClient.v2_escape()``.

    Raises:
        ValueError: If any entry index is outside [0, 72].
    """
    for idx in entries:
        if not (0 <= idx < _OD_WRITE_NUM_ENTRIES):
            raise ValueError(
                f"OD8 entry index {idx} out of range "
                f"[0, {_OD_WRITE_NUM_ENTRIES - 1}]")

    buf = build_v2_escape(
        command_code=CWDDE_CMD_OD_WRITE,
        total_size=_OD_WRITE_TOTAL,
        cwdde_block_size=_OD_WRITE_CWDDE_SIZE,
    )

    # CWDDE params (offsets relative to buffer start)
    struct.pack_into("<I", buf, 0x0E0, 0x4C)          # response size
    struct.pack_into("<I", buf, 0x0E4, sub_command)
    struct.pack_into("<I", buf, 0x0E8, sub_flag)
    struct.pack_into("<I", buf, 0x110, 1)              # interface flag
    struct.pack_into("<I", buf, 0x114, 1)              # interface flag

    # OD entry table
    for idx, (value, is_set) in entries.items():
        off = _OD_WRITE_TABLE_OFF + idx * _OD_WRITE_ENTRY_SIZE
        struct.pack_into("<iI", buf, off, value, is_set)

    # Pre-populate response area header hints (Adrenalin fills these
    # before sending; the driver reads them as format hints).
    struct.pack_into("<I", buf, _OD_WRITE_RESP_OFF + 4, 0x013C)  # data_size
    struct.pack_into("<I", buf, _OD_WRITE_RESP_OFF + 8, 0x4C)    # header_size

    return buf


@dataclass
class OdWriteResponse:
    """Parsed response from an OD write (0x00C000A1) escape."""
    status: int
    data_size: int
    header_size: int
    sub_command_echo: int
    success_flag: int
    raw: bytes


def parse_v2_od_write_response(buf: bytes) -> OdWriteResponse:
    """Parse the 324-byte response area from an OD write escape.

    The response starts at +0x06D8 in the 2076-byte buffer::

        +0x06D8: u32 status           (0 = success)
        +0x06DC: u32 data_size        (0x013C = 316)
        +0x06E0: u32 header_size      (0x4C = 76)
        +0x06E4: u32 sub_command_echo (0x02B2 in most responses)
        ...
        +0x080C: u32 success_flag     (1 = applied)

    Args:
        buf: The full 2076-byte buffer after ``D3DKMTClient.v2_escape()``.

    Returns:
        Parsed :class:`OdWriteResponse` with status fields and raw bytes.
    """
    resp = buf[_OD_WRITE_RESP_OFF:_OD_WRITE_RESP_OFF + _OD_WRITE_RESP_SIZE]
    status, data_size, header_size, sub_echo = struct.unpack_from(
        "<IIII", resp, 0)
    success_off = 0x080C - _OD_WRITE_RESP_OFF
    success_flag = 0
    if success_off + 4 <= len(resp):
        success_flag = struct.unpack_from("<I", resp, success_off)[0]
    return OdWriteResponse(
        status=status,
        data_size=data_size,
        header_size=header_size,
        sub_command_echo=sub_echo,
        success_flag=success_flag,
        raw=bytes(resp),
    )


# ── OD Write Response Value Array ────────────────────────────────────────
#
# The 324-byte response area from 0x00C000A1 contains a 24-byte header
# followed by an array of i32 values — one per OD8 entry index.
#
# Layout (verified against Frida captures):
#   resp+0x00: u32 status (0 = success)
#   resp+0x04: u32 data_size (0x013C = 316)
#   resp+0x08: u32 header_size (0x4C = 76)
#   resp+0x0C: u32 sub_command_echo
#   resp+0x10: u32 reserved
#   resp+0x14: u32 reserved
#   resp+0x18: i32[75] value array (indices 0-72 are OD8 values,
#              index 73 is a hash/timestamp, index 74 is trailing)
#
# Confirmed mappings:
#   value[5]  = GfxclkFmax        → buf+0x0704
#   value[6]  = PowerLimit/offset → buf+0x0708
#   value[16] = FanCurveSpeed[0]  → buf+0x0730
#   value[33] = FanMode           → buf+0x0774
#   value[34] = GfxclkFoffset     → buf+0x0778

_OD_WRITE_RESP_HEADER_SIZE = 24
_OD_WRITE_VALUES_OFF = _OD_WRITE_RESP_OFF + _OD_WRITE_RESP_HEADER_SIZE
_OD_WRITE_MAX_VALUES = 75


def parse_v2_od_write_values(buf: bytes) -> Dict[int, int]:
    """Extract the OD8 current-value array from a 0x00C000A1 response.

    After the driver processes the OD write command (even a no-op with
    all ``is_set=0``), it fills the response with current values for
    every OD8 index via ``GetCurrentSettings``.

    The value array starts at buf+0x06F0 (response offset +0x18), with
    one i32 per OD8 index.  Indices 0-72 are actual OD8 entry values.

    Args:
        buf: The full 2076-byte buffer after ``D3DKMTClient.v2_escape()``.

    Returns:
        Dict mapping OD8 index (0-72) to the current i32 value.
        Only indices 0 through ``_OD_WRITE_NUM_ENTRIES - 1`` (72) are
        included; trailing metadata entries (hash, etc.) are excluded.
    """
    values: Dict[int, int] = {}
    n = min(_OD_WRITE_NUM_ENTRIES, _OD_WRITE_MAX_VALUES)
    for i in range(n):
        off = _OD_WRITE_VALUES_OFF + i * 4
        if off + 4 > len(buf):
            break
        val = struct.unpack_from("<i", buf, off)[0]
        values[i] = val
    return values


def parse_v2_od_write_values_full(buf: bytes) -> Dict[int, int]:
    """Extract all 75 response values including metadata slots.

    Same as :func:`parse_v2_od_write_values` but includes indices 73-74
    (hash/timestamp and trailing metadata).
    """
    values: Dict[int, int] = {}
    for i in range(_OD_WRITE_MAX_VALUES):
        off = _OD_WRITE_VALUES_OFF + i * 4
        if off + 4 > len(buf):
            break
        val = struct.unpack_from("<i", buf, off)[0]
        values[i] = val
    return values


# ── SmartShift (0xB0) SetDeltaGainControl / (0xAF) GetCurrentSettings ────
#
# Decompiled from FUN_141458b48 (set) and FUN_141458a00 (get).
#
# Handler param layout (from Ghidra pass 12 debug strings):
#   Input  (param_2): +0x04=ulModes(u32), +0x08=ulValue(u32)
#   Output (param_3): zeroed to 0x20, then filled:
#     +0x04=ulMinRange, +0x08=ulMaxRange, +0x0C=ulDefault,
#     +0x10=ulModes(echo), +0x14=ulValue(echo)
#
# CWDDE dispatch table entry fields (32-byte entries):
#   0xB0 (SET): field3=20 (input size), field4=0x20 (output size)
#   0xAF (GET): field3=0  (no input),   field4=0x20 (output size)
#
# The dispatcher passes:
#   param_2 → CWDDE+8 (cmd_code acts as struct header)
#   param_3 → CWDDE+block_size (output area, immediately after CWDDE block)
#
# Input fields: CWDDE+12=ulModes, CWDDE+16=ulValue
# Output struct at param_3: [hdr(4), minRange(4), maxRange(4), default(4),
#                             modes(4), value(4)] = 24 bytes within 32B zeroed
#
# CWDDE block_size constraints (empirically validated):
#   - block_size must be >= 12 + field3 (input data fits)
#   - block_size should be 8-byte aligned
#   - The response_size_hint is ALWAYS at absolute buffer offset 0x0E0,
#     regardless of whether that's inside or outside the CWDDE block
#
# HARDWARE GATING: SmartShift SET (0xB0) is BLOCKED by the CWDDE dispatcher
# on desktop RDNA 4 GPUs (status=0x06 regardless of buffer format).
# SmartShift requires a laptop with AMD CPU+GPU SmartShift support.
# GET (0xAF) succeeds but returns all zeros on non-SmartShift hardware.

_SS_OUTPUT_SIZE    = 32   # 0x20 — handler zeroes this much in output buf
_SS_RESP_DATA      = 20   # 5 × u32 actual data fields
_SS_SET_CWDDE_SIZE = 32   # 12 header + 20 (field3) — requires SmartShift HW
_SS_GET_CWDDE_SIZE = 16   # 12 header + 4 padding

_GM_OUTPUT_SIZE    = 16   # handler zeroes 0x10 in output buf
_GM_RESP_DATA      = 8    # 2 × u32 (status + ulStates)
_GM_SET_CWDDE_SIZE = 24   # 12 header + 12 (field3=8 + alignment)
_GM_GET_CWDDE_SIZE = 16   # 12 header + 4 padding

_V2_OUTPUT_PAD     = 128  # trailing pad required by v2 protocol validation


@dataclass
class SmartShiftResponse:
    """Parsed response from SmartShift GetCurrentSettings or SetDeltaGainControl."""
    success: bool
    status_byte: int
    min_range: int
    max_range: int
    default: int
    modes: int
    value: int
    raw: bytes


@dataclass
class GameModeResponse:
    """Parsed response from GameMode GetCurrentSettings or SetPolicyControl."""
    success: bool
    status_byte: int
    states: int
    raw: bytes


def build_v2_smartshift_set(
    modes: int = 0,
    value: int = 0,
) -> bytearray:
    """Build a v2 escape buffer for SmartShift SetDeltaGainControl (0x00C000B0).

    The handler reads ulModes and ulValue from the input area (CWDDE+12, +16),
    calls the internal SmartShift set function, and writes the response
    (ulMinRange, ulMaxRange, ulDefault, ulModes, ulValue) to the output area
    at param_3 = CWDDE + block_size.

    Sending modes=0 value=0 is a safe read-only probe that returns current
    state and ranges without changing anything.

    .. warning:: This command is **hardware-gated** by the CWDDE dispatcher
       on desktop RDNA 4 GPUs (returns status 0x06 regardless of buffer
       format).  SmartShift requires a laptop with AMD CPU+GPU SmartShift
       support.

    Args:
        modes: SmartShift mode flags (0 = query only).
        value: Delta gain value to set (0 = query only).

    Returns:
        Mutable bytearray ready for ``D3DKMTClient.v2_escape()``.
    """
    cwdde_size = _SS_SET_CWDDE_SIZE  # 32
    total = V2_CWDDE_OFFSET + cwdde_size + _SS_OUTPUT_SIZE + _V2_OUTPUT_PAD

    # CWDDE params: modes(4) + value(4) + hint_at_0xE0(4) + padding(8)
    params = struct.pack("<II", modes, value)
    params += struct.pack("<I", _SS_OUTPUT_SIZE)  # response_size at 0x0E0
    params += b"\x00" * (cwdde_size - 12 - len(params))

    buf = build_v2_escape(
        command_code=CWDDE_CMD_SMARTSHIFT_SET,
        total_size=total,
        cwdde_block_size=cwdde_size,
        cwdde_params=params,
    )

    return buf


def build_v2_smartshift_get() -> bytearray:
    """Build a v2 escape buffer for SmartShift GetCurrentSettings (0x00C000AF).

    Read-only query that returns the current SmartShift delta gain state.
    On non-SmartShift hardware, succeeds but returns all zeros.

    Output struct at param_3 = CWDDE+16 (0x0DC)::

        +0x0DC: u32 header (zeroed by handler)
        +0x0E0: u32 ulMinRange  (also serves as response_size_hint pre-call)
        +0x0E4: u32 ulMaxRange
        +0x0E8: u32 ulDefault
        +0x0EC: u32 ulModes
        +0x0F0: u32 ulValue
    """
    cwdde_size = _SS_GET_CWDDE_SIZE  # 16
    total = V2_CWDDE_OFFSET + cwdde_size + _SS_OUTPUT_SIZE + _V2_OUTPUT_PAD

    buf = build_v2_escape(
        command_code=CWDDE_CMD_SMARTSHIFT_GET,
        total_size=total,
        cwdde_block_size=cwdde_size,
        cwdde_params=struct.pack("<I", 0),
    )

    resp_off = V2_CWDDE_OFFSET + cwdde_size
    struct.pack_into("<II", buf, resp_off, 0, _SS_OUTPUT_SIZE)
    return buf


def parse_v2_smartshift_response(
    buf: bytes,
    cwdde_size: int = _SS_SET_CWDDE_SIZE,
) -> SmartShiftResponse:
    """Parse SmartShift response from an escape buffer.

    The output struct (param_3) starts at CWDDE + block_size.  The
    handler zeroes 0x20 bytes there, then fills 5 u32s at offsets
    +4 through +0x14 within param_3::

        param_3+0x00: u32 header (zeroed)
        param_3+0x04: u32 ulMinRange
        param_3+0x08: u32 ulMaxRange
        param_3+0x0C: u32 ulDefault
        param_3+0x10: u32 ulModes
        param_3+0x14: u32 ulValue

    On non-SmartShift hardware, the handler doesn't run and the output
    area retains its pre-call values (including the response_size_hint).
    """
    status_byte = buf[0x0094] if len(buf) > 0x0094 else 0

    param3_off = V2_CWDDE_OFFSET + cwdde_size
    if param3_off + _SS_OUTPUT_SIZE > len(buf):
        return SmartShiftResponse(
            success=False, status_byte=status_byte,
            min_range=0, max_range=0, default=0, modes=0, value=0,
            raw=bytes(buf[param3_off:]) if param3_off < len(buf) else b"",
        )

    raw = bytes(buf[param3_off:param3_off + _SS_OUTPUT_SIZE])
    _hdr, min_r, max_r, dflt, modes, val = struct.unpack_from(
        "<IIIIII", raw, 0)

    hint_echo = (min_r == _SS_OUTPUT_SIZE and max_r == 0 and dflt == 0
                 and modes == 0 and val == 0)
    has_data = not hint_echo and any(b != 0 for b in raw[4:24])
    return SmartShiftResponse(
        success=has_data,
        status_byte=status_byte,
        min_range=min_r if not hint_echo else 0,
        max_range=max_r,
        default=dflt,
        modes=modes,
        value=val,
        raw=raw,
    )


def build_v2_gamemode_set(states: int = 0) -> bytearray:
    """Build a v2 escape buffer for GameMode SetPolicyControl (0x00C000B9).

    The handler (2-param: context + input, no output) reads ulStates from
    param_2+4 = CWDDE+12.  Dispatch table: field3=8, field4=0 (no output).

    Sending states=0 is a safe no-op / disable game mode.

    Args:
        states: Game mode policy state flags (0 = disable / query).
    """
    cwdde_size = _GM_SET_CWDDE_SIZE  # 24
    total = V2_CWDDE_OFFSET + cwdde_size + _V2_OUTPUT_PAD

    # CWDDE params: states(4) + padding(4) + 0 at 0x0E0(4) = 12 bytes
    params = struct.pack("<III", states, 0, 0)

    buf = build_v2_escape(
        command_code=CWDDE_CMD_GAMEMODE_SET,
        total_size=total,
        cwdde_block_size=cwdde_size,
        cwdde_params=params,
    )

    return buf


def build_v2_gamemode_get() -> bytearray:
    """Build a v2 escape buffer for GameMode GetCurrentSettings (0x00C000B8).

    Read-only query that returns the current GameMode policy state.
    Dispatch table: field3=0, field4=0x10 (16 bytes output).

    Output struct at param_3 = CWDDE+16 (0x0DC)::

        +0x0DC: u32 header (zeroed by handler)
        +0x0E0: u32 ulStates  (also serves as response_size_hint pre-call)
    """
    cwdde_size = _GM_GET_CWDDE_SIZE  # 16
    total = V2_CWDDE_OFFSET + cwdde_size + _GM_OUTPUT_SIZE + _V2_OUTPUT_PAD

    buf = build_v2_escape(
        command_code=CWDDE_CMD_GAMEMODE_GET,
        total_size=total,
        cwdde_block_size=cwdde_size,
        cwdde_params=struct.pack("<I", 0),
    )

    resp_off = V2_CWDDE_OFFSET + cwdde_size
    struct.pack_into("<II", buf, resp_off, 0, _GM_OUTPUT_SIZE)
    return buf


def parse_v2_gamemode_response(
    buf: bytes,
    cwdde_size: int = _GM_GET_CWDDE_SIZE,
) -> GameModeResponse:
    """Parse GameMode response from an escape buffer.

    For GET (cwdde=16), param_3 = CWDDE+16 (0x0DC).  The handler
    zeroes 0x10 bytes there and writes ulStates at param_3+4.

    For SET (cwdde=24), there is no output (field4=0).  The response
    is inferred from a subsequent GET call.

    On inactive hardware, the handler doesn't run and the output area
    retains pre-call values.
    """
    status_byte = buf[0x0094] if len(buf) > 0x0094 else 0

    param3_off = V2_CWDDE_OFFSET + cwdde_size
    if param3_off + 8 > len(buf):
        return GameModeResponse(
            success=False, status_byte=status_byte, states=0,
            raw=bytes(buf[param3_off:]) if param3_off < len(buf) else b"",
        )

    read_len = min(_GM_OUTPUT_SIZE, len(buf) - param3_off)
    raw = bytes(buf[param3_off:param3_off + read_len])
    _hdr, states = struct.unpack_from("<II", raw, 0) if len(raw) >= 8 else (0, 0)

    hint_echo = (_hdr == 0 and states == _GM_OUTPUT_SIZE)
    has_data = not hint_echo and any(b != 0 for b in raw[:8])
    return GameModeResponse(
        success=has_data,
        status_byte=status_byte,
        states=states if not hint_echo else 0,
        raw=raw,
    )


# ── ActivateClient (0x70/0x71/0x82) ──────────────────────────────────────
#
# Three ActivateClient variants discovered in the CWDDE dispatch table
# (Ghidra pass 12).  These are session initialization commands — the
# hypothesis is that they populate the per-process feature table that
# GetFeatureStatus (0x01) reads from.
#
# From the CWDDE dispatch table at RVA 0x00945BC0:
#   0x70: handler=RVA 0x00268148, input=0, output=144B ("perf" init)
#   0x71: handler=RVA 0x002688E4, input=0, output=144B (variant)
#   0x82: handler=RVA 0x01457880, input=0, output=552B (large/full init)
#
# All three returned status 0x06 in Phase 3 probe (handler dispatched but
# needs correct buffer format/size).  0x82 is in the same code region as
# the PowerPlay/OverDrive handlers (0x01457xxx), suggesting it's the
# "full" activation path.
#
# Adrenalin's observed escape sequence (Frida capture) does NOT include
# 0x70/0x71/0x82 — the activation may happen via a different mechanism,
# or it may be called before the Frida hook was attached.

CWDDE_CMD_ACTIVATE_CLIENT_70 = 0x00C00070
CWDDE_CMD_ACTIVATE_CLIENT_71 = 0x00C00071
CWDDE_CMD_ACTIVATE_CLIENT_82 = 0x00C00082

ACTIVATE_CLIENT_VARIANTS: Dict[int, Dict] = {
    0x70: {"cmd": CWDDE_CMD_ACTIVATE_CLIENT_70, "output_size": 144,
           "rva": 0x00268148, "label": "perf"},
    0x71: {"cmd": CWDDE_CMD_ACTIVATE_CLIENT_71, "output_size": 144,
           "rva": 0x002688E4, "label": "variant"},
    0x82: {"cmd": CWDDE_CMD_ACTIVATE_CLIENT_82, "output_size": 552,
           "rva": 0x01457880, "label": "large"},
}


@dataclass
class ActivateClientResponse:
    """Parsed response from an ActivateClient escape (0x70/0x71/0x82)."""
    variant: int
    success: bool
    status_byte: int
    output_nonzero_bytes: int
    output_size: int
    raw: bytes
    template: str = ""


def build_v2_activate_client(
    variant: int = 0x82,
    cwdde_block_size: int = 16,
    resp_size_hint: Optional[int] = None,
) -> bytearray:
    """Build a v2 escape buffer for an ActivateClient command.

    The ActivateClient handlers (0x70/0x71/0x82) have no input data and
    write their output to the area after the CWDDE block.  Multiple buffer
    formats are tried because the exact format required by these handlers
    is not yet confirmed.

    The ``resp_size_hint`` is written at absolute buffer offset 0x0E0,
    which the v2 envelope parser always reads as the expected response
    size.  If None, defaults to the dispatch table output_size.

    Args:
        variant:         Handler index: 0x70, 0x71, or 0x82.
        cwdde_block_size: CWDDE block size (>= 16 for no-input commands).
        resp_size_hint:  Response size hint at offset 0x0E0.

    Returns:
        Mutable bytearray ready for ``D3DKMTClient.v2_escape()``.
    """
    info = ACTIVATE_CLIENT_VARIANTS.get(variant)
    if info is None:
        raise ValueError(
            f"Unknown ActivateClient variant 0x{variant:02X}; "
            f"valid: {list(ACTIVATE_CLIENT_VARIANTS.keys())}")

    output_size = info["output_size"]
    if resp_size_hint is None:
        resp_size_hint = output_size

    total = V2_CWDDE_OFFSET + cwdde_block_size + output_size + _V2_OUTPUT_PAD

    cwdde_params_len = cwdde_block_size - 12
    cwdde_params = b"\x00" * cwdde_params_len

    buf = build_v2_escape(
        command_code=info["cmd"],
        total_size=total,
        cwdde_block_size=cwdde_block_size,
        cwdde_params=cwdde_params,
    )

    # Write the response_size_hint at 0x0E0 (always checked by v2 parser).
    # If the CWDDE block extends past 0x0E0, it was already written as
    # part of cwdde_params; we overwrite it with the correct hint.
    if 0x0E0 + 4 <= total:
        struct.pack_into("<I", buf, 0x0E0, resp_size_hint)

    # Pre-populate the output area header (similar to SmartShift GET).
    resp_off = V2_CWDDE_OFFSET + cwdde_block_size
    if resp_off + 8 <= total:
        struct.pack_into("<II", buf, resp_off, 0, resp_size_hint)

    return buf


def parse_v2_activate_client_response(
    buf: bytes,
    variant: int = 0x82,
    cwdde_block_size: int = 16,
    template: str = "",
) -> ActivateClientResponse:
    """Parse an ActivateClient response from an escape buffer.

    The output struct (param_3) starts at CWDDE + block_size.  The
    output_size is determined by the dispatch table entry for the variant.

    Args:
        buf: The full escape buffer after ``D3DKMTClient.v2_escape()``.
        variant: Handler index (0x70, 0x71, or 0x82).
        cwdde_block_size: CWDDE block size used in the request.
        template: Name of the buffer template used (for logging).

    Returns:
        Parsed :class:`ActivateClientResponse`.
    """
    info = ACTIVATE_CLIENT_VARIANTS.get(variant, {})
    output_size = info.get("output_size", 144)

    status_byte = buf[0x0094] if len(buf) > 0x0094 else 0

    param3_off = V2_CWDDE_OFFSET + cwdde_block_size
    end = min(param3_off + output_size, len(buf))
    raw = bytes(buf[param3_off:end])

    nonzero = sum(1 for b in raw if b != 0)

    return ActivateClientResponse(
        variant=variant,
        success=nonzero > 0 and status_byte not in (0x03, 0x06),
        status_byte=status_byte,
        output_nonzero_bytes=nonzero,
        output_size=len(raw),
        raw=raw,
        template=template,
    )


# ── CWDDEPM ActivateClient (0xC08008) — BACO Power State Control ──────
#
# Decompiled from FUN_14014723F0 (RVA 0x014723F0, 1647 bytes) in
# ghidra_decompiled_pptable13.txt line 1121.
#
# This is NOT a session initializer — it is a BACO (Bus Active, Chip Off)
# power state control command.  It controls thermal controller, fan speed,
# clock gating, dynamic state management, and IPS notifications.
#
# CWDDEPM dispatch: command > 0xC08000 → cwddepm_new_path (FUN_141472a68)
# Function table index = command - 0xC08001 = 7
#
# Input: 8 bytes after the 12-byte CWDDE header (cwdde_block_size=20):
#   +0x0D8: u32 cmd_or_reserved (0 for normal use)
#   +0x0DC: u32 flags (BACO control bitmask)
#
# Return codes (status_byte at +0x0094):
#   0 = success (command accepted)
#   3 = BACO not enabled / isPPLibActive is false
#   6 = invalid input (wrong size or bad flags)
#
# The isPPLibActive gate at pp+0x00 and client-active byte at pp+0x1068
# must both be non-zero for CWDDE/CWDDEPM dispatch to proceed.

_CWDDEPM_AC_CWDDE_SIZE  = 20   # 12 header + 8 input
_CWDDEPM_AC_OUTPUT_SIZE = 64   # modest output area for potential response


@dataclass
class CwddepmActivateClientResponse:
    """Parsed response from CWDDEPM ActivateClient (0xC08008)."""
    success: bool
    status_byte: int
    flags_sent: int
    output_nonzero_bytes: int
    raw: bytes


def build_v2_cwddepm_activate_client(
    flags: int = 0,
    *,
    allow_unsafe: bool = False,
) -> bytearray:
    """Build a v2 escape buffer for CWDDEPM ActivateClient (0xC08008).

    The CWDDEPM ActivateClient handler is a BACO (Bus Active, Chip Off)
    power state control command, routed through cwddepm_new_path when the
    command code > 0xC08000.

    Input layout (8 bytes after the 12-byte CWDDE header)::

        +0x0D8: u32 cmd_or_reserved  (0)
        +0x0DC: u32 flags            (BACO control bitmask)

    Safe flag combinations for probing:

    - ``flags=0x0``:  no BACO transition; returns 0 if isPPLibActive
    - ``flags=0x200000``: extended mode only, no enter/exit

    .. warning:: Flags 0x10000 (exit BACO) and 0x20000 (enter BACO) control
       GPU power state transitions.  Using them may power-cycle the GPU.
       Pass ``allow_unsafe=True`` to override the safety check.

    Args:
        flags:        BACO control bitmask.
        allow_unsafe: Must be True to send BACO enter/exit flags.

    Returns:
        Mutable bytearray ready for ``D3DKMTClient.v2_escape()``.

    Raises:
        ValueError: If unsafe flags are set without ``allow_unsafe=True``.
    """
    if (flags & _BACO_UNSAFE_FLAGS) and not allow_unsafe:
        raise ValueError(
            f"BACO flags 0x{flags:X} contain unsafe bits "
            f"(EXIT=0x{BACO_FLAG_EXIT:X}, ENTER=0x{BACO_FLAG_ENTER:X}) "
            f"that could power-cycle the GPU. "
            f"Set allow_unsafe=True to override.")

    cwdde_size = _CWDDEPM_AC_CWDDE_SIZE
    output_size = _CWDDEPM_AC_OUTPUT_SIZE
    total = V2_CWDDE_OFFSET + cwdde_size + output_size + _V2_OUTPUT_PAD

    cwdde_params = struct.pack("<II", 0, flags)

    buf = build_v2_escape(
        command_code=CWDDEPM_CMD_ACTIVATE_CLIENT,
        total_size=total,
        cwdde_block_size=cwdde_size,
        cwdde_params=cwdde_params,
    )

    # Output area starts at CWDDE + cwdde_size = 0x0CC + 20 = 0x0E0.
    # This coincides with the v2 response_size_hint position (0x0E0),
    # so the first u32 of the output area doubles as the hint.
    resp_off = V2_CWDDE_OFFSET + cwdde_size
    struct.pack_into("<I", buf, resp_off, output_size)

    return buf


def parse_v2_cwddepm_activate_client_response(
    buf: bytes,
    flags_sent: int = 0,
) -> CwddepmActivateClientResponse:
    """Parse a CWDDEPM ActivateClient (0xC08008) response.

    The status_byte at +0x0094 indicates the handler result:

    - 0x00: success (command accepted, isPPLibActive was true)
    - 0x03: BACO not enabled / isPPLibActive is false
    - 0x06: invalid input (wrong buffer format or flags)

    The output area starts at CWDDE + 20 = +0x0E0.  The handler may
    or may not write data there; we capture raw bytes for analysis.

    Args:
        buf:        The full escape buffer after ``D3DKMTClient.v2_escape()``.
        flags_sent: The BACO flags that were sent (for annotation).

    Returns:
        Parsed :class:`CwddepmActivateClientResponse`.
    """
    status_byte = buf[0x0094] if len(buf) > 0x0094 else 0xFF

    output_off = V2_CWDDE_OFFSET + _CWDDEPM_AC_CWDDE_SIZE
    output_end = min(output_off + _CWDDEPM_AC_OUTPUT_SIZE, len(buf))
    raw = bytes(buf[output_off:output_end])
    nonzero = sum(1 for b in raw if b != 0)

    return CwddepmActivateClientResponse(
        success=status_byte == 0x00,
        status_byte=status_byte,
        flags_sent=flags_sent,
        output_nonzero_bytes=nonzero,
        raw=raw,
    )


# Known u32 offsets within each 256-byte OD limits response block.
# Derived from the Frida capture response data analysis.
_OD_BLOCK_FIELDS = [
    (0x00, "GfxclkFmax"),
    (0x04, "GfxclkFmin"),
    (0x08, "BoostClock"),
    (0x0C, "SocClkMin"),
    (0x10, "UclkFmax"),
    (0x14, "UclkFmin"),
    (0x20, "VddGfxVmax"),
    (0x24, "TdcLimit"),
    (0x28, "VddSocVmax_or_PPT"),
    (0x2C, "MaxOpTemp"),
    (0x30, "AcousticTargetRPM"),
    (0x34, "FanTargetTemp"),
    (0x38, "AcousticLimitRPM"),
    (0x3C, "FanZeroRpmStopTemp"),
    (0x50, "FanMinimumPwm"),
    (0x58, "GfxclkFmaxVmax"),
    (0x60, "UclkBoost"),
    (0x64, "EdcLimit"),
    (0x68, "VoltageClockBound"),
    (0x6C, "PwmTempBound"),
]

_OD_BLOCK_SIZE = 256
_OD_DATA_OFFSET = V2_CWDDE_OFFSET + 16 + 8  # 0x0E4


def parse_v2_od_block(data: bytes, offset: int = 0) -> Dict[str, int]:
    """Parse a single 256-byte OD limits block from escape response."""
    result: Dict[str, int] = {}
    for field_off, name in _OD_BLOCK_FIELDS:
        abs_off = offset + field_off
        if abs_off + 4 <= len(data):
            result[name] = struct.unpack_from("<I", data, abs_off)[0]
    return result


def parse_v2_od_limits(
    buf: bytes,
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Parse the 3 × 256-byte OD limit blocks from a 0x00C0009B response.

    Args:
        buf: The full 996-byte escape buffer after the D3DKMTEscape call.

    Returns:
        ``(block0_max, block1_adv_max, block2_min)`` — three dicts of
        OD parameter values (u32 fields keyed by name).
    """
    off = _OD_DATA_OFFSET
    b0 = parse_v2_od_block(buf, off)
    b1 = parse_v2_od_block(buf, off + _OD_BLOCK_SIZE)
    b2 = parse_v2_od_block(buf, off + 2 * _OD_BLOCK_SIZE)
    return b0, b1, b2


# ── FeatureCtrlMask Inference from OD Limits ─────────────────────────
#
# The 0x00C0009B escape response does NOT contain the raw FeatureCtrlMask
# bitmask.  It only contains OD parameter limit values (max/min ranges).
# We infer which PP_OD_FEATURE bits are enabled by checking which limit
# fields have a non-zero max and an adjustable range (max > min).
#
# Some features cannot be reliably detected from the limits response:
#   - GFX_VF_CURVE (bit 0):  VF curve offsets aren't in the limit blocks
#   - SOC_VMAX (bit 2):      overlaps with the PPT field in the response
#   - FULL_CTRL (bit 6):     full control mode limits aren't exposed
#   - FCLK (bit 10):         FCLK limits aren't in the parsed block fields
#
# For these, use DMA-based reads from the PP table (offset 0x105C) or
# the OverDriveTable_t.FeatureCtrlMask (offset 0x00) via read_od().

_OD_BLOCK_FEATURE_MAP: Dict[str, PpOdFeature] = {
    "GfxclkFmax":       PpOdFeature.GFXCLK,
    "UclkFmax":         PpOdFeature.UCLK,
    "VddGfxVmax":       PpOdFeature.GFX_VMAX,
    "TdcLimit":         PpOdFeature.TDC,
    "VddSocVmax_or_PPT": PpOdFeature.PPT,
    "MaxOpTemp":        PpOdFeature.TEMPERATURE,
    "AcousticTargetRPM": PpOdFeature.FAN_LEGACY,
    "AcousticLimitRPM": PpOdFeature.FAN_LEGACY,
    "FanTargetTemp":    PpOdFeature.FAN_LEGACY,
    "FanZeroRpmStopTemp": PpOdFeature.ZERO_FAN,
    "FanMinimumPwm":    PpOdFeature.FAN_CURVE,
    "PwmTempBound":     PpOdFeature.FAN_CURVE,
    "EdcLimit":         PpOdFeature.EDC,
}


@dataclass
class OdFeatureInfo:
    """Result of FeatureCtrlMask inference from OD limits response.

    Attributes:
        mask:          Inferred PpOdFeature bitmask (features with max > 0).
        adjustable:    Subset of mask — features with max > min (adjustable range).
        undetectable:  Feature bits that cannot be inferred from the limits
                       response and require DMA-based reads.
        block_max:     Raw max-limits block from the response.
        block_min:     Raw min-limits block from the response.
    """
    mask: int
    adjustable: int
    undetectable: int
    block_max: Dict[str, int]
    block_min: Dict[str, int]

    @property
    def features(self) -> PpOdFeature:
        return PpOdFeature(self.mask)

    @property
    def adjustable_features(self) -> PpOdFeature:
        return PpOdFeature(self.adjustable)

    def has_feature(self, feature: PpOdFeature) -> bool:
        return bool(self.mask & feature)


_UNDETECTABLE_FEATURES = (
    PpOdFeature.GFX_VF_CURVE
    | PpOdFeature.SOC_VMAX
    | PpOdFeature.FULL_CTRL
    | PpOdFeature.FCLK
)


def infer_feature_ctrl_mask(
    block_max: Dict[str, int],
    block_min: Dict[str, int],
) -> OdFeatureInfo:
    """Infer PP_OD_FEATURE bitmask from OD limits response blocks.

    Checks each known OD limit field in the max-limits block.  If the max
    value is > 0, the corresponding PP_OD_FEATURE bit is set.  If max > min,
    the feature is also marked as having an adjustable range.

    Args:
        block_max: Max-limits block (block 0 from ``parse_v2_od_limits``).
        block_min: Min-limits block (block 2 from ``parse_v2_od_limits``).

    Returns:
        :class:`OdFeatureInfo` with the inferred mask and metadata.
    """
    mask = 0
    adjustable = 0

    for field_name, feature_bit in _OD_BLOCK_FEATURE_MAP.items():
        max_val = block_max.get(field_name, 0)
        min_val = block_min.get(field_name, 0)
        if max_val > 0:
            mask |= feature_bit
        if max_val > min_val:
            adjustable |= feature_bit

    return OdFeatureInfo(
        mask=mask,
        adjustable=adjustable,
        undetectable=_UNDETECTABLE_FEATURES,
        block_max=block_max,
        block_min=block_min,
    )


# ── Adapter Info ─────────────────────────────────────────────────────────

@dataclass
class AdapterInfo:
    """Opened D3DKMT adapter information."""
    handle: int
    device_path: str
    luid_low: int
    luid_high: int

    @property
    def luid(self) -> int:
        """64-bit Locally Unique Identifier for this adapter."""
        return self.luid_low | (self.luid_high << 32)


# ── D3DKMT Client ───────────────────────────────────────────────────────

class D3DKMTClient:
    """WDDM escape client for AMD GPU adapters.

    Wraps D3DKMTOpenAdapter and D3DKMTEscape for sending v2 protocol
    escape commands to amdkmdag.sys.

    Usage::

        with D3DKMTClient.open_amd_adapter() as client:
            # Read OD limits via v2 protocol
            block_max, block_adv, block_min = client.read_od_limits()
            print(f"GfxclkFmax = {block_max['GfxclkFmax']} MHz")

            # Send arbitrary v2 commands
            buf = build_v2_escape(command_code=0x00C0009B, total_size=996)
            client.v2_escape(buf)

            # Or send raw buffers for protocol experimentation
            client.escape_raw(buf)
    """

    def __init__(self, adapter: AdapterInfo):
        self._adapter = adapter
        self._device_handle: int = 0
        self._closed = False
        self._od_initialized = False

    @property
    def adapter(self) -> AdapterInfo:
        """The underlying adapter handle and identification."""
        return self._adapter

    # ── Factory Methods ──────────────────────────────────────────────────

    @classmethod
    def open_gdi(cls, gdi_display_name: str) -> D3DKMTClient:
        r"""Open an adapter by GDI display name.

        The gdi_display_name comes from enumerate_display_devices() or
        find_amd_gdi_display_name(), e.g. ``\\.\DISPLAY1``.

        This is the most reliable adapter-open method on modern Windows.

        Raises:
            D3DKMTError: If D3DKMTOpenAdapterFromGdiDisplayName fails.
        """
        params = _D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME()
        params.DeviceName = gdi_display_name

        status = _D3DKMTOpenAdapterFromGdiDisplayName(ctypes.byref(params))
        _check(status, "D3DKMTOpenAdapterFromGdiDisplayName")

        info = AdapterInfo(
            handle=params.hAdapter,
            device_path=gdi_display_name,
            luid_low=params.AdapterLuid.LowPart,
            luid_high=params.AdapterLuid.HighPart,
        )
        _log.info("Opened adapter via GDI: handle=%d LUID=0x%X name=%s",
                   info.handle, info.luid, gdi_display_name)
        return cls(info)

    @classmethod
    def open_luid(cls, luid_low: int, luid_high: int = 0) -> D3DKMTClient:
        """Open an adapter by its Locally Unique Identifier.

        The LUID can be obtained from enumerate_adapters().

        Raises:
            D3DKMTError: If D3DKMTOpenAdapterFromLuid fails.
        """
        params = _D3DKMT_OPENADAPTERFROMLUID()
        params.AdapterLuid.LowPart = luid_low
        params.AdapterLuid.HighPart = luid_high

        status = _D3DKMTOpenAdapterFromLuid(ctypes.byref(params))
        _check(status, "D3DKMTOpenAdapterFromLuid")

        luid = luid_low | (luid_high << 32)
        info = AdapterInfo(
            handle=params.hAdapter,
            device_path=f"LUID:0x{luid:X}",
            luid_low=luid_low,
            luid_high=luid_high,
        )
        _log.info("Opened adapter via LUID: handle=%d LUID=0x%X",
                   info.handle, luid)
        return cls(info)

    @classmethod
    def open_device_name(cls, device_path: str) -> D3DKMTClient:
        """Open an adapter by device interface path (D3DKMTOpenAdapterFromDeviceName).

        NOTE: On some Windows 10/11 builds this API rejects PnP device
        interface paths.  Prefer open_gdi() or open_luid() instead.

        Raises:
            D3DKMTError: If D3DKMTOpenAdapterFromDeviceName fails.
        """
        params = _D3DKMT_OPENADAPTERFROMDEVICENAME()
        params.pDeviceName = device_path

        status = _D3DKMTOpenAdapterFromDeviceName(ctypes.byref(params))
        _check(status, "D3DKMTOpenAdapterFromDeviceName")

        info = AdapterInfo(
            handle=params.hAdapter,
            device_path=device_path,
            luid_low=params.AdapterLuid.LowPart,
            luid_high=params.AdapterLuid.HighPart,
        )
        _log.info("Opened adapter via DeviceName: handle=%d LUID=0x%X",
                   info.handle, info.luid)
        return cls(info)

    @classmethod
    def open_amd_adapter(cls) -> D3DKMTClient:
        r"""Find and open the first AMD GPU adapter.

        Tries multiple methods in order of reliability:

        1. GDI display name (``\\.\DISPLAYn``) via EnumDisplayDevices
           + D3DKMTOpenAdapterFromGdiDisplayName
        2. LUID via D3DKMTEnumAdapters3 + D3DKMTOpenAdapterFromLuid
        3. Device interface path via cfgmgr32
           + D3DKMTOpenAdapterFromDeviceName (fallback)

        Raises:
            RuntimeError: If no AMD GPU is found or all open methods fail.
        """
        errors: List[str] = []

        # Method 1: GDI display name (most reliable)
        gdi_name = find_amd_gdi_display_name()
        if gdi_name:
            try:
                return cls.open_gdi(gdi_name)
            except D3DKMTError as e:
                errors.append(f"GDI ({gdi_name}): {e}")

        # Method 2: LUID via enumeration
        if _D3DKMTEnumAdapters3 is not None:
            try:
                adapters = enumerate_adapters()
                if adapters:
                    h, lo, hi = adapters[0]
                    return cls.open_luid(lo, hi)
            except (D3DKMTError, RuntimeError) as e:
                errors.append(f"LUID: {e}")

        # Method 3: Device interface path (may not work on all Windows builds)
        amd_paths = find_amd_display_devices()
        if amd_paths:
            try:
                return cls.open_device_name(amd_paths[0])
            except D3DKMTError as e:
                errors.append(f"DeviceName: {e}")

        detail = "\n  ".join(errors) if errors else "No AMD GPU found"
        raise RuntimeError(
            f"Could not open AMD GPU adapter.\n  {detail}\n"
            "Ensure an AMD GPU with amdkmdag.sys is installed.")

    # ── Device Handle ─────────────────────────────────────────────────────

    def create_device(self) -> int:
        """Create a D3D device on this adapter (D3DKMTCreateDevice).

        The device handle is stored internally and automatically used by
        escape_raw().  Creates a minimal device with no flags — just
        enough to satisfy the escape interface's device-context
        requirement.

        Returns:
            The new device handle.

        Raises:
            D3DKMTError: If D3DKMTCreateDevice fails.
        """
        if self._device_handle:
            return self._device_handle

        params = _D3DKMT_CREATEDEVICE()
        params.hAdapter = self._adapter.handle
        params.Flags = 0

        status = _D3DKMTCreateDevice(ctypes.byref(params))
        _check(status, "D3DKMTCreateDevice")

        self._device_handle = params.hDevice
        _log.info("Created device: hDevice=%d on adapter=%d",
                   self._device_handle, self._adapter.handle)
        return self._device_handle

    def destroy_device(self) -> None:
        """Destroy the D3D device created by create_device()."""
        if not self._device_handle:
            return
        params = _D3DKMT_DESTROYDEVICE()
        params.hDevice = self._device_handle
        try:
            status = _D3DKMTDestroyDevice(ctypes.byref(params))
            if status == STATUS_SUCCESS:
                _log.info("Destroyed device %d", self._device_handle)
            else:
                _log.warning("D3DKMTDestroyDevice: NTSTATUS 0x%08X",
                             status & 0xFFFFFFFF)
        except Exception as e:
            _log.warning("D3DKMTDestroyDevice: %s", e)
        self._device_handle = 0

    @property
    def device_handle(self) -> int:
        return self._device_handle

    # ── Escape Interface ─────────────────────────────────────────────────

    def escape_raw(self, private_data: bytearray, *, flags: int = 0) -> bytearray:
        """Send a raw escape buffer to the display miniport driver.

        The driver reads input from and writes output to the same buffer
        (modified in-place).  The buffer must be a ``bytearray`` so the
        driver can write back into it.

        If a device handle has been created (via create_device()), it is
        automatically included in the escape call.

        Args:
            private_data: Mutable buffer containing the escape command.
                          Modified in-place with the driver response.
            flags: D3DDDI_ESCAPEFLAGS value (default 0).

        Returns:
            The same bytearray after driver processing.

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
            RuntimeError: If the client has been closed.
        """
        if self._closed:
            raise RuntimeError("D3DKMTClient is closed")

        c_buf = (ctypes.c_char * len(private_data)).from_buffer(private_data)

        esc = _D3DKMT_ESCAPE()
        esc.hAdapter = self._adapter.handle
        esc.hDevice = self._device_handle
        esc.Type = D3DKMT_ESCAPE_DRIVERPRIVATE
        esc.Flags = flags
        esc.pPrivateDriverData = ctypes.addressof(c_buf)
        esc.PrivateDriverDataSize = len(private_data)
        esc.hContext = 0

        _log.debug("D3DKMTEscape: adapter=%d device=%d size=%d first4=%s",
                    self._adapter.handle, self._device_handle,
                    len(private_data), private_data[:4].hex())

        status = _D3DKMTEscape(ctypes.byref(esc))
        _check(status, "D3DKMTEscape")

        return private_data

    def atid_escape(
        self,
        escape_code: int,
        sub_code: int = 0,
        payload: bytes = b"",
        output_size: int = 0,
    ) -> Tuple[Dict[str, int], bytes]:
        """Send an ATID-signed escape command and parse the response.

        Builds the ATID header, sends via D3DKMTEscape, then parses the
        response header and extracts the payload.

        Args:
            escape_code: Primary command code (CWDDE module selector).
            sub_code:    Sub-command within the module.
            payload:     Input data appended after the ATID header.
            output_size: Expected output payload size.

        Returns:
            ``(header_dict, payload_bytes)`` tuple.

        Raises:
            D3DKMTError: If D3DKMTEscape fails.
        """
        buf = build_atid_escape(escape_code, sub_code, payload, output_size)

        _log.debug("ATID escape: code=0x%X sub=0x%X in=%d out=%d total=%d",
                    escape_code, sub_code, len(payload), output_size, len(buf))

        self.escape_raw(buf)
        header, resp_payload = parse_atid_response(buf)

        _log.debug("ATID response: %s", header)
        return header, resp_payload

    # ── v2 Protocol Methods ──────────────────────────────────────────────

    def v2_escape(self, buf: bytearray) -> bytearray:
        """Send a pre-built v2 protocol escape buffer.

        The buffer is modified in-place by the driver.  Build it with
        :func:`build_v2_escape` or a command-specific builder first.

        Returns:
            The same bytearray after driver processing.

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
        """
        cmd = struct.unpack_from("<I", buf, V2_CWDDE_OFFSET + 8)[0]
        _log.debug("v2 escape: cmd=0x%08X size=%d", cmd, len(buf))
        return self.escape_raw(buf)

    def read_od_limits(
        self,
    ) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int]]:
        """Read OverDrive limits via v2 escape command 0x00C0009B.

        Sends a 996-byte v2 escape buffer and parses the 3 × 256-byte
        OD limit blocks from the driver response.

        Returns:
            ``(max_limits, advanced_max_limits, min_limits)`` dicts.

        Raises:
            D3DKMTError: If D3DKMTEscape fails.
        """
        buf = build_v2_od_limits_read()
        self.v2_escape(buf)
        return parse_v2_od_limits(buf)

    def read_feature_ctrl_mask(self) -> OdFeatureInfo:
        """Infer the PP_OD_FEATURE bitmask from the OD limits response.

        Sends 0x00C0009B and examines which fields in the max-limits
        block have non-zero values.  Each non-zero field implies the
        corresponding PP_OD_FEATURE bit is enabled by the VBIOS.

        Some features (GFX_VF_CURVE, SOC_VMAX, FULL_CTRL, FCLK) cannot
        be detected from the limits response — use DMA-based PP table
        reads for definitive values.  See ``OdFeatureInfo.undetectable``.

        Returns:
            :class:`OdFeatureInfo` with inferred mask and limit data.

        Raises:
            D3DKMTError: If D3DKMTEscape fails.
        """
        block_max, _block_adv, block_min = self.read_od_limits()
        info = infer_feature_ctrl_mask(block_max, block_min)
        _log.info(
            "FeatureCtrlMask inferred from 0x00C0009B: 0x%04X (%s), "
            "adjustable: 0x%04X (%s), undetectable: 0x%04X",
            info.mask, info.features,
            info.adjustable, info.adjustable_features,
            info.undetectable,
        )
        return info

    def od_initialize(self) -> OdWriteResponse:
        """Send the initial OD activation escape (sub_command=0x02B2, sub_flag=1).

        Adrenalin sends this once on startup to activate the driver's OD
        subsystem.  Without it, subsequent normal applies may be silently
        ignored.  Sets ``+0x0FC = 1`` in the CWDDE params (observed in
        Frida captures but never set by our normal writes).

        This is called automatically by :meth:`od_write` and
        :meth:`od_read_current_values` on the first invocation per client
        instance.  It is safe to call multiple times.

        Returns:
            :class:`OdWriteResponse` from the initialization escape.
        """
        buf = build_v2_od_write(
            {}, sub_command=0x02B2, sub_flag=1)
        struct.pack_into("<I", buf, 0x0FC, 1)
        self.v2_escape(buf)
        resp = parse_v2_od_write_response(buf)
        _log.info(
            "od_initialize response: status=%d data_size=0x%04X "
            "header_size=0x%04X sub_cmd_echo=0x%04X success_flag=%d",
            resp.status, resp.data_size, resp.header_size,
            resp.sub_command_echo, resp.success_flag)
        self._od_initialized = True
        return resp

    def _ensure_od_initialized(self) -> None:
        """Call od_initialize() if not yet done for this client instance."""
        if not self._od_initialized:
            self.od_initialize()

    def od_write(
        self,
        entries: Dict[int, Tuple[int, int]],
        *,
        sub_command: int = 0,
        sub_flag: int = 0,
    ) -> OdWriteResponse:
        """Apply OD8 settings via v2 escape command 0x00C000A1.

        Builds a 2076-byte OD write buffer, sends it, and parses the
        driver response.

        Args:
            entries:     Dict mapping OD8 index (0-72) to ``(value, is_set)``.
            sub_command: 0 for normal apply, 0x02B2 for initial.
            sub_flag:    0 for normal, 1 for initial/reset.

        Returns:
            :class:`OdWriteResponse` with status and echoed values.

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
            ValueError: If any entry index is out of range.
        """
        self._ensure_od_initialized()
        buf = build_v2_od_write(
            entries, sub_command=sub_command, sub_flag=sub_flag)
        self.v2_escape(buf)
        resp = parse_v2_od_write_response(buf)
        _log.info(
            "od_write response: status=%d data_size=0x%04X header_size=0x%04X "
            "sub_cmd_echo=0x%04X success_flag=%d",
            resp.status, resp.data_size, resp.header_size,
            resp.sub_command_echo, resp.success_flag)
        return resp

    def od_read_current_values(self) -> Dict[int, int]:
        """Read current OD8 values via a no-op 0x00C000A1 escape.

        Sends a 2076-byte OD write buffer with all entries having
        ``is_set=0`` (no changes applied).  The driver still calls
        ``GetCurrentSettings`` which fills the response with current
        values for all 73 OD8 indices.

        Returns:
            Dict mapping OD8 index (0-72) to the current i32 value.

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
        """
        self._ensure_od_initialized()
        buf = build_v2_od_write({})
        self.v2_escape(buf)
        resp = parse_v2_od_write_response(buf)
        _log.info(
            "od_read_current_values response: status=%d data_size=0x%04X "
            "header_size=0x%04X sub_cmd_echo=0x%04X success_flag=%d",
            resp.status, resp.data_size, resp.header_size,
            resp.sub_command_echo, resp.success_flag)
        if resp.status != 0:
            _log.warning(
                "OD no-op write returned non-zero status: %d", resp.status)
        return parse_v2_od_write_values(buf)

    # ── SmartShift / GameMode ────────────────────────────────────────────

    def smartshift_read(self) -> SmartShiftResponse:
        """Read current SmartShift delta gain state via 0x00C000AF.

        Returns the current ulMinRange, ulMaxRange, ulDefault, ulModes,
        and ulValue from the SmartShift subsystem.  Read-only, no state
        change.

        Returns:
            :class:`SmartShiftResponse` with current delta gain state.
        """
        buf = build_v2_smartshift_get()
        self.v2_escape(buf)
        resp = parse_v2_smartshift_response(buf, _SS_GET_CWDDE_SIZE)
        _log.info(
            "smartshift_read: success=%s status=0x%02X "
            "min=%d max=%d default=%d modes=%d value=%d",
            resp.success, resp.status_byte,
            resp.min_range, resp.max_range, resp.default,
            resp.modes, resp.value)
        return resp

    def smartshift_set_delta_gain(
        self,
        modes: int = 0,
        value: int = 0,
    ) -> SmartShiftResponse:
        """Set SmartShift delta gain via 0x00C000B0.

        Controls the power/performance budget allocation between CPU and GPU
        in SmartShift-capable systems.  The handler validates the input
        against the min/max range and returns the effective state.

        Sending modes=0 value=0 is a safe read-only probe that returns
        current state and ranges without modification.

        .. warning:: **Hardware-gated**: the CWDDE dispatcher returns
           status 0x06 on desktop RDNA 4 GPUs.  SmartShift requires a
           laptop with AMD CPU+GPU SmartShift support.  On those systems
           the call should succeed; on desktop GPUs it will raise
           :class:`D3DKMTError` or return ``status_byte=0x06``.

        Args:
            modes: SmartShift mode flags (bitmask).
            value: Delta gain value within [min_range, max_range].

        Returns:
            :class:`SmartShiftResponse` with result including ranges.
        """
        buf = build_v2_smartshift_set(modes, value)
        self.v2_escape(buf)
        resp = parse_v2_smartshift_response(buf, _SS_SET_CWDDE_SIZE)
        _log.info(
            "smartshift_set: success=%s status=0x%02X "
            "min=%d max=%d default=%d modes=%d value=%d",
            resp.success, resp.status_byte,
            resp.min_range, resp.max_range, resp.default,
            resp.modes, resp.value)
        return resp

    def gamemode_read(self) -> GameModeResponse:
        """Read current GameMode policy state via 0x00C000B8.

        Returns:
            :class:`GameModeResponse` with current policy state.
        """
        buf = build_v2_gamemode_get()
        self.v2_escape(buf)
        resp = parse_v2_gamemode_response(buf, _GM_GET_CWDDE_SIZE)
        _log.info(
            "gamemode_read: success=%s status=0x%02X states=%d",
            resp.success, resp.status_byte, resp.states)
        return resp

    def gamemode_set_policy(self, states: int = 0) -> GameModeResponse:
        """Set GameMode policy via 0x00C000B9.

        Controls the game mode policy state which may relax thermal and
        power limits for improved gaming performance.

        Sending states=0 disables game mode (safe no-op).

        Args:
            states: Game mode policy state flags.

        Returns:
            :class:`GameModeResponse` with effective state.
        """
        buf = build_v2_gamemode_set(states)
        self.v2_escape(buf)
        resp = parse_v2_gamemode_response(buf, _GM_SET_CWDDE_SIZE)
        _log.info(
            "gamemode_set: success=%s status=0x%02X states=%d",
            resp.success, resp.status_byte, resp.states)
        return resp

    # ── ActivateClient ─────────────────────────────────────────────────

    def activate_client(
        self,
        variant: int = 0x82,
        cwdde_block_size: int = 16,
        resp_size_hint: Optional[int] = None,
    ) -> ActivateClientResponse:
        """Send an ActivateClient escape (0x70/0x71/0x82).

        Attempts to activate the per-process session context that populates
        the driver's feature table.  After a successful activation,
        GetFeatureStatus (0x01) should return real per-feature data instead
        of context-copy template values.

        Args:
            variant:         Handler index: 0x70, 0x71, or 0x82.
            cwdde_block_size: CWDDE block size (>= 16).
            resp_size_hint:  Response size hint at offset 0x0E0.

        Returns:
            :class:`ActivateClientResponse` with output data (if any).

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
        """
        buf = build_v2_activate_client(
            variant, cwdde_block_size, resp_size_hint)
        self.v2_escape(buf)
        resp = parse_v2_activate_client_response(
            buf, variant, cwdde_block_size,
            template=f"c{cwdde_block_size}")
        info = ACTIVATE_CLIENT_VARIANTS.get(variant, {})
        _log.info(
            "activate_client(0x%02X %s): status=0x%02X nonzero=%d/%d "
            "success=%s",
            variant, info.get("label", "?"), resp.status_byte,
            resp.output_nonzero_bytes, resp.output_size, resp.success)
        return resp

    def activate_client_probe(
        self,
        variant: int = 0x82,
    ) -> List[ActivateClientResponse]:
        """Probe an ActivateClient handler with multiple buffer formats.

        Tries CWDDE block sizes 16, 24, 32, 48, and 64 with the correct
        output area for the variant.  Returns all attempts, including
        failures (status 0x06).

        Args:
            variant: Handler index: 0x70, 0x71, or 0x82.

        Returns:
            List of :class:`ActivateClientResponse` for each template tried.
        """
        info = ACTIVATE_CLIENT_VARIANTS.get(variant)
        if info is None:
            raise ValueError(f"Unknown variant 0x{variant:02X}")

        output_size = info["output_size"]
        results: List[ActivateClientResponse] = []

        for cwdde_sz in (16, 24, 32, 48, 64):
            for hint in (output_size, output_size + 8, 0):
                try:
                    buf = build_v2_activate_client(
                        variant, cwdde_sz, hint if hint > 0 else None)
                    self.v2_escape(buf)
                    resp = parse_v2_activate_client_response(
                        buf, variant, cwdde_sz,
                        template=f"c{cwdde_sz}_h{hint}")
                    results.append(resp)
                    if resp.success:
                        _log.info(
                            "activate_client_probe: 0x%02X SUCCESS with "
                            "c%d_h%d (nonzero=%d)",
                            variant, cwdde_sz, hint,
                            resp.output_nonzero_bytes)
                        return results
                except D3DKMTError as e:
                    _log.debug(
                        "activate_client_probe: 0x%02X c%d_h%d failed: %s",
                        variant, cwdde_sz, hint, e)
                    results.append(ActivateClientResponse(
                        variant=variant, success=False,
                        status_byte=0xFF,
                        output_nonzero_bytes=0, output_size=0,
                        raw=b"",
                        template=f"c{cwdde_sz}_h{hint}_ERR"))

        _log.warning(
            "activate_client_probe: 0x%02X — all %d templates failed",
            variant, len(results))
        return results

    # ── CWDDEPM ActivateClient ────────────────────────────────────────

    def cwddepm_activate_client(
        self,
        flags: int = 0,
        *,
        allow_unsafe: bool = False,
    ) -> CwddepmActivateClientResponse:
        """Send CWDDEPM ActivateClient (0xC08008) with BACO control flags.

        This command is routed through the CWDDEPM dispatch path
        (cwddepm_new_path, function table entry [7]).  It is a BACO
        power state control command, NOT a session initializer.

        Safe probe flags:

        - ``flags=0``: no BACO transition, returns 0 if isPPLibActive
        - ``flags=0x200000``: extended mode only, no enter/exit

        .. warning:: Flags 0x10000 (exit BACO) and 0x20000 (enter BACO)
           control GPU power state transitions and may power-cycle the GPU.
           Pass ``allow_unsafe=True`` to use them.

        Args:
            flags:        BACO control bitmask.
            allow_unsafe: Must be True to send BACO enter/exit flags.

        Returns:
            :class:`CwddepmActivateClientResponse` with status and output.

        Raises:
            D3DKMTError: If D3DKMTEscape returns a failure NTSTATUS.
            ValueError: If unsafe flags are set without ``allow_unsafe=True``.
        """
        buf = build_v2_cwddepm_activate_client(
            flags, allow_unsafe=allow_unsafe)
        self.v2_escape(buf)
        resp = parse_v2_cwddepm_activate_client_response(buf, flags)
        _log.info(
            "cwddepm_activate_client(flags=0x%X): status=0x%02X "
            "nonzero=%d success=%s",
            flags, resp.status_byte, resp.output_nonzero_bytes, resp.success)
        return resp

    def query_session(self, sub_query_id: int = 0) -> bytes:
        """Send session/capability query via v2 escape (0x00C00001).

        Args:
            sub_query_id: Feature selector (0x00–0x2A).

        Returns:
            Raw bytes of the full response area (from +0x0E4 onward),
            including the 8-byte format header and 20-byte handler output.

        Raises:
            D3DKMTError: If D3DKMTEscape fails.
        """
        buf = build_v2_session_query(sub_query_id)
        self.v2_escape(buf)
        return bytes(buf[_SESSION_RESP_HDR_OFF:])

    def query_feature_status(
        self,
        sub_query_id: int = 0,
    ) -> SessionQueryResponse:
        """Query a single feature's status via GetFeatureStatus (0x00C00001).

        The session query handler is actually GetFeatureStatus — it returns
        whether the feature is supported, enabled, enabled-by-default, and
        its version number.

        Args:
            sub_query_id: Feature selector (0x00–0x2A).  See
                :data:`SESSION_QUERY_FEATURE_MAP` for known names.

        Returns:
            :class:`SessionQueryResponse` with feature status fields.

        Raises:
            D3DKMTError: If D3DKMTEscape fails.
        """
        buf = build_v2_session_query(sub_query_id)
        self.v2_escape(buf)
        resp = parse_v2_session_query_response(buf, sub_query_id)
        name = session_query_feature_name(sub_query_id)
        _log.info(
            "query_feature_status(0x%02X %s): status=0x%02X "
            "supported=%d enabled=%d default=%d version=%d",
            sub_query_id, name, resp.status_byte,
            resp.feature_supported, resp.feature_enabled,
            resp.feature_enabled_by_default, resp.feature_version)
        return resp

    def enumerate_features(
        self,
        sub_query_ids: Optional[List[int]] = None,
    ) -> Dict[int, SessionQueryResponse]:
        """Enumerate driver feature status by probing all sub_query_ids.

        Sends GetFeatureStatus (0x00C00001) for each sub_query_id and
        collects the responses.  Sub-queries known to be unsupported
        (0x03–0x06, 0x08, 0x0A, 0x18, 0x24) are skipped by default
        but can be included explicitly.

        .. warning:: The handler copies 20 bytes from its internal
           context before looking up each feature.  If the feature table
           hasn't been populated (requires Adrenalin session init), the
           lookup fails and the context-copy values persist.  All
           responses will match ``_CTX_COPY_SIGNATURE`` in that case.
           Use :func:`_is_likely_context_copy` to detect this.

        Args:
            sub_query_ids: List of sub-query IDs to probe.
                Defaults to all valid IDs (0x00–0x2A excluding known
                unsupported ones).

        Returns:
            Dict mapping sub_query_id to :class:`SessionQueryResponse`.
        """
        if sub_query_ids is None:
            sub_query_ids = [
                i for i in SESSION_QUERY_SUB_IDS
                if i not in _UNSUPPORTED_SUB_QUERIES
            ]

        results: Dict[int, SessionQueryResponse] = {}
        for sq_id in sub_query_ids:
            try:
                results[sq_id] = self.query_feature_status(sq_id)
            except D3DKMTError as e:
                _log.warning(
                    "enumerate_features: sub_query 0x%02X failed: %s",
                    sq_id, e)

        ctx_copy_count = sum(
            1 for r in results.values() if _is_likely_context_copy(r))
        if ctx_copy_count == len(results) and results:
            _log.warning(
                "enumerate_features: all %d responses match the context-"
                "copy signature {0x14, 1, 0, 1, 4} — the feature table "
                "is likely not populated (requires Adrenalin session "
                "init via FUN_14146c05c)",
                len(results))
        elif ctx_copy_count > 0:
            _log.info(
                "enumerate_features: %d/%d responses are context-copy, "
                "%d appear to have real feature data",
                ctx_copy_count, len(results),
                len(results) - ctx_copy_count)

        return results

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the device (if any) and adapter handle."""
        if self._closed:
            return
        self._closed = True

        self.destroy_device()

        handle = self._adapter.handle
        params = _D3DKMT_CLOSEADAPTER()
        params.hAdapter = handle
        try:
            status = _D3DKMTCloseAdapter(ctypes.byref(params))
            if status == STATUS_SUCCESS:
                _log.info("Closed adapter %d", handle)
            else:
                _log.warning("D3DKMTCloseAdapter: NTSTATUS 0x%08X",
                             status & 0xFFFFFFFF)
        except Exception as e:
            _log.warning("D3DKMTCloseAdapter: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ── Adapter Enumeration (D3DKMTEnumAdapters3) ────────────────────────────

def enumerate_adapters() -> List[Tuple[int, int, int]]:
    """Enumerate WDDM adapters via D3DKMTEnumAdapters3.

    Returns:
        List of ``(hAdapter, luid_low, luid_high)`` tuples.

    The returned handles are from the enumeration API.  To send escape
    commands, use D3DKMTClient.open() with a device interface path
    instead.

    Raises:
        RuntimeError: If D3DKMTEnumAdapters3 is unavailable.
    """
    if _D3DKMTEnumAdapters3 is None:
        raise RuntimeError(
            "D3DKMTEnumAdapters3 not available (requires Windows 10 1803+)")

    params = _D3DKMT_ENUMADAPTERS3()
    params.Filter = 0
    params.NumAdapters = 0
    params.pAdapters = None

    # First call with pAdapters=NULL to get the adapter count
    _D3DKMTEnumAdapters3(ctypes.byref(params))
    count = params.NumAdapters
    if count == 0:
        return []

    arr = (_D3DKMT_ADAPTERINFO * count)()
    params.NumAdapters = count
    params.pAdapters = arr

    status = _D3DKMTEnumAdapters3(ctypes.byref(params))
    _check(status, "D3DKMTEnumAdapters3")

    return [
        (arr[i].hAdapter,
         arr[i].AdapterLuid.LowPart,
         arr[i].AdapterLuid.HighPart)
        for i in range(params.NumAdapters)
    ]


# ── CLI / Probe ──────────────────────────────────────────────────────────

def _print_od_block(label: str, block: Dict[str, int]) -> None:
    """Print an OD limits response block."""
    print(f"\n  {label}:")
    for name, val in block.items():
        print(f"    {name:30s}: {val}")


def main() -> int:
    """Probe the D3DKMT escape interface using the v2 protocol."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(name)s %(levelname)s: %(message)s")

    print("=" * 65)
    print("  D3DKMTEscape Client — v2 Protocol Probe")
    print("=" * 65)

    # ── Step 1: GDI Display Devices ──────────────────────────────────────

    print("\n--- GDI Display Devices ---")
    gdi_devs = enumerate_display_devices()
    for dd in gdi_devs:
        flags = ""
        if dd.is_active:
            flags += " ACTIVE"
        if dd.is_primary:
            flags += " PRIMARY"
        is_amd = "VEN_1002" in dd.device_id.upper()
        tag = " [AMD]" if is_amd else ""
        print(f"  {dd.name} = {dd.description}{flags}{tag}")

    # ── Step 2: Open AMD Adapter ─────────────────────────────────────────

    print("\n--- Open AMD Adapter ---")
    try:
        client = D3DKMTClient.open_amd_adapter()
    except (RuntimeError, D3DKMTError, OSError) as e:
        print(f"  FAILED: {e}")
        return 1

    a = client.adapter
    print(f"  Handle:  {a.handle}")
    print(f"  LUID:    0x{a.luid:016X}")

    # ── Step 3: v2 Session Query / GetFeatureStatus (0x00C00001) ─────────

    print("\n--- v2 Escape: GetFeatureStatus (0x00C00001) ---")
    try:
        resp = client.query_feature_status(0x00)
        print("  STATUS_SUCCESS")
        name = session_query_feature_name(0x00)
        print(f"  Feature 0x00 ({name}):")
        print(f"    Supported:         {resp.feature_supported}")
        print(f"    Enabled:           {resp.feature_enabled}")
        print(f"    EnabledByDefault:  {resp.feature_enabled_by_default}")
        print(f"    Version:           {resp.feature_version}")
        if _is_likely_context_copy(resp):
            print(f"    ** CONTEXT COPY — feature table not populated **")

        raw_resp = client.query_session(0x00)
        hex_resp = " ".join(f"{b:02X}" for b in raw_resp[:32])
        print(f"  Raw response (first 32 B): {hex_resp}")
    except D3DKMTError as e:
        print(f"  FAILED: {e}")

    # ── Step 4: v2 OD Limits Read (0x00C0009B) ──────────────────────────

    print("\n--- v2 Escape: OD Limits Read (0x00C0009B) ---")
    od_ok = False
    try:
        block_max, block_adv, block_min = client.read_od_limits()
        print("  STATUS_SUCCESS — OD limits data received")

        _print_od_block("Block 0 (Basic Max)", block_max)
        _print_od_block("Block 1 (Advanced Max)", block_adv)
        _print_od_block("Block 2 (Min)", block_min)

        gfx_max = block_max.get("GfxclkFmax", 0)
        uclk_max = block_max.get("UclkFmax", 0)
        print(f"\n  Sanity check:")
        if gfx_max > 1000 and uclk_max > 500:
            print(f"    PASS — GfxclkFmax={gfx_max} MHz, UclkFmax={uclk_max} MHz")
            od_ok = True
        else:
            print(f"    WARNING — values look unexpected "
                  f"(GfxclkFmax={gfx_max}, UclkFmax={uclk_max})")

    except D3DKMTError as e:
        print(f"  FAILED: {e}")
        buf = build_v2_od_limits_read()
        hex_head = " ".join(f"{b:02X}" for b in buf[:64])
        print(f"  Request[0:64]: {hex_head}")

    # ── Step 5: FeatureCtrlMask Inference ────────────────────────────────

    feature_info: Optional[OdFeatureInfo] = None
    print("\n--- FeatureCtrlMask (inferred from 0x00C0009B) ---")
    try:
        feature_info = client.read_feature_ctrl_mask()
        print(f"  Inferred mask:     0x{feature_info.mask:04X}")
        print(f"  Features present:  {feature_info.features!r}")
        print(f"  Adjustable mask:   0x{feature_info.adjustable:04X}")
        print(f"  Adjustable:        {feature_info.adjustable_features!r}")
        print(f"  Undetectable:      0x{feature_info.undetectable:04X}")
        print()
        for feat in PpOdFeature:
            if feat & feature_info.undetectable:
                tag = "UNDETECTABLE (needs DMA)"
            elif feat & feature_info.adjustable:
                tag = "YES (adjustable)"
            elif feat & feature_info.mask:
                tag = "YES (fixed range)"
            else:
                tag = "NO"
            print(f"    {feat.name:20s} (bit {feat.bit_length() - 1:2d}): {tag}")
    except D3DKMTError as e:
        print(f"  FAILED: {e}")

    client.close()
    print("\n  Adapter closed.")

    # ── Summary ──────────────────────────────────────────────────────────

    print("\n--- Summary ---")
    print(f"  Protocol:  v2 (version=0x00000002, CWDDE at +0x0CC)")
    print(f"  Adapter:   handle={a.handle}, LUID=0x{a.luid:016X}")
    status = "OD limits read WORKING" if od_ok else "OD limits read FAILED"
    print(f"  Status:    {status}")
    if feature_info:
        print(f"  Features:  0x{feature_info.mask:04X} "
              f"(adjustable: 0x{feature_info.adjustable:04X})")
    print(f"\n  Next steps:")
    print(f"    1. Probe unknown OD8 indices with per-index active probe")
    print(f"    2. Map indices to OverDriveTable_t fields via DMA diff")
    print(f"    3. Read GFX_VF_CURVE/SOC_VMAX/FULL_CTRL/FCLK via PP table DMA")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
