"""
SMU MMIO Access Layer via WinRing0
===================================

Provides low-level GPU register access for direct SMU communication:
  - PCI config space read/write (via WinRing0 DLL) -- always works
  - Physical memory read (via WinRing0 driver IOCTL, NOP-patched)
  - MMIO register write (via I/O BAR direct port writes)
  - SMN bus read/write (via MMIO BAR index/data register pair)

Access Strategy:
  READS:  Physical memory read at BAR + offset.  Uses the NOP-patched
          WinRing0x64_patched.sys which removes the 1MB MmMapIoSpace
          restriction.  This is stable and proven safe.

  WRITES: I/O BAR direct port writes.  The GPU's I/O BAR (typically port
          0xEF00) maps MMIO registers at direct port offsets:
            port IO_BAR + offset  =>  writes to BAR + offset
          This uses the ORIGINAL WinRing0 DLL's WriteIoPortDword function --
          no custom driver code needed.  Requires I/O Space to be enabled
          in the PCI Command register (bit 0).

  The WriteMemory IOCTL code cave in the patched driver is NOT used.  It has
  a bug that causes KMODE_EXCEPTION_NOT_HANDLED BSODs.

Requirements:
  - WinRing0x64.dll in overclocking/drivers/ (or script directory)
  - WinRing0x64_patched.sys (for physical memory reads beyond 1MB)
  - inpoutx64.dll in overclocking/drivers/ (or InpOutBinaries/x64/)
  - Test signing enabled: bcdedit /set testsigning on
  - Run as Administrator
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import time
import shutil


def require_admin():
    """Check that we're running as Administrator.  Raise if not."""
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False
    if not is_admin:
        raise PermissionError(
            "This script must be run as Administrator.\n"
            "Right-click your terminal / IDE and choose 'Run as administrator',\n"
            "then re-run this script."
        )


# ---------------------------------------------------------------------------
# WinRing0 driver IOCTL definitions (from DriverIoCtl.h)
# ---------------------------------------------------------------------------

OLS_TYPE = 40000  # 0x9C40

def _CTL_CODE(device_type, function, method, access):
    return (device_type << 16) | (access << 14) | (function << 2) | method

_METHOD_BUFFERED    = 0
_FILE_READ_ACCESS   = 1
_FILE_WRITE_ACCESS  = 2

IOCTL_OLS_GET_DRIVER_VERSION = _CTL_CODE(OLS_TYPE, 0x800, _METHOD_BUFFERED, 0)
IOCTL_OLS_READ_MEMORY        = _CTL_CODE(OLS_TYPE, 0x841, _METHOD_BUFFERED, _FILE_READ_ACCESS)
IOCTL_OLS_WRITE_MEMORY       = _CTL_CODE(OLS_TYPE, 0x842, _METHOD_BUFFERED, _FILE_WRITE_ACCESS)
IOCTL_OLS_MAP_PHYS_TO_LIN    = _CTL_CODE(OLS_TYPE, 0x840, _METHOD_BUFFERED,
                                          _FILE_READ_ACCESS | _FILE_WRITE_ACCESS)
IOCTL_OLS_UNMAP_PHYS          = _CTL_CODE(OLS_TYPE, 0x843, _METHOD_BUFFERED,
                                          _FILE_READ_ACCESS | _FILE_WRITE_ACCESS)

# Driver device name (from OLS_DRIVER_ID in DriverIoCtl.h)
WR0_DEVICE_NAME = r"\\.\WinRing0_1_2_0"

# Win32 constants
GENERIC_READ       = 0x80000000
GENERIC_WRITE      = 0x40000000
OPEN_EXISTING      = 3

# WinRing0 DLL status codes
_STATUS_MESSAGES = {
    0: "No error",
    1: "Unsupported platform",
    2: "Driver not loaded (is test signing enabled? Run as admin?)",
    3: "Driver file (.sys) not found next to the DLL",
    4: "Driver was unloaded unexpectedly",
    5: "Cannot load driver from a network path",
    9: "Unknown error",
}

# ---------------------------------------------------------------------------
# IOCTL structures (packed to match driver's #pragma pack(push, 4))
# ---------------------------------------------------------------------------

class _OLS_READ_MEMORY_INPUT(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("Address",  ctypes.c_int64),   # PHYSICAL_ADDRESS (LARGE_INTEGER)
        ("UnitSize", ctypes.c_uint32),  # ULONG
        ("Count",    ctypes.c_uint32),  # ULONG
    ]


class _OLS_MAP_PHYS_INPUT(ctypes.Structure):
    """Input for IOCTL_OLS_MAP_PHYS_TO_LIN -- maps physical memory to user space."""
    _pack_ = 4
    _fields_ = [
        ("PhysicalAddress", ctypes.c_int64),   # LARGE_INTEGER
        ("MemorySize",      ctypes.c_uint32),  # ULONG
    ]


def _make_write_mem_buf(phys_addr, data_bytes):
    """Build packed buffer for IOCTL_OLS_WRITE_MEMORY.

    Layout: Address(8) + UnitSize(4) + Count(4) + Data(N)
    """
    buf = struct.pack("<qII", phys_addr, len(data_bytes), 1) + data_bytes
    return (ctypes.c_char * len(buf)).from_buffer_copy(buf)


# ---------------------------------------------------------------------------
# WinRing0 class
# ---------------------------------------------------------------------------

class WinRing0:
    """
    Low-level interface to WinRing0.

    Provides:
      - PCI config space read/write (via DLL)
      - PCI device enumeration (via DLL)
      - Physical memory read/write (via driver IOCTL)
      - I/O port read/write (via DLL)
    """

    def __init__(self, dll_path=None, prefer_patched=False):
        self._dll = None
        self._dev_handle = None
        self._initialized = False
        self._kernel32 = ctypes.windll.kernel32
        self._has_phys_patch = False  # True if patched driver loaded

        # --- Require admin ---
        require_admin()

        # --- Try to load the driver ---
        self._setup_kernel32()
        loaded = False

        if prefer_patched:
            # Try patched driver first (full physical memory support)
            try:
                dll_path, _ = self._ensure_installed(use_patched=True)
                self._stop_existing_service()
                self._dll = ctypes.WinDLL(dll_path)
                self._setup_dll_functions()
                if self._dll.InitializeOls():
                    self._initialized = True
                    self._has_phys_patch = True
                    loaded = True
                    print("[WR0] Patched driver loaded OK")
                else:
                    self._dll = None
            except Exception:
                self._dll = None

        if not loaded:
            # Fall back to original driver
            try:
                dll_path, _ = self._ensure_installed(use_patched=False)
                self._stop_existing_service()
                if self._dll is None:
                    self._dll = ctypes.WinDLL(dll_path)
                    self._setup_dll_functions()
                else:
                    # DLL already loaded, just re-init
                    pass
                if not self._dll.InitializeOls():
                    # Last resort: try loading DLL fresh
                    self._dll = ctypes.WinDLL(dll_path)
                    self._setup_dll_functions()
                    if not self._dll.InitializeOls():
                        status = self._dll.GetDllStatus()
                        msg = _STATUS_MESSAGES.get(status, f"Unknown status code {status}")
                        raise RuntimeError(f"WinRing0 initialization failed: {msg}")
                self._initialized = True
                loaded = True
                print("[WR0] Original driver loaded OK (PCI config access available, "
                      "physical memory limited to 1MB)")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"WinRing0 initialization failed: {e}")

        # --- Open direct handle to driver for IOCTL ---
        self._open_device()

    # -- Service management --

    @staticmethod
    def _stop_existing_service():
        """Stop and delete the existing WinRing0 driver service.
        
        This ensures the DLL's InitializeOls() loads the FRESH .sys
        file (e.g., the patched version) rather than using an already-
        running old driver.
        """
        import subprocess
        svc_name = "WinRing0_1_2_0"
        try:
            subprocess.run(
                ["sc", "stop", svc_name],
                capture_output=True, timeout=10,
            )
            time.sleep(0.5)
            subprocess.run(
                ["sc", "delete", svc_name],
                capture_output=True, timeout=10,
            )
            time.sleep(0.5)
        except Exception:
            pass  # Service may not exist yet -- that's fine

    # -- File location and installation --

    @staticmethod
    def _ensure_installed(use_patched=True):
        """Ensure WinRing0 DLL+SYS are next to python.exe.

        The DLL resolves the .sys path via GetModuleFileName(NULL) which
        returns the host EXE path.  We must ensure both files are in the
        Python executable's directory.

        Args:
            use_patched: If True and available, use the patched .sys file.
                         If False, always use the original .sys file.
        """
        py_dir = os.path.dirname(sys.executable)
        target_dll = os.path.join(py_dir, "WinRing0x64.dll")
        target_sys = os.path.join(py_dir, "WinRing0x64.sys")

        # Search for source files in script dir and CWD
        script_dir = os.path.dirname(os.path.abspath(
            sys.modules[__name__].__file__
            if hasattr(sys.modules[__name__], '__file__')
            else __file__
        ))
        drivers_dir = os.path.join(script_dir, "drivers")
        search_dirs = list(dict.fromkeys([drivers_dir, script_dir, os.getcwd()]))

        source_dll = source_sys = None
        patched_sys = None
        original_sys = None

        for d in search_dirs:
            if source_dll is None:
                p = os.path.join(d, "WinRing0x64.dll")
                if os.path.isfile(p):
                    source_dll = p
            if patched_sys is None:
                p = os.path.join(d, "WinRing0x64_patched.sys")
                if os.path.isfile(p):
                    patched_sys = p
            if original_sys is None:
                p = os.path.join(d, "WinRing0x64.sys")
                if os.path.isfile(p):
                    original_sys = p

        # Select driver
        if use_patched and patched_sys:
            source_sys = patched_sys
            print("[WR0] Trying PATCHED driver (full physical memory support)")
        elif original_sys:
            source_sys = original_sys
            print("[WR0] Using ORIGINAL driver (PCI config OK, phys memory limited to 1MB)")
        elif patched_sys:
            source_sys = patched_sys
            print("[WR0] Using PATCHED driver (no original available)")
        
        missing = []
        if source_dll is None:
            missing.append("WinRing0x64.dll")
        if source_sys is None:
            missing.append("WinRing0x64.sys or WinRing0x64_patched.sys")
        if missing:
            raise FileNotFoundError(
                f"Missing: {', '.join(missing)}\n"
                f"Place files in: {script_dir}\n"
            )

        # Always copy fresh to ensure we're using the right version
        shutil.copy2(source_dll, target_dll)
        shutil.copy2(source_sys, target_sys)

        return target_dll, target_sys

    # -- DLL function signatures --

    def _setup_dll_functions(self):
        d = self._dll
        # Core
        d.InitializeOls.restype = wt.BOOL
        d.InitializeOls.argtypes = []
        d.DeinitializeOls.restype = None
        d.DeinitializeOls.argtypes = []
        d.GetDllStatus.restype = wt.DWORD
        d.GetDllStatus.argtypes = []
        # PCI enumeration
        d.FindPciDeviceById.restype = wt.DWORD
        d.FindPciDeviceById.argtypes = [wt.WORD, wt.WORD, wt.BYTE]
        d.FindPciDeviceByClass.restype = wt.DWORD
        d.FindPciDeviceByClass.argtypes = [wt.BYTE, wt.BYTE, wt.BYTE, wt.BYTE]
        # PCI config -- legacy (reg 0-255)
        d.ReadPciConfigByte.restype = wt.BYTE
        d.ReadPciConfigByte.argtypes = [wt.DWORD, wt.BYTE]
        d.ReadPciConfigDword.restype = wt.DWORD
        d.ReadPciConfigDword.argtypes = [wt.DWORD, wt.BYTE]
        # PCI config -- extended (reg 0-4095)
        d.ReadPciConfigDwordEx.restype = wt.BOOL
        d.ReadPciConfigDwordEx.argtypes = [wt.DWORD, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        d.WritePciConfigDwordEx.restype = wt.BOOL
        d.WritePciConfigDwordEx.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD]
        # I/O ports
        d.ReadIoPortDword.restype = wt.DWORD
        d.ReadIoPortDword.argtypes = [wt.WORD]
        d.WriteIoPortDword.restype = None
        d.WriteIoPortDword.argtypes = [wt.WORD, wt.DWORD]

    def _setup_kernel32(self):
        k = self._kernel32
        k.CreateFileW.restype = wt.HANDLE
        k.CreateFileW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
            wt.DWORD, wt.DWORD, wt.HANDLE,
        ]
        k.DeviceIoControl.restype = wt.BOOL
        k.DeviceIoControl.argtypes = [
            wt.HANDLE, wt.DWORD,
            ctypes.c_void_p, wt.DWORD,
            ctypes.c_void_p, wt.DWORD,
            ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
        ]
        k.CloseHandle.restype = wt.BOOL
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.GetLastError.restype = wt.DWORD
        k.GetLastError.argtypes = []

    # -- Driver device handle --

    def _open_device(self):
        h = self._kernel32.CreateFileW(
            WR0_DEVICE_NAME,
            GENERIC_READ | GENERIC_WRITE,
            0, None, OPEN_EXISTING, 0, None,
        )
        if h is None or h == wt.HANDLE(-1).value or h == 0:
            err = self._kernel32.GetLastError()
            raise RuntimeError(
                f"Cannot open WinRing0 device ({WR0_DEVICE_NAME}), error={err}.\n"
                "Is the driver loaded?  Run as Administrator."
            )
        self._dev_handle = h

    def _ioctl(self, code, in_buf, in_size, out_buf, out_size):
        bytes_ret = wt.DWORD(0)
        ok = self._kernel32.DeviceIoControl(
            self._dev_handle, code,
            in_buf, in_size,
            out_buf, out_size,
            ctypes.byref(bytes_ret), None,
        )
        if not ok:
            err = self._kernel32.GetLastError()
            raise IOError(
                f"DeviceIoControl failed, ioctl=0x{code:08X}, error={err}"
            )
        return bytes_ret.value

    # -- Physical memory read/write (via driver IOCTL) --

    def read_phys32(self, phys_addr):
        """Read a 32-bit DWORD from a physical address."""
        inp = _OLS_READ_MEMORY_INPUT(Address=phys_addr, UnitSize=4, Count=1)
        out = wt.DWORD(0)
        self._ioctl(
            IOCTL_OLS_READ_MEMORY,
            ctypes.byref(inp), ctypes.sizeof(inp),
            ctypes.byref(out), 4,
        )
        return out.value

    def write_phys32(self, phys_addr, value):
        """Write a 32-bit DWORD to a physical address."""
        buf = _make_write_mem_buf(phys_addr, struct.pack("<I", value & 0xFFFFFFFF))
        self._ioctl(
            IOCTL_OLS_WRITE_MEMORY,
            ctypes.byref(buf), len(buf),
            None, 0,
        )

    def read_phys_block(self, phys_addr, size):
        """Read a block of bytes from physical memory."""
        inp = _OLS_READ_MEMORY_INPUT(Address=phys_addr, UnitSize=1, Count=size)
        out = (ctypes.c_char * size)()
        self._ioctl(
            IOCTL_OLS_READ_MEMORY,
            ctypes.byref(inp), ctypes.sizeof(inp),
            out, size,
        )
        return bytes(out)

    def can_read_phys(self, phys_addr):
        """Test whether a physical address is readable (non-destructive)."""
        try:
            self.read_phys32(phys_addr)
            return True
        except IOError:
            return False

    # -- Physical memory mapping (via driver IOCTL) --

    def map_phys(self, phys_addr, size):
        """Map a physical address range into user-space virtual memory.

        Returns a user-space virtual address (integer) that can be used
        with ctypes to read/write the mapped region directly.  This is
        the equivalent of Linux's ioremap() -- proper memory-mapped I/O.

        The caller MUST call unmap_phys() when done.

        Args:
            phys_addr: Physical address to map.
            size:      Number of bytes to map.

        Returns:
            User-space virtual address (integer).

        Raises:
            IOError: If the mapping fails.
        """
        inp = _OLS_MAP_PHYS_INPUT(
            PhysicalAddress=phys_addr,
            MemorySize=size,
        )
        # Output is a pointer-sized value (8 bytes on 64-bit)
        out = ctypes.c_uint64(0)
        self._ioctl(
            IOCTL_OLS_MAP_PHYS_TO_LIN,
            ctypes.byref(inp), ctypes.sizeof(inp),
            ctypes.byref(out), ctypes.sizeof(out),
        )
        addr = out.value
        if addr == 0:
            raise IOError(
                f"MapPhysToLin returned NULL for phys=0x{phys_addr:X}, size={size}.\n"
                "The driver may not support physical memory mapping at this address."
            )
        return addr

    def unmap_phys(self, virt_addr, size):
        """Unmap a previously mapped physical memory region.

        Args:
            virt_addr: Virtual address returned by map_phys().
            size:      Size that was passed to map_phys().
        """
        # Input is the virtual address + size
        inp = _OLS_MAP_PHYS_INPUT(
            PhysicalAddress=virt_addr,
            MemorySize=size,
        )
        try:
            self._ioctl(
                IOCTL_OLS_UNMAP_PHYS,
                ctypes.byref(inp), ctypes.sizeof(inp),
                None, 0,
            )
        except IOError:
            pass  # Best effort

    # -- I/O port access (via DLL) --

    def read_io32(self, port):
        """Read a 32-bit DWORD from an I/O port."""
        return self._dll.ReadIoPortDword(port)

    def write_io32(self, port, value):
        """Write a 32-bit DWORD to an I/O port."""
        self._dll.WriteIoPortDword(port, value & 0xFFFFFFFF)

    # -- PCI config space --

    def find_pci_device(self, vendor_id, device_id, index=0):
        """Find PCI device by vendor/device ID.  Returns PCI address or raises."""
        addr = self._dll.FindPciDeviceById(vendor_id, device_id, index)
        if addr == 0xFFFFFFFF:
            raise RuntimeError(f"PCI device {vendor_id:04X}:{device_id:04X} not found")
        return addr

    def find_pci_by_class(self, base_class, sub_class, prog_if=0, index=0):
        """Find PCI device by class code."""
        addr = self._dll.FindPciDeviceByClass(base_class, sub_class, prog_if, index)
        if addr == 0xFFFFFFFF:
            raise RuntimeError(
                f"PCI class {base_class:02X}:{sub_class:02X}:{prog_if:02X} not found"
            )
        return addr

    def read_pci_dword(self, pci_addr, reg):
        """Read DWORD from PCI config (reg 0-255)."""
        return self._dll.ReadPciConfigDword(pci_addr, reg & 0xFF)

    def read_pci_byte(self, pci_addr, reg):
        """Read byte from PCI config (reg 0-255)."""
        return self._dll.ReadPciConfigByte(pci_addr, reg & 0xFF)

    def read_pci_dword_ex(self, pci_addr, reg):
        """Read DWORD from extended PCI config (reg 0-4095)."""
        val = wt.DWORD(0)
        ok = self._dll.ReadPciConfigDwordEx(pci_addr, reg, ctypes.byref(val))
        if not ok:
            raise IOError(f"ReadPciConfigDwordEx failed: pci=0x{pci_addr:X}, reg=0x{reg:X}")
        return val.value

    def write_pci_dword_ex(self, pci_addr, reg, value):
        """Write DWORD to extended PCI config (reg 0-4095)."""
        ok = self._dll.WritePciConfigDwordEx(pci_addr, reg, value & 0xFFFFFFFF)
        if not ok:
            raise IOError(f"WritePciConfigDwordEx failed: pci=0x{pci_addr:X}, reg=0x{reg:X}")

    # -- Lifecycle --

    def close(self):
        if self._dev_handle is not None:
            self._kernel32.CloseHandle(self._dev_handle)
            self._dev_handle = None
        if self._initialized:
            self._dll.DeinitializeOls()
            self._initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GpuMMIO class
# ---------------------------------------------------------------------------

class GpuMMIO:
    """
    GPU MMIO register and SMN bus access.

    Access methods:

    1. MMIO Reads: Physical memory read at BAR + offset.
       Requires NOP-patched WinRing0x64_patched.sys for addresses > 1MB.

    2. MMIO Writes: I/O BAR direct port writes.
       The GPU's I/O BAR maps MMIO registers at direct port offsets:
         port IO_BAR + offset => writes to MMIO BAR + offset
       Requires I/O Space enabled in PCI Command register (managed automatically).
       Uses original WinRing0 DLL WriteIoPortDword -- no patched driver needed.

    3. SMN (System Management Network) access:
       Uses MMIO BAR index/data register pair:
         - RDNA4 (NBIO v7.11): PCIE_INDEX2/DATA2 at byte offsets 0x38/0x3C
         - Secondary pair: PCIE_INDEX/DATA at 0x30/0x34
         - Legacy GPUs: 0x60/0x64
       NOTE: Offset 0xC8/0xCC is INVALID on RDNA4 and causes instant reboot!

    4. PCI config indirect SMN access (probed but may not exist on RDNA4):
       Uses PCI extended config registers 0xE0/0xE4 or 0xC0/0xC4.
    """

    # PCI config indirect register pairs for SMN access
    PCIE_SMN_PAIRS = {
        "nbio_v7":  (0xE0, 0xE4),   # RDNA2+ (NBIO v7.4+, v7.9, v11.0)
        "legacy":   (0xC0, 0xC4),   # Older GPUs
    }

    # MMIO BAR-based SMN index/data pairs
    # Probed in order -- RDNA4 pair first, then secondary, then legacy.
    # NOTE: 0xC8/0xCC is INVALID on RDNA4 (NBIO v7.11) and will crash the GPU!
    MMIO_SMN_PAIRS = {
        "rdna4":     (0x38, 0x3C),  # PCIE_INDEX2/DATA2 (NBIO v7.11, RDNA4)
        "rdna4_alt": (0x30, 0x34),  # PCIE_INDEX/DATA   (NBIO v7.11 secondary)
        "legacy":    (0x60, 0x64),  # Older GPUs
    }

    def __init__(self, winring0, bar_phys_addr, pci_addr=None,
                 io_bar_port=None, smn_pair="auto", inpout=None):
        """
        Args:
            winring0:       WinRing0 instance (or None if using InpOut32-only mode).
            bar_phys_addr:  Physical address of the GPU's MMIO register BAR.
            pci_addr:       PCI address of the GPU (bus/dev/func encoded).
                            Required for PCI config indirect SMN and I/O Space mgmt.
            io_bar_port:    I/O BAR base port (e.g. 0xEF00). If provided, used
                            for MMIO writes via direct port offset mapping.
            smn_pair:       "auto", or force a specific pair name.
            inpout:         Optional InpOut32 instance for memory-mapped writes
                            and (when winring0 is None) physical memory reads.
                            This is the preferred write backend for SMN transactions
                            because I/O port writes to DATA2 do NOT trigger SMN
                            write transactions on RDNA4 (NBIO v7.11).
        """
        self._wr0 = winring0
        self._bar = bar_phys_addr
        self._pci_addr = pci_addr
        self._inpout = inpout  # InpOut32 for memory-mapped writes (and reads if no WR0)

        # I/O BAR state (for MMIO writes via direct port offset mapping)
        self._io_port = io_bar_port
        self._io_space_was_on = None  # Original state of I/O Space enable bit
        self._io_space_enabled = False

        # PCI config indirect SMN state
        self._pcie_smn_idx = None
        self._pcie_smn_data = None
        self._use_pcie_smn = False

        # MMIO BAR-based SMN state (default to RDNA4 offsets)
        self._smn_idx = 0x38
        self._smn_data = 0x3C

        # Check if physical memory access works for this BAR
        # Try WinRing0 first, fall back to InpOut32
        self._use_phys = False
        self._phys_reader = None  # Object that provides read_phys32()
        if winring0 is not None and winring0.can_read_phys(bar_phys_addr):
            self._use_phys = True
            self._phys_reader = winring0
            print(f"[MMIO] Physical memory read OK at BAR 0x{bar_phys_addr:X}")
        elif inpout is not None and inpout.can_read_phys(bar_phys_addr):
            self._use_phys = True
            self._phys_reader = inpout
            print(f"[MMIO] Physical memory read OK via InpOut32 at BAR 0x{bar_phys_addr:X}")
        else:
            print(f"[MMIO] Physical memory read BLOCKED for BAR 0x{bar_phys_addr:X}")
            if winring0 is None:
                print(f"[MMIO] InpOut32 could not read physical memory at this address.")
            else:
                print(f"[MMIO] Need patched driver for physical memory reads beyond 1MB.")

        # --- InpOut32 memory-mapped write backend ---
        if self._inpout is not None:
            print(f"[MMIO] InpOut32 available for memory-mapped writes")

        # --- Enable I/O Space for MMIO writes (fallback) ---
        if self._io_port is not None and pci_addr is not None:
            self._enable_io_space()
            if self._io_space_enabled:
                print(f"[MMIO] I/O BAR writes enabled at port 0x{self._io_port:04X}")
            else:
                print(f"[MMIO] WARNING: Could not enable I/O Space for writes")
        elif self._io_port is not None:
            print(f"[MMIO] I/O BAR port 0x{self._io_port:04X} (no PCI addr, "
                  f"cannot manage I/O Space)")

        # --- Probe PCI config indirect SMN access (preferred) ---
        if pci_addr is not None:
            self._probe_pcie_smn()

        # --- Fallback: probe MMIO BAR SMN pair ---
        if not self._use_pcie_smn and self._use_phys:
            if smn_pair == "auto":
                self._smn_idx, self._smn_data = self._probe_mmio_smn_pair()
            elif smn_pair in self.MMIO_SMN_PAIRS:
                self._smn_idx, self._smn_data = self.MMIO_SMN_PAIRS[smn_pair]

        # Summarize access capabilities
        can_read = self._use_phys
        can_io_write = self._io_space_enabled and self._io_port is not None
        can_mmio_write = self._inpout is not None
        print(f"[MMIO] read={'phys_mem' if can_read else 'NONE'}, "
              f"write={'inpout32_mmio' if can_mmio_write else ('io_bar' if can_io_write else 'NONE')}")
        if self._use_pcie_smn:
            print(f"[SMN] Using PCI config indirect "
                  f"(idx=0x{self._pcie_smn_idx:02X}, data=0x{self._pcie_smn_data:02X})")
        elif can_read and (can_mmio_write or can_io_write):
            method = "inpout32_mmio" if can_mmio_write else "io_bar"
            print(f"[SMN] Using MMIO BAR pair "
                  f"(idx=0x{self._smn_idx:02X}, data=0x{self._smn_data:02X})"
                  f" -- read:phys_mem, write:{method}")
        elif can_read:
            print(f"[SMN] Read-only via MMIO BAR (no write path available)")
        else:
            print(f"[SMN] No SMN access available")

    # ---- MMIO register access ----

    def read32(self, byte_offset):
        """Read 32-bit register at MMIO BAR + byte_offset.

        Uses physical memory read via WinRing0 or InpOut32.
        """
        if self._use_phys:
            return self._phys_reader.read_phys32(self._bar + byte_offset)
        raise IOError(
            "Physical memory access blocked for MMIO BAR.\n"
            "Need InpOut32 or NOP-patched WinRing0 for physical memory reads."
        )

    def write32(self, byte_offset, value):
        """Write 32-bit register at MMIO BAR + byte_offset.

        Preferred: InpOut32 SetPhysLong (true memory-mapped write, triggers
        SMN write transactions).

        Fallback: I/O BAR direct port writes (port IO_BAR + offset => BAR +
        offset).  Works for INDEX registers but NOT for DATA registers that
        trigger SMN write transactions on RDNA4.
        """
        if self._inpout is not None:
            self._inpout.write_phys32(self._bar + byte_offset, value)
            return
        if self._io_space_enabled and self._io_port is not None:
            io_writer = self._wr0 or self._inpout
            if io_writer is not None:
                io_writer.write_io32(self._io_port + byte_offset, value)
                return
        raise IOError(
            "MMIO write not available.\n"
            "Need InpOut32 (preferred) or I/O BAR + I/O Space enabled."
        )

    # ---- SMN bus access ----

    def smn_read32(self, smn_addr):
        """Read 32-bit value from SMN address.

        Uses PCI config indirect if available (safe, no physical memory writes).
        Falls back to MMIO BAR index/data pair otherwise.
        """
        if self._use_pcie_smn:
            return self._pcie_smn_read(smn_addr)
        # Fallback: MMIO BAR index/data
        self.write32(self._smn_idx, smn_addr)
        return self.read32(self._smn_data)

    def smn_write32(self, smn_addr, value):
        """Write 32-bit value to SMN address.

        Uses PCI config indirect if available (safe, no physical memory writes).
        Falls back to MMIO BAR index/data pair otherwise.
        """
        if self._use_pcie_smn:
            self._pcie_smn_write(smn_addr, value)
            return
        # Fallback: MMIO BAR index/data
        self.write32(self._smn_idx, smn_addr)
        self.write32(self._smn_data, value)

    # ---- PCI config indirect SMN access ----

    def _pcie_smn_read(self, smn_addr):
        """Read SMN register via PCI config space indirect."""
        backend = self._pci_backend
        pci = self._pci_addr
        backend.write_pci_dword_ex(pci, self._pcie_smn_idx, smn_addr)
        return backend.read_pci_dword_ex(pci, self._pcie_smn_data)

    def _pcie_smn_write(self, smn_addr, value):
        """Write SMN register via PCI config space indirect."""
        backend = self._pci_backend
        pci = self._pci_addr
        backend.write_pci_dword_ex(pci, self._pcie_smn_idx, smn_addr)
        backend.write_pci_dword_ex(pci, self._pcie_smn_data, value)

    # ---- Properties ----

    @property
    def bar_address(self):
        return self._bar

    @property
    def pci_address(self):
        return self._pci_addr

    @property
    def smn_offsets(self):
        """Return the active SMN index/data pair info."""
        if self._use_pcie_smn:
            return (self._pcie_smn_idx, self._pcie_smn_data)
        return (self._smn_idx, self._smn_data)

    @property
    def smn_method(self):
        """Return the SMN access method in use."""
        if self._use_pcie_smn:
            return "pcie_config_indirect"
        if self._use_phys:
            return "mmio_bar"
        return "none"

    @property
    def access_method(self):
        """Return the MMIO read access method."""
        if self._use_phys:
            return "physical_memory"
        return "none"

    @property
    def write_method(self):
        """Return the MMIO write access method."""
        if self._inpout is not None:
            return "inpout32_mmio"
        if self._io_space_enabled and self._io_port is not None:
            return "io_bar"
        return "none"

    # ---- I/O Space management ----

    @property
    def _pci_backend(self):
        """Return whichever backend provides PCI config access (WR0 or InpOut32)."""
        return self._wr0 or self._inpout

    def _enable_io_space(self):
        """Enable I/O Space in PCI Command register (bit 0).

        This allows I/O BAR port access to reach the GPU's MMIO registers.
        The original Command register value is saved for restoration.
        """
        pci = self._pci_backend
        if self._pci_addr is None or pci is None:
            return
        cmd = pci.read_pci_dword(self._pci_addr, 0x04) & 0xFFFF
        self._io_space_was_on = bool(cmd & 1)
        if self._io_space_was_on:
            self._io_space_enabled = True
            return
        # Enable I/O Space (set bit 0)
        pci.write_pci_dword_ex(self._pci_addr, 0x04, cmd | 1)
        time.sleep(0.05)
        # Verify
        cmd_after = pci.read_pci_dword(self._pci_addr, 0x04) & 0xFFFF
        self._io_space_enabled = bool(cmd_after & 1)

    def _restore_io_space(self):
        """Restore I/O Space to its original state in PCI Command register."""
        if (self._pci_addr is None or self._io_space_was_on is None
                or self._io_space_was_on):
            return  # Nothing to restore
        try:
            pci = self._pci_backend
            if pci is None:
                return
            cmd = pci.read_pci_dword(self._pci_addr, 0x04) & 0xFFFF
            if cmd & 1:  # I/O Space is still on, restore to off
                pci.write_pci_dword_ex(self._pci_addr, 0x04, cmd & ~1)
        except Exception:
            pass  # Best effort

    def close(self):
        """Restore PCI Command register and release resources."""
        self._restore_io_space()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- Probing ----

    def _probe_pcie_smn(self):
        """Probe PCI config space indirect SMN access.

        The AMD GPU exposes SMN bus access through PCI extended config
        registers.  This is the same mechanism the Linux amdgpu driver uses
        (RREG32_PCIE / WREG32_PCIE via amdgpu_device_pcie_rreg/wreg).

        Candidate pairs (PCI config register offsets):
          - 0xE0/0xE4: NBIO v7.4+ (RDNA2, RDNA3, RDNA4)
          - 0xC0/0xC4: Older GPUs
        """
        backend = self._pci_backend
        pci = self._pci_addr
        if pci is None or backend is None:
            return

        for name, (idx_reg, data_reg) in self.PCIE_SMN_PAIRS.items():
            try:
                # Write SMN address 0 to index, read data
                backend.write_pci_dword_ex(pci, idx_reg, 0x00000000)
                val0 = backend.read_pci_dword_ex(pci, data_reg)

                # Write SMN address 4 to index, read data
                backend.write_pci_dword_ex(pci, idx_reg, 0x00000004)
                val4 = backend.read_pci_dword_ex(pci, data_reg)

                # Valid if we get different non-trivial values
                if (val0 != 0xFFFFFFFF and val0 != 0x00000000
                        and val4 != 0xFFFFFFFF and val0 != val4):
                    print(f"[SMN] PCI config indirect OK: {name} "
                          f"(idx=0x{idx_reg:02X}, data=0x{data_reg:02X})")
                    print(f"[SMN]   SMN[0x0]=0x{val0:08X}, SMN[0x4]=0x{val4:08X}")
                    self._pcie_smn_idx = idx_reg
                    self._pcie_smn_data = data_reg
                    self._use_pcie_smn = True
                    return
            except IOError:
                continue

        print("[SMN] PCI config indirect not available, will use MMIO BAR")

    def _probe_mmio_smn_pair(self):
        """Probe MMIO BAR index/data pairs for SMN access (fallback)."""
        for name, (idx, data) in self.MMIO_SMN_PAIRS.items():
            try:
                self.write32(idx, 0x00000000)
                val0 = self.read32(data)
                self.write32(idx, 0x00000004)
                val4 = self.read32(data)
                if (val0 != 0xFFFFFFFF and val0 != 0x00000000 and
                        val4 != 0xFFFFFFFF and val0 != val4):
                    print(f"[SMN] Detected MMIO BAR {name.upper()} pair "
                          f"(idx=0x{idx:02X}, data=0x{data:02X})")
                    return idx, data
            except IOError:
                continue
        print("[SMN] MMIO BAR auto-detect inconclusive, defaulting to RDNA4 (0x38/0x3C)")
        return self.MMIO_SMN_PAIRS["rdna4"]

    # ---- GPU auto-detection ----

    @staticmethod
    def find_gpu_bar(winring0, vendor_id=0x1002, device_id=None):
        """
        Auto-detect the GPU's BARs from PCI config space.

        Returns: (pci_addr, mmio_bar_phys, io_bar_port_or_None, vram_bar_phys_or_None)
        """
        wr0 = winring0

        # --- Find GPU PCI device ---
        if device_id is not None:
            pci_addr = wr0.find_pci_device(vendor_id, device_id)
            did = device_id
        else:
            common = [
                0x7590,                     # Navi 44 / RX 9060 XT (RDNA4)
                0x15BF, 0x15C8,             # Navi 44/48 alt IDs (RDNA4)
                0x744C, 0x7480,             # Navi 31 (RDNA3)
                0x73DF, 0x73BF,             # Navi 22/21 (RDNA2)
                0x7340, 0x731F,             # Navi 14/10 (RDNA1)
            ]
            pci_addr = None
            for did in common:
                try:
                    pci_addr = wr0.find_pci_device(vendor_id, did)
                    break
                except RuntimeError:
                    continue

            if pci_addr is None:
                try:
                    pci_addr = wr0.find_pci_by_class(0x03, 0x00)
                    vid = wr0.read_pci_dword(pci_addr, 0x00) & 0xFFFF
                    did = (wr0.read_pci_dword(pci_addr, 0x00) >> 16) & 0xFFFF
                    if vid != vendor_id:
                        raise RuntimeError(f"VGA device vendor 0x{vid:04X} is not AMD")
                except RuntimeError:
                    raise RuntimeError(
                        "No AMD GPU found. Specify device_id= manually.\n"
                        "Check Device Manager -> Display adapters -> Hardware IDs"
                    )

        bus = (pci_addr >> 8) & 0xFF
        dev = (pci_addr >> 3) & 0x1F
        func = pci_addr & 7
        print(f"[PCI] GPU: vendor=0x{vendor_id:04X} device=0x{did:04X} "
              f"@ bus={bus} dev={dev} func={func}")

        # --- Parse BARs ---
        bars = []
        reg = 0x10
        idx = 0
        while reg <= 0x24 and idx < 6:
            lo = wr0.read_pci_dword(pci_addr, reg)
            is_io = bool(lo & 1)
            if is_io:
                bars.append({"idx": idx, "addr": lo & 0xFFFFFFFC,
                             "is_io": True, "is_64": False, "prefetch": False})
                reg += 4; idx += 1
            else:
                is_64 = ((lo >> 1) & 3) == 2
                prefetch = bool(lo & 8)
                addr = lo & 0xFFFFFFF0
                if is_64 and reg + 4 <= 0x24:
                    hi = wr0.read_pci_dword(pci_addr, reg + 4)
                    addr |= (hi << 32)
                    bars.append({"idx": idx, "addr": addr,
                                 "is_io": False, "is_64": True, "prefetch": prefetch})
                    reg += 8; idx += 2
                else:
                    bars.append({"idx": idx, "addr": addr,
                                 "is_io": False, "is_64": False, "prefetch": prefetch})
                    reg += 4; idx += 1

        for b in bars:
            if b["addr"]:
                kind = "I/O" if b["is_io"] else ("Mem64" if b["is_64"] else "Mem32")
                pf = ", prefetch" if b["prefetch"] else ""
                print(f"[PCI] BAR{b['idx']}: 0x{b['addr']:012X} ({kind}{pf})")

        # --- Identify MMIO register BAR (non-prefetchable memory) ---
        mmio_candidates = [b for b in bars
                           if not b["is_io"] and b["addr"] and not b["prefetch"]]
        if not mmio_candidates:
            mmio_candidates = [b for b in bars if not b["is_io"] and b["addr"]]
        if not mmio_candidates:
            raise RuntimeError("No memory BAR found for GPU")
        mmio_bar = max(mmio_candidates, key=lambda b: b["idx"])
        print(f"[PCI] Selected BAR{mmio_bar['idx']} as MMIO register BAR: "
              f"0x{mmio_bar['addr']:012X}")

        # --- Identify VRAM BAR (prefetchable memory, typically BAR0) ---
        vram_candidates = [b for b in bars
                           if not b["is_io"] and b["addr"] and b["prefetch"]]
        vram_bar = None
        if vram_candidates:
            # Prefer the lowest-indexed prefetchable BAR (usually BAR0)
            vram_bar = min(vram_candidates, key=lambda b: b["idx"])
            print(f"[PCI] VRAM BAR{vram_bar['idx']}: 0x{vram_bar['addr']:012X} "
                  f"(prefetchable)")

        # --- Find I/O BAR (used for MMIO writes via direct port offset mapping) ---
        # The I/O BAR maps MMIO registers at direct port offsets:
        #   port IO_BAR + offset => writes to MMIO BAR + offset
        # I/O Space must be enabled in PCI Command register (bit 0).
        io_bars = [b for b in bars if b["is_io"] and b["addr"]]
        io_port = io_bars[0]["addr"] if io_bars else None
        if io_port:
            print(f"[PCI] I/O BAR: 0x{io_port:04X} (for MMIO writes)")

        vram_bar_addr = vram_bar["addr"] if vram_bar else None
        return pci_addr, mmio_bar["addr"], io_port, vram_bar_addr


# ---------------------------------------------------------------------------
# InpOut32 driver wrapper (for physical memory writes)
# ---------------------------------------------------------------------------

class InpOut32:
    """
    Thin wrapper around inpoutx64.dll for physical memory read/write.

    This provides the critical memory-mapped write capability that WinRing0's
    patched driver lacks (its WRITE_MEMORY IOCTL causes BSODs).

    InpOut32 uses its own kernel driver (inpoutx64.sys) which is auto-installed
    from resources embedded in the DLL when first loaded.

    Key functions used:
      - SetPhysLong(addr, val) -- write DWORD to physical address
      - GetPhysLong(addr, &val) -- read DWORD from physical address

    PCI config space access:
      - Bus 0: CF8/CFC I/O ports (legacy, always works)
      - Bus > 0: ECAM (memory-mapped config via ACPI MCFG table)
        CF8/CFC often can't reach non-root buses on AMD Zen platforms.

    Download: https://www.highrez.co.uk/downloads/inpout32/
    """

    def __init__(self, dll_path=None):
        """
        Load inpoutx64.dll and verify the kernel driver is running.

        Args:
            dll_path: Explicit path to inpoutx64.dll.  If None, searches
                      common locations (script dir, InpOutBinaries/x64/, CWD).

        Raises:
            FileNotFoundError: If the DLL cannot be found.
            RuntimeError: If the kernel driver fails to load.
        """
        self._dll = None
        self._ecam_base = None      # Cached ECAM base address
        self._ecam_searched = False  # Whether we've searched for ECAM

        if dll_path is None:
            dll_path = self._find_dll()
        if dll_path is None:
            raise FileNotFoundError(
                "Cannot find inpoutx64.dll.\n"
                "Place it in the script directory or InpOutBinaries/x64/.\n"
                "Download from: https://www.highrez.co.uk/downloads/inpout32/"
            )

        self._dll = ctypes.WinDLL(dll_path)
        self._setup_functions()

        if not self._dll.IsInpOutDriverOpen():
            raise RuntimeError(
                "InpOut32 kernel driver failed to load.\n"
                "The DLL loaded but the driver did not start.\n"
                "Try running as Administrator, or check Event Viewer."
            )
        print(f"[InpOut32] Driver loaded OK ({dll_path})")

    @staticmethod
    def _find_dll():
        """Search common locations for inpoutx64.dll."""
        script_dir = os.path.dirname(os.path.abspath(
            sys.modules[__name__].__file__
            if hasattr(sys.modules[__name__], '__file__')
            else __file__
        ))
        drivers_dir = os.path.join(script_dir, "drivers")
        search_paths = [
            os.path.join(drivers_dir, "inpoutx64.dll"),
            os.path.join(script_dir, "inpoutx64.dll"),
            os.path.join(script_dir, "InpOutBinaries", "x64", "inpoutx64.dll"),
            os.path.join(os.getcwd(), "inpoutx64.dll"),
            os.path.join(os.getcwd(), "InpOutBinaries", "x64", "inpoutx64.dll"),
        ]
        for p in search_paths:
            if os.path.isfile(p):
                return p
        return None

    def _setup_functions(self):
        d = self._dll
        # Driver status
        d.IsInpOutDriverOpen.restype = wt.BOOL
        d.IsInpOutDriverOpen.argtypes = []
        # Physical memory DWORD read
        d.GetPhysLong.restype = wt.BOOL
        d.GetPhysLong.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
        # Physical memory DWORD write
        d.SetPhysLong.restype = wt.BOOL
        d.SetPhysLong.argtypes = [ctypes.c_void_p, wt.DWORD]
        # Physical memory mapping (persistent)
        d.MapPhysToLin.restype = ctypes.c_void_p  # returns PBYTE (virtual addr)
        d.MapPhysToLin.argtypes = [
            ctypes.c_void_p,                  # PBYTE pbPhysAddr
            wt.DWORD,                         # DWORD dwPhysSize
            ctypes.POINTER(wt.HANDLE),        # HANDLE *pPhysicalMemoryHandle
        ]
        d.UnmapPhysicalMemory.restype = wt.BOOL
        d.UnmapPhysicalMemory.argtypes = [
            wt.HANDLE,          # HANDLE PhysicalMemoryHandle
            ctypes.c_void_p,    # PBYTE pbLinAddr
        ]
        # I/O port access (the primary purpose of InpOut32)
        d.DlPortWritePortUlong.restype = None
        d.DlPortWritePortUlong.argtypes = [wt.ULONG, wt.ULONG]
        d.DlPortReadPortUlong.restype = wt.ULONG
        d.DlPortReadPortUlong.argtypes = [wt.ULONG]

    # ---- I/O port access ----

    def read_io32(self, port):
        """Read a 32-bit DWORD from an I/O port."""
        return self._dll.DlPortReadPortUlong(port)

    def write_io32(self, port, value):
        """Write a 32-bit DWORD to an I/O port."""
        self._dll.DlPortWritePortUlong(port, value & 0xFFFFFFFF)

    # ---- PCI config space via I/O ports 0xCF8/0xCFC ----

    def _pci_addr_encode(self, bus, dev, func, reg):
        """Encode PCI Type 1 config address for CF8/CFC access."""
        return ((1 << 31) | (bus << 16) | (dev << 11) |
                (func << 8) | (reg & 0xFC))

    def _pci_addr_to_bdf(self, pci_addr):
        """Convert WinRing0-style PCI address to (bus, dev, func)."""
        bus = (pci_addr >> 8) & 0xFF
        dev = (pci_addr >> 3) & 0x1F
        func = pci_addr & 7
        return bus, dev, func

    def read_pci_dword(self, pci_addr, reg):
        """Read DWORD from PCI config (reg 0-255).

        pci_addr uses WinRing0 encoding: (bus << 8) | (dev << 3) | func

        Uses ECAM (memory-mapped config) for buses > 0, since CF8/CFC
        I/O port access often can't reach non-root buses on AMD Zen.
        """
        bus, dev, func = self._pci_addr_to_bdf(pci_addr)

        # ECAM for non-root buses (CF8/CFC can't reach them on AMD Zen)
        if bus > 0:
            phys = self._ecam_phys_addr(bus, dev, func, reg)
            if phys is not None:
                return self.read_phys32(phys)

        # CF8/CFC fallback (works for bus 0)
        self.write_io32(0xCF8, self._pci_addr_encode(bus, dev, func, reg))
        return self.read_io32(0xCFC)

    def read_pci_byte(self, pci_addr, reg):
        """Read byte from PCI config."""
        dword = self.read_pci_dword(pci_addr, reg & ~3)
        return (dword >> (8 * (reg & 3))) & 0xFF

    def read_pci_dword_ex(self, pci_addr, reg):
        """Read DWORD from extended PCI config (reg 0-4095).

        Uses ECAM (memory-mapped config) when available.  Falls back to
        CF8/CFC for reg < 256 on bus 0.
        """
        bus, dev, func = self._pci_addr_to_bdf(pci_addr)

        # Try ECAM first (required for extended regs and non-root buses)
        phys = self._ecam_phys_addr(bus, dev, func, reg)
        if phys is not None:
            return self.read_phys32(phys)

        # CF8/CFC fallback (bus 0, reg < 256 only)
        if reg < 256:
            self.write_io32(0xCF8, self._pci_addr_encode(bus, dev, func, reg))
            return self.read_io32(0xCFC)

        raise IOError(
            f"Extended PCI config read requires ECAM (MCFG not found). "
            f"bus={bus}, reg=0x{reg:X}"
        )

    def write_pci_dword_ex(self, pci_addr, reg, value):
        """Write DWORD to extended PCI config (reg 0-4095).

        Uses ECAM (memory-mapped config) when available.  Falls back to
        CF8/CFC for reg < 256 on bus 0.
        """
        bus, dev, func = self._pci_addr_to_bdf(pci_addr)

        # Try ECAM first (required for extended regs and non-root buses)
        phys = self._ecam_phys_addr(bus, dev, func, reg)
        if phys is not None:
            self.write_phys32(phys, value)
            return

        # CF8/CFC fallback (bus 0, reg < 256 only)
        if reg < 256:
            self.write_io32(0xCF8, self._pci_addr_encode(bus, dev, func, reg))
            self.write_io32(0xCFC, value & 0xFFFFFFFF)
            return

        raise IOError(
            f"Extended PCI config write requires ECAM (MCFG not found). "
            f"bus={bus}, reg=0x{reg:X}"
        )

    def _get_ecam_base(self):
        """Get ECAM base address with lazy initialization and caching."""
        if not self._ecam_searched:
            self._ecam_searched = True
            self._ecam_base = self._discover_ecam_base()
            if self._ecam_base is not None:
                print(f"[PCI] ECAM/MMCFG base: 0x{self._ecam_base:X}")
            else:
                print(f"[PCI] ECAM/MMCFG not found (PCI config limited to bus 0 via CF8/CFC)")
        return self._ecam_base

    @staticmethod
    def _discover_ecam_base():
        """Find ECAM/MMCFG base from ACPI MCFG firmware table.

        The MCFG (Memory-mapped Configuration space) ACPI table contains
        the base physical address for PCI Express Enhanced Configuration
        Space.  This is required for PCI config access to devices on
        buses > 0 on modern AMD Zen platforms, where CF8/CFC I/O port
        access typically only reaches bus 0.

        Uses Win32 GetSystemFirmwareTable() API to read the raw ACPI table.

        Returns:
            ECAM base physical address, or None if not found.
        """
        try:
            kernel32 = ctypes.windll.kernel32

            # GetSystemFirmwareTable signatures
            kernel32.GetSystemFirmwareTable.restype = wt.UINT
            kernel32.GetSystemFirmwareTable.argtypes = [
                wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD
            ]

            # Provider = 'ACPI' = 0x41435049 (little-endian)
            # Table ID = 'MCFG' = 0x4746434D (little-endian)
            ACPI_SIG = 0x41435049
            MCFG_SIG = 0x4746434D

            # Query required buffer size
            buf_size = kernel32.GetSystemFirmwareTable(
                ACPI_SIG, MCFG_SIG, None, 0
            )
            if buf_size == 0:
                return None

            buf = (ctypes.c_char * buf_size)()
            ret = kernel32.GetSystemFirmwareTable(
                ACPI_SIG, MCFG_SIG, buf, buf_size
            )
            if ret == 0:
                return None

            data = bytes(buf)

            # MCFG table layout:
            #   [0:4]    Signature ('MCFG')
            #   [4:8]    Length (uint32)
            #   [8]      Revision
            #   [9]      Checksum
            #   [10:16]  OEM ID
            #   [16:24]  OEM Table ID
            #   [24:28]  OEM Revision
            #   [28:32]  Creator ID
            #   [32:36]  Creator Revision
            #   [36:44]  Reserved (8 bytes)
            #   [44:]    Config Space Base Address Allocation entries:
            #            [0:8]   Base Address (uint64)
            #            [8:10]  PCI Segment Group (uint16)
            #            [10]    Start Bus Number
            #            [11]    End Bus Number
            #            [12:16] Reserved (4 bytes)

            if len(data) < 44 + 16:
                return None

            base_addr = struct.unpack_from('<Q', data, 44)[0]
            seg_group = struct.unpack_from('<H', data, 52)[0]
            start_bus = data[54]
            end_bus = data[55]

            if base_addr > 0 and seg_group == 0:
                return base_addr

            return None
        except Exception:
            return None

    def _ecam_phys_addr(self, bus, dev, func, reg):
        """Calculate ECAM physical address for a PCI config register.

        ECAM layout: base + (bus << 20) + (dev << 15) + (func << 12) + reg
        Each function gets 4096 bytes of config space (standard 256 + extended).

        Returns physical address, or None if ECAM is not available.
        """
        base = self._get_ecam_base()
        if base is None:
            return None
        return base + (bus << 20) + (dev << 15) + (func << 12) + (reg & 0xFFC)

    # ---- Safe PCI device discovery via Windows APIs ----
    # NEVER scan PCI buses via CF8/CFC -- some bus numbers host management
    # controllers that crash the system (MCE / hard power-off) when probed.

    @staticmethod
    def _wmi_find_gpu(vendor_id=0x1002):
        """Find AMD GPU bus/dev/func using Windows PnP APIs.

        Uses PowerShell Get-PnpDevice + DEVPKEY_Device_LocationInfo to get
        the PCI location safely, without any raw hardware bus scanning.

        Returns list of (bus, dev, func, device_id) tuples.
        """
        import subprocess
        import re
        results = []

        # PowerShell: get InstanceId + LocationInfo for AMD display devices
        try:
            ps_cmd = (
                'Get-PnpDevice -Class Display -Status OK | '
                'Get-PnpDeviceProperty -KeyName DEVPKEY_Device_LocationInfo | '
                'Select-Object InstanceId, Data | Format-List'
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True, timeout=15, creationflags=0x08000000  # CREATE_NO_WINDOW
            ).strip()

            # Parse output blocks like:
            #   InstanceId : PCI\VEN_1002&DEV_7590&SUBSYS_...
            #   Data       : PCI bus 12, device 0, function 0
            current_id = None
            current_loc = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("InstanceId"):
                    current_id = line.split(":", 1)[1].strip().upper()
                elif line.startswith("Data"):
                    current_loc = line.split(":", 1)[1].strip()

                    # Process the pair
                    if current_id and current_loc:
                        ven_hex = f"VEN_{vendor_id:04X}"
                        if ven_hex in current_id:
                            # Extract device_id from InstanceId
                            dev_match = re.search(r'DEV_([0-9A-F]{4})', current_id)
                            did = int(dev_match.group(1), 16) if dev_match else 0

                            # Parse "PCI bus 12, device 0, function 0"
                            loc_match = re.search(
                                r'bus\s+(\d+).*device\s+(\d+).*function\s+(\d+)',
                                current_loc, re.IGNORECASE
                            )
                            if loc_match:
                                bus = int(loc_match.group(1))
                                dev = int(loc_match.group(2))
                                func = int(loc_match.group(3))
                                results.append((bus, dev, func, did))

                    current_id = None
                    current_loc = None
        except Exception as e:
            print(f"[PCI] PowerShell GPU discovery failed: {e}")

        return results

    def find_pci_device(self, vendor_id, device_id, index=0):
        """Find PCI device by vendor/device ID using safe Windows APIs.

        Returns WinRing0-compatible address: (bus << 8) | (dev << 3) | func.

        Uses PowerShell/WMI to discover bus/dev/func -- NEVER scans PCI buses
        directly via CF8/CFC (that can crash the system).
        """
        gpus = self._wmi_find_gpu(vendor_id)
        matches = [(b, d, f, did) for b, d, f, did in gpus if did == device_id]
        if index < len(matches):
            bus, dev, func, _ = matches[index]
            return (bus << 8) | (dev << 3) | func

        # If specific device_id not found, check if any AMD GPU exists
        if gpus:
            # Return first AMD GPU found (caller may accept any)
            raise RuntimeError(
                f"PCI device {vendor_id:04X}:{device_id:04X} not found.\n"
                f"Found AMD GPUs: {['%04X @ bus %d' % (did, b) for b,d,f,did in gpus]}"
            )
        raise RuntimeError(f"PCI device {vendor_id:04X}:{device_id:04X} not found")

    def find_pci_by_class(self, base_class, sub_class, prog_if=0, index=0):
        """Find PCI device by class code using safe Windows APIs.

        For display class (0x03, 0x00), uses WMI to find GPU.
        """
        if base_class == 0x03 and sub_class == 0x00:
            gpus = self._wmi_find_gpu(0x1002)  # AMD vendor
            if index < len(gpus):
                bus, dev, func, _ = gpus[index]
                return (bus << 8) | (dev << 3) | func
        raise RuntimeError(
            f"PCI class {base_class:02X}:{sub_class:02X}:{prog_if:02X} not found"
        )

    def can_read_phys(self, addr):
        """Test if physical memory read works at the given address."""
        try:
            self.read_phys32(addr)
            return True
        except Exception:
            return False

    def read_phys32(self, phys_addr):
        """Read a 32-bit DWORD from a physical address."""
        val = wt.DWORD(0)
        ok = self._dll.GetPhysLong(phys_addr, ctypes.byref(val))
        if not ok:
            raise IOError(f"InpOut32 GetPhysLong failed at 0x{phys_addr:X}")
        return val.value

    def write_phys32(self, phys_addr, value):
        """Write a 32-bit DWORD to a physical address."""
        ok = self._dll.SetPhysLong(phys_addr, value & 0xFFFFFFFF)
        if not ok:
            raise IOError(f"InpOut32 SetPhysLong failed at 0x{phys_addr:X}")

    def map_phys(self, phys_addr, size):
        """Map a physical address range to user-space virtual memory.

        This creates a PERSISTENT mapping -- the equivalent of Linux's
        ioremap().  The mapping stays valid until unmap_phys() is called.

        For GPU MMIO registers, this is crucial: writes to the mapped
        virtual address generate proper PCIe Memory Write TLPs, just
        like Linux's writel() through an ioremap'd address.

        Args:
            phys_addr: Physical address to map (should be page-aligned
                       for best results, but the driver handles alignment).
            size:      Number of bytes to map.

        Returns:
            (virt_addr, handle) tuple.  virt_addr is a user-space pointer
            (integer) for ctypes access.  handle must be passed to
            unmap_phys() when done.

        Raises:
            IOError: If the mapping fails.
        """
        handle = wt.HANDLE()
        virt = self._dll.MapPhysToLin(phys_addr, size, ctypes.byref(handle))
        if virt is None or virt == 0:
            raise IOError(
                f"InpOut32 MapPhysToLin failed for phys=0x{phys_addr:X}, "
                f"size={size}"
            )
        return int(virt), handle

    def unmap_phys(self, virt_addr, handle):
        """Unmap a previously mapped physical memory region."""
        self._dll.UnmapPhysicalMemory(handle, virt_addr)

    def close(self):
        """Release the DLL (driver stays loaded until reboot)."""
        self._dll = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------------------

def main():
    """Full connectivity test."""
    print("=" * 60)
    print("SMU MMIO Access Layer -- Connectivity Test")
    print("=" * 60)

    with WinRing0() as wr0:
        print("\n[OK] WinRing0 initialized.\n")

        # Verify driver IOCTL works
        ver = wt.DWORD(0)
        wr0._ioctl(IOCTL_OLS_GET_DRIVER_VERSION, None, 0, ctypes.byref(ver), 4)
        v = ver.value
        print(f"[OK] Driver version: {(v>>24)&0xFF}.{(v>>16)&0xFF}.{(v>>8)&0xFF}.{v&0xFF}")

        # Check physical memory access capability
        phys_bios = wr0.can_read_phys(0x000F0000)
        print(f"\n[SYS] Physical memory read (BIOS area): {'OK' if phys_bios else 'BLOCKED'}")

        # Find GPU (read-only PCI config scan, no writes)
        pci_addr, bar_addr, io_port, vram_bar = GpuMMIO.find_gpu_bar(wr0)

        # Test if we can read the actual MMIO BAR
        phys_mmio = wr0.can_read_phys(bar_addr)
        print(f"[SYS] Physical memory read (GPU MMIO BAR): {'OK' if phys_mmio else 'BLOCKED'}")

        # Create MMIO accessor (pass io_bar_port for I/O BAR writes)
        mmio = GpuMMIO(wr0, bar_addr, pci_addr=pci_addr, io_bar_port=io_port)

        print(f"\n--- MMIO Read:  {mmio.access_method} ---")
        print(f"--- MMIO Write: {mmio.write_method} ---")
        print(f"--- SMN Access: {mmio.smn_method} ---")

        if mmio.access_method != "none":
            print(f"\n--- MMIO Register Reads (read-only, safe) ---")
            for off in [0x00, 0x04, 0x08, 0x0C]:
                try:
                    val = mmio.read32(off)
                    print(f"  BAR+0x{off:04X} = 0x{val:08X}")
                except IOError as e:
                    print(f"  BAR+0x{off:04X} = ERROR: {e}")

        if mmio.smn_method != "none":
            print(f"\n--- SMN Bus Test (via {mmio.smn_method}) ---")
            idx, data = mmio.smn_offsets
            print(f"  SMN pair: idx=0x{idx:02X}, data=0x{data:02X}")
            for smn in [0x00000000, 0x00000004]:
                try:
                    val = mmio.smn_read32(smn)
                    print(f"  SMN[0x{smn:08X}] = 0x{val:08X}")
                except IOError as e:
                    print(f"  SMN[0x{smn:08X}] = ERROR: {e}")

            # Test SMN write + readback (write SMN addr to index, read back)
            print(f"\n--- SMN Write Test ---")
            try:
                # Read current value at a safe SMN address
                test_smn = 0x00000000
                orig = mmio.smn_read32(test_smn)
                print(f"  SMN[0x{test_smn:08X}] = 0x{orig:08X} (original)")
                # Write same value back (safe no-op)
                mmio.smn_write32(test_smn, orig)
                readback = mmio.smn_read32(test_smn)
                print(f"  SMN[0x{test_smn:08X}] = 0x{readback:08X} (after write-back)")
                if readback == orig:
                    print(f"  *** SMN write+readback OK! ***")
                else:
                    print(f"  [WARN] Value changed: 0x{orig:08X} -> 0x{readback:08X}")
            except IOError as e:
                print(f"  SMN write test FAILED: {e}")
        else:
            print("\n[!!] No SMN access available.")
            print("[!!] Need either PCI config indirect or MMIO BAR + write support.")

        # Clean up I/O Space
        mmio.close()

        print(f"\n--- Summary ---")
        print(f"GPU PCI address:  0x{pci_addr:04X}")
        print(f"MMIO BAR:         0x{bar_addr:012X}")
        print(f"MMIO read:        {mmio.access_method}")
        print(f"MMIO write:       {mmio.write_method}")
        print(f"SMN access:       {mmio.smn_method}")
        smn_idx, smn_data = mmio.smn_offsets
        print(f"SMN pair:         idx=0x{smn_idx:02X}, data=0x{smn_data:02X}")


if __name__ == "__main__":
    main()
