"""
PPTable blob sources (ROM / registry / SMU).

This module provides best-effort helpers for getting raw PPTable bytes from:
  - VBIOS ROM dump (e.g. bios/vbios.rom)
  - Windows registry (SPPT / PPTable override blobs)
  - SMU table transfer (TABLE_PPTABLE -> DMA buffer)

The callers are responsible for validating/parsing the blob contents.
"""

from __future__ import annotations

import os
import ctypes
from typing import Dict, Optional, Iterable, Tuple, Union

try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - non-Windows environments
    winreg = None  # type: ignore


_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID


def _norm_pci_id(v: Union[int, str]) -> int:
    if isinstance(v, int):
        return int(v) & 0xFFFF
    s = str(v).strip()
    if s.lower().startswith("0x"):
        return int(s, 16) & 0xFFFF
    # If it's hex-looking (common for PCI ids), interpret as hex.
    try:
        if s and all(c in "0123456789abcdefABCDEF" for c in s):
            return int(s, 16) & 0xFFFF
        return int(s) & 0xFFFF
    except Exception:
        # Last resort: let int() raise a useful error in caller contexts.
        return int(s, 16) & 0xFFFF


def _read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception:
        # Keep this helper "best effort"; callers can decide what to do.
        return None


def read_rom_blob(path: str) -> Optional[bytes]:
    """
    Read a VBIOS ROM blob from disk. Decodes if stored in encoded format (VBEN magic).
    """
    if not path:
        return None
    p = path
    if not os.path.isabs(p):
        _proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(_proj, p)
    from src.io.vbios_storage import read_vbios_decoded
    decoded, _ = read_vbios_decoded(p)
    return decoded


def _enum_subkeys(root, path: str) -> Iterable[str]:
    """Yield all direct subkey names under root\\path."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    yield winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
    except OSError:
        return


def _query_value_str(key, name: str) -> Optional[str]:
    try:
        v, t = winreg.QueryValueEx(key, name)
        if t == winreg.REG_SZ and isinstance(v, str):
            return v
        if isinstance(v, str):
            return v
        return None
    except OSError:
        return None


def _query_value_bytes(key, name: str) -> Optional[bytes]:
    try:
        v, t = winreg.QueryValueEx(key, name)
        if t == winreg.REG_BINARY and isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        return None
    except OSError:
        return None


def enumerate_display_adapters(
    *,
    class_path: str = _DISPLAY_CLASS_PATH,
) -> Tuple[Dict[str, str], ...]:
    """
    Enumerate display adapters from the Windows Display class key.

    Returns:
        Tuple of dicts with basic info. Each dict contains:
          - key_path (relative to HKLM)
          - MatchingDeviceId (if present)
          - DriverDesc / ProviderName / DriverVersion (if present)
    """
    if winreg is None:
        return ()

    root = winreg.HKEY_LOCAL_MACHINE
    out = []
    for sub in _enum_subkeys(root, class_path):
        sub_path = class_path + "\\" + sub
        try:
            with winreg.OpenKey(root, sub_path, 0, winreg.KEY_READ) as k:
                info: Dict[str, str] = {"key_path": sub_path}
                for name in ("MatchingDeviceId", "DriverDesc", "ProviderName", "DriverVersion"):
                    v = _query_value_str(k, name)
                    if v:
                        info[name] = v
                out.append(info)
        except OSError:
            continue
    return tuple(out)


def read_registry_values(
    key_path: str,
    *,
    value_names: Iterable[str],
) -> Dict[str, Union[str, bytes]]:
    """
    Read named values from a registry key under HKLM.

    Returns:
        Dict of name -> value (bytes for REG_BINARY-like, str for REG_SZ-like).
        Missing/unreadable values are skipped.
    """
    if winreg is None:
        return {}

    root = winreg.HKEY_LOCAL_MACHINE
    out: Dict[str, Union[str, bytes]] = {}
    try:
        with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ) as k:
            for vn in value_names:
                b = _query_value_bytes(k, vn)
                if b is not None:
                    out[vn] = b
                    continue
                s = _query_value_str(k, vn)
                if s is not None:
                    out[vn] = s
    except OSError:
        return {}
    return out


def find_display_adapter_class_keys(
    vendor_id: Union[int, str],
    device_id: Union[int, str],
    *,
    class_path: str = _DISPLAY_CLASS_PATH,
) -> Tuple[str, ...]:
    """
    Find candidate display adapter class subkeys for a PCI VEN/DEV.

    Returns:
        Tuple of registry paths relative to HKLM for matching adapters.
        Typically looks like:
          SYSTEM\\...\\Class\\{guid}\\0000
    """
    if winreg is None:
        return ()

    ven = _norm_pci_id(vendor_id)
    dev = _norm_pci_id(device_id)
    needle = f"PCI\\VEN_{ven:04X}&DEV_{dev:04X}".upper()

    matches = []
    try:
        root = winreg.HKEY_LOCAL_MACHINE
        for sub in _enum_subkeys(root, class_path):
            sub_path = class_path + "\\" + sub
            try:
                with winreg.OpenKey(root, sub_path, 0, winreg.KEY_READ) as k:
                    mdid = _query_value_str(k, "MatchingDeviceId") or ""
                    if needle in mdid.upper():
                        matches.append(sub_path)
            except OSError:
                continue
    except Exception:
        return ()

    return tuple(matches)


def read_registry_pptable_blob(
    vendor_id: Union[int, str],
    device_id: Union[int, str],
) -> Dict[str, bytes]:
    """
    Read PPTable-related blobs from the Windows display adapter registry key.

    It tries the common AMD values:
      - PP_PhmSoftPowerPlayTable (SPPT override)
      - PP_PhmPowerPlayTable     (full PPTable override / cache)

    Returns:
        Dict mapping value name -> raw bytes. Empty dict if not found or not
        readable.
    """
    if winreg is None:
        return {}

    value_names = ("PP_PhmSoftPowerPlayTable", "PP_PhmPowerPlayTable")
    out: Dict[str, bytes] = {}

    # Prefer the first adapter key that actually contains any of the values.
    for key_path in find_display_adapter_class_keys(vendor_id, device_id):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ
            ) as k:
                for vn in value_names:
                    b = _query_value_bytes(k, vn)
                    if b:
                        out[vn] = b
        except OSError:
            continue

        if out:
            return out

    return out


def _read_buf_from_virt(virt: int, n: int) -> bytes:
    buf = (ctypes.c_ubyte * n)()
    ctypes.memmove(buf, virt, n)
    return bytes(buf)


def read_smu_pptable_blob(
    smu,
    virt: int,
    *,
    table_id: Optional[int] = None,
    read_size: int = 256 * 1024,
    use_tools: bool = True,
) -> Optional[bytes]:
    """
    Read the SMU PPTABLE into the existing DMA buffer and return the bytes.

    Notes:
      - The DMA buffer address must already be configured on the SMU side.
      - The virtual mapping at `virt` must cover at least `read_size` bytes.
      - This is best-effort; it returns None if the SMU transfer fails.

    Args:
        smu: SMU object (see smu.py) with either transfer_table_from_smu()
             or send_msg().
        virt: Virtual address (int) of the mapped DMA buffer.
        table_id: Table ID to read. If None, tries to import TABLE_PPTABLE
                  from od_table, falling back to 0.
        read_size: How many bytes to read back from the DMA buffer.
        use_tools: If True and smu.transfer_table_from_smu exists, uses the
                   "WithAddr" path (tools DRAM addr). If False, uses driver
                   DRAM addr path.

    Returns:
        Raw bytes of length read_size, or None on failure.
    """
    if table_id is None:
        try:
            from src.engine.od_table import TABLE_PPTABLE as _TBL
        except Exception:
            _TBL = 0
        table_id = int(_TBL)

    try:
        if hasattr(smu, "transfer_table_from_smu"):
            smu.transfer_table_from_smu(int(table_id), use_tools=bool(use_tools))
        elif hasattr(smu, "send_msg"):
            # Match overclock.py's convention: 0x12 == TransferTableSmu2Dram
            smu.send_msg(0x12, int(table_id) & 0xFFFF)
        else:
            return None
    except Exception:
        return None

    try:
        return _read_buf_from_virt(int(virt), int(read_size))
    except Exception:
        return None

