"""
CN Escape cache: read, parse, modify, and write PP_CNEscapeInput blobs.

PP_CNEscapeInput is a Windows-only Adrenalin structure (~1556 bytes) that
caches OverDrive escape-interface settings.  The driver / Adrenalin
re-applies these values on login.

Binary layout (RDNA4 / Navi44, 1556 bytes):

  Header: 28 bytes (7 dwords)
    0x00  u32  Size             total blob length
    0x04  u32  Version          structure version (1)
    0x08  u32  SubVersion       sub-version (2)
    0x0C  u16  CapLo            basic OD capability bitmask
    0x0E  u16  CapHi            advanced OD capability bitmask
    0x10  u32  NumSettingTypes  number of OD setting types (24)
    0x14  u32  Reserved         always 0
    0x18  u32  NumRecords       number of 20-byte records (76)

  Records: NumRecords x 20 bytes starting at 0x1C
    +0   i32  value    (setting value, signed)
    +4   u32  enabled  (0 or 1)
    +8   u8[12] pad    (zeros)

  Trailing: 8 bytes padding

Typical flow::

    cache = CnEscapeCache.from_registry(adapter_key_path)
    cache.set_field("GfxclkFoffset", 200)
    cache.set_field("VoltageOffset", -80)
    ok = cache.write_to_registry(adapter_key_path)
"""

from __future__ import annotations

import logging
import struct
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger("overclock.cn_escape")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CN_REG_VALUE = "PP_CNEscapeInput"

# Known field map.
# The blob is a 28-byte header followed by 76 x 20-byte records.
# Each record: [i32 value][u32 enabled][12 bytes padding].
# Record index N starts at offset 0x1C + N * 20.
# Format: (name, offset, struct_fmt, group, unit, description)
_KNOWN_FIELDS: List[Tuple[str, int, str, str, str, str]] = [
    # ---- Header (28 bytes) ----
    ("Size",              0x0000, "I", "header", "",    "Blob size (== len)"),
    ("Version",           0x0004, "I", "header", "",    "Structure version"),
    ("SubVersion",        0x0008, "I", "header", "",    "Sub-version"),
    ("CapLo",             0x000C, "H", "header", "",    "Basic OD capability bits"),
    ("CapHi",             0x000E, "H", "header", "",    "Advanced OD capability bits"),
    ("NumSettingTypes",   0x0010, "I", "header", "",    "Number of OD setting types"),
    ("NumRecords",        0x0018, "I", "header", "",    "Number of 20-byte records"),

    # ---- Primary OD values ----
    # Each record: [value(4)] [enabled_flag(4)] [padding(12)] = 20-byte stride.
    # Record N starts at 0x1C + N * 20.
    ("GfxclkFoffset",     0x001C, "i", "clock",   "MHz", "GFX clock offset"),            # rec 0
    ("GfxclkFoffset_en",  0x0020, "I", "flag",    "",    "GfxclkFoffset enabled"),
    ("AutoUvEngine",      0x0030, "i", "clock",   "",    "Auto undervolt toggle"),        # rec 1
    ("AutoUvEngine_en",   0x0034, "I", "flag",    "",    "AutoUvEngine enabled"),
    ("AutoOcEngine",      0x0044, "i", "clock",   "",    "Auto overclock toggle"),        # rec 2
    ("AutoOcEngine_en",   0x0048, "I", "flag",    "",    "AutoOcEngine enabled"),
    ("UclkFmax",          0x00BC, "I", "memory",  "MHz", "Memory clock max"),             # rec 8
    ("UclkFmax_en",       0x00C0, "I", "flag",    "",    "UclkFmax enabled"),
    ("Ppt",               0x00D0, "i", "power",   "%",   "Power limit percent"),          # rec 9
    ("Ppt_en",            0x00D4, "I", "flag",    "",    "Ppt enabled"),

    # ---- Fan curve (interleaved temp/PWM pairs) ----
    # 6 points x 2 records each = 12 records (rec 19..30), 20-byte stride.
    ("FanTempPoint0",     0x0198, "i", "fan",     "C",   "Fan curve temp 0"),             # rec 19
    ("FanTempPoint0_en",  0x019C, "I", "flag",    "",    "FanTempPoint0 enabled"),
    ("FanPwmPoint0",      0x01AC, "i", "fan",     "%",   "Fan curve PWM 0"),              # rec 20
    ("FanPwmPoint0_en",   0x01B0, "I", "flag",    "",    "FanPwmPoint0 enabled"),
    ("FanTempPoint1",     0x01C0, "i", "fan",     "C",   "Fan curve temp 1"),             # rec 21
    ("FanTempPoint1_en",  0x01C4, "I", "flag",    "",    "FanTempPoint1 enabled"),
    ("FanPwmPoint1",      0x01D4, "i", "fan",     "%",   "Fan curve PWM 1"),              # rec 22
    ("FanPwmPoint1_en",   0x01D8, "I", "flag",    "",    "FanPwmPoint1 enabled"),
    ("FanTempPoint2",     0x01E8, "i", "fan",     "C",   "Fan curve temp 2"),             # rec 23
    ("FanTempPoint2_en",  0x01EC, "I", "flag",    "",    "FanTempPoint2 enabled"),
    ("FanPwmPoint2",      0x01FC, "i", "fan",     "%",   "Fan curve PWM 2"),              # rec 24
    ("FanPwmPoint2_en",   0x0200, "I", "flag",    "",    "FanPwmPoint2 enabled"),
    ("FanTempPoint3",     0x0210, "i", "fan",     "C",   "Fan curve temp 3"),             # rec 25
    ("FanTempPoint3_en",  0x0214, "I", "flag",    "",    "FanTempPoint3 enabled"),
    ("FanPwmPoint3",      0x0224, "i", "fan",     "%",   "Fan curve PWM 3"),              # rec 26
    ("FanPwmPoint3_en",   0x0228, "I", "flag",    "",    "FanPwmPoint3 enabled"),
    ("FanTempPoint4",     0x0238, "i", "fan",     "C",   "Fan curve temp 4"),             # rec 27
    ("FanTempPoint4_en",  0x023C, "I", "flag",    "",    "FanTempPoint4 enabled"),
    ("FanPwmPoint4",      0x024C, "i", "fan",     "%",   "Fan curve PWM 4"),              # rec 28
    ("FanPwmPoint4_en",   0x0250, "I", "flag",    "",    "FanPwmPoint4 enabled"),
    ("FanTempPoint5",     0x0260, "i", "fan",     "C",   "Fan curve temp 5"),             # rec 29
    ("FanTempPoint5_en",  0x0264, "I", "flag",    "",    "FanTempPoint5 enabled"),
    ("FanPwmPoint5",      0x0274, "i", "fan",     "%",   "Fan curve PWM 5"),              # rec 30
    ("FanPwmPoint5_en",   0x0278, "I", "flag",    "",    "FanPwmPoint5 enabled"),

    # ---- Voltage ----
    ("VoltageOffset",     0x0300, "i", "voltage", "mV",  "Voltage offset (signed)"),      # rec 37
    ("VoltageOffset_en",  0x0304, "I", "flag",    "",    "VoltageOffset enabled"),
]

# Set of offsets covered by known fields (for auto-discovery filtering)
_KNOWN_OFFSETS: Dict[int, str] = {}
for _n, _off, _fmt, *_ in _KNOWN_FIELDS:
    for _b in range(_off, _off + struct.calcsize(_fmt)):
        _KNOWN_OFFSETS[_b] = _n


# ---------------------------------------------------------------------------
# CnEscapeField
# ---------------------------------------------------------------------------

@dataclass
class CnEscapeField:
    """Metadata for a single editable field inside the CN escape blob."""
    name: str
    offset: int
    size: int
    fmt: str            # struct format char: 'I', 'i', 'H', 'h', 'B', 'b'
    value: int
    group: str          # "header", "clock", "memory", "power", "voltage", "curve", "unknown"
    unit: str = ""
    description: str = ""
    signed: bool = False

    @property
    def display_value(self) -> str:
        if self.signed:
            max_unsigned = 1 << (self.size * 8)
            half = max_unsigned >> 1
            if self.value >= half:
                return str(self.value - max_unsigned)
        return str(self.value)

    @property
    def display_value_int(self) -> int:
        if self.signed:
            max_unsigned = 1 << (self.size * 8)
            half = max_unsigned >> 1
            if self.value >= half:
                return self.value - max_unsigned
        return self.value


# ---------------------------------------------------------------------------
# CnEscapeCache
# ---------------------------------------------------------------------------

class CnEscapeCache:
    """Mutable PP_CNEscapeInput blob with parsed field map.

    After construction, ``fields`` is an OrderedDict of name -> CnEscapeField.
    Call ``set_field(name, value)`` to patch individual values, then
    ``to_bytes()`` (or ``write_to_registry()``) to get the modified blob.
    """

    def __init__(self, blob: bytes, *, source: str = "unknown"):
        self._blob = bytearray(blob)
        self._original = bytes(blob)
        self.source = source
        self.fields: OrderedDict[str, CnEscapeField] = OrderedDict()
        self._parse()

    # -- Construction helpers -----------------------------------------------

    @classmethod
    def from_registry(
        cls,
        adapter_key_path: str,
    ) -> Optional["CnEscapeCache"]:
        """Read PP_CNEscapeInput from the Windows registry.

        Args:
            adapter_key_path: Registry path relative to HKLM
                (e.g. ``SYSTEM\\...\\Class\\{guid}\\0000``).
        """
        try:
            from src.io.pptable_sources import read_registry_values
        except ImportError:
            _log.warning("from_registry: pptable_sources not importable")
            return None

        vals = read_registry_values(adapter_key_path, value_names=(_CN_REG_VALUE,))
        blob = vals.get(_CN_REG_VALUE)
        if not isinstance(blob, bytes) or len(blob) < 16:
            _log.info("from_registry: %s not present or too small at %s",
                       _CN_REG_VALUE, adapter_key_path)
            return None

        return cls(blob, source=f"registry:{adapter_key_path}")

    @classmethod
    def from_registry_scan(cls) -> Optional["CnEscapeCache"]:
        """Scan all AMD adapter keys for PP_CNEscapeInput.

        Useful when the primary adapter key doesn't hold the blob (e.g.
        multi-adapter systems where Adrenalin writes to a different subkey).
        """
        try:
            from src.io.pptable_sources import (
                read_registry_values, enumerate_display_adapters,
            )
        except ImportError:
            return None

        for info in enumerate_display_adapters():
            kp = info.get("key_path", "")
            mdid = info.get("MatchingDeviceId", "")
            if "VEN_1002" not in mdid.upper():
                continue
            vals = read_registry_values(kp, value_names=(_CN_REG_VALUE,))
            blob = vals.get(_CN_REG_VALUE)
            if isinstance(blob, bytes) and len(blob) >= 16:
                _log.info("from_registry_scan: found %s at %s (%d bytes)",
                           _CN_REG_VALUE, kp, len(blob))
                return cls(blob, source=f"registry:{kp}")

        _log.info("from_registry_scan: %s not found on any AMD adapter",
                    _CN_REG_VALUE)
        return None

    @classmethod
    def from_bytes(cls, blob: bytes, *, source: str = "raw") -> "CnEscapeCache":
        """Wrap an arbitrary CN escape blob (e.g. loaded from a file)."""
        return cls(blob, source=source)

    # -- Parsing ------------------------------------------------------------

    def _parse(self) -> None:
        """Populate self.fields from known offsets + auto-discovered non-zero dwords."""
        self.fields.clear()
        blob_len = len(self._blob)

        # 1) Known fields
        for name, offset, fmt, group, unit, desc in _KNOWN_FIELDS:
            sz = struct.calcsize(fmt)
            if offset + sz > blob_len:
                continue
            raw_val = struct.unpack_from(f"<{fmt}", self._blob, offset)[0]
            signed = fmt in ("i", "h", "b")
            self.fields[name] = CnEscapeField(
                name=name, offset=offset, size=sz, fmt=fmt,
                value=raw_val, group=group, unit=unit,
                description=desc, signed=signed,
            )

        # 2) Auto-discover non-zero dwords not covered by known fields
        n_dwords = blob_len // 4
        for i in range(n_dwords):
            off = i * 4
            if off in _KNOWN_OFFSETS:
                continue
            val = struct.unpack_from("<I", self._blob, off)[0]
            if val == 0:
                continue
            field_name = f"dw_0x{off:04X}"
            self.fields[field_name] = CnEscapeField(
                name=field_name, offset=off, size=4, fmt="I",
                value=val, group="unknown", unit="",
                description=f"Unknown dword at 0x{off:04X}",
            )

        _log.info("Parsed %d fields (%d known + %d auto) from %d-byte blob",
                   len(self.fields),
                   sum(1 for f in self.fields.values() if f.group != "unknown"),
                   sum(1 for f in self.fields.values() if f.group == "unknown"),
                   blob_len)

    # -- Field access -------------------------------------------------------

    def get_field(self, name: str) -> Optional[CnEscapeField]:
        """Return field metadata, or None if not found."""
        return self.fields.get(name)

    def get_value(self, name: str) -> Optional[int]:
        """Return current raw value of a named field."""
        f = self.fields.get(name)
        return f.value if f else None

    def get_display_value(self, name: str) -> Optional[int]:
        """Return current value with sign correction applied."""
        f = self.fields.get(name)
        return f.display_value_int if f else None

    def set_field(self, name: str, value: int) -> bool:
        """Modify a field value in the blob.

        For signed fields, accepts negative Python ints (e.g. -80 for
        VoltageOffset).  The value is packed with the field's struct format.

        Returns True if the field was found and written, False otherwise.
        """
        f = self.fields.get(name)
        if f is None:
            _log.warning("set_field: unknown field '%s'", name)
            return False

        if f.offset + f.size > len(self._blob):
            _log.error("set_field: offset 0x%04X + size %d exceeds blob (%d)",
                        f.offset, f.size, len(self._blob))
            return False

        try:
            struct.pack_into(f"<{f.fmt}", self._blob, f.offset, value)
        except struct.error as e:
            _log.error("set_field: pack error for '%s' value=%d: %s", name, value, e)
            return False

        f.value = struct.unpack_from(f"<{f.fmt}", self._blob, f.offset)[0]
        return True

    def set_field_at_offset(self, offset: int, fmt: str, value: int) -> bool:
        """Write a value at an arbitrary byte offset (for raw editing)."""
        sz = struct.calcsize(fmt)
        if offset + sz > len(self._blob):
            return False
        try:
            struct.pack_into(f"<{fmt}", self._blob, offset, value)
        except struct.error:
            return False
        for f in self.fields.values():
            if f.offset == offset:
                f.value = struct.unpack_from(f"<{f.fmt}", self._blob, f.offset)[0]
                break
        return True

    # -- Bulk field queries -------------------------------------------------

    def fields_by_group(self, group: str) -> List[CnEscapeField]:
        """Return all fields in a logical group."""
        return [f for f in self.fields.values() if f.group == group]

    def known_fields(self) -> List[CnEscapeField]:
        """Return only the fields from the known map (not auto-discovered)."""
        return [f for f in self.fields.values() if f.group != "unknown"]

    def unknown_fields(self) -> List[CnEscapeField]:
        """Return auto-discovered non-zero dwords."""
        return [f for f in self.fields.values() if f.group == "unknown"]

    def editable_fields(self) -> List[CnEscapeField]:
        """Return all fields except 'header' (read-only metadata)."""
        return [f for f in self.fields.values() if f.group != "header"]

    def od_fields(self) -> List[CnEscapeField]:
        """Return the primary OD-relevant fields (clock, memory, power, voltage, fan)."""
        od_groups = {"clock", "memory", "power", "voltage", "fan"}
        return [f for f in self.fields.values() if f.group in od_groups]

    # -- Blob output --------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Return the (possibly modified) blob as immutable bytes."""
        return bytes(self._blob)

    @property
    def original_bytes(self) -> bytes:
        """Return the unmodified blob as originally loaded."""
        return self._original

    @property
    def is_modified(self) -> bool:
        """True if any byte has been changed since load."""
        return bytes(self._blob) != self._original

    @property
    def size(self) -> int:
        return len(self._blob)

    def diff(self) -> List[Tuple[str, int, int]]:
        """Return list of (field_name, original_value, current_value) for changed fields."""
        changes = []
        for f in self.fields.values():
            try:
                orig_val = struct.unpack_from(f"<{f.fmt}", self._original, f.offset)[0]
            except struct.error:
                continue
            if orig_val != f.value:
                changes.append((f.name, orig_val, f.value))
        return changes

    def reset(self) -> None:
        """Revert all modifications back to original blob."""
        self._blob = bytearray(self._original)
        self._parse()

    def clone(self) -> "CnEscapeCache":
        """Return an independent copy."""
        return CnEscapeCache(bytes(self._blob), source=self.source)

    # -- Registry I/O -------------------------------------------------------

    def write_to_registry(self, adapter_key_path: str) -> bool:
        """Write the current blob to registry as PP_CNEscapeInput.

        Args:
            adapter_key_path: Registry path relative to HKLM.

        Returns:
            True on success, False on failure.
        """
        try:
            from src.io.pptable_sources import write_registry_binary
        except ImportError:
            _log.error("write_to_registry: pptable_sources not importable")
            return False

        blob = self.to_bytes()
        _log.info("write_to_registry: writing %d bytes to %s\\%s",
                   len(blob), adapter_key_path, _CN_REG_VALUE)
        return write_registry_binary(adapter_key_path, _CN_REG_VALUE, blob)

    @staticmethod
    def delete_from_registry(adapter_key_path: str) -> bool:
        """Delete PP_CNEscapeInput from registry."""
        try:
            from src.io.pptable_sources import delete_registry_value
        except ImportError:
            return False
        return delete_registry_value(adapter_key_path, _CN_REG_VALUE)

    @staticmethod
    def read_registry_status(adapter_key_path: str) -> Optional[int]:
        """Check if CN escape blob exists in registry. Returns size or None."""
        try:
            from src.io.pptable_sources import read_registry_values
        except ImportError:
            return None
        vals = read_registry_values(adapter_key_path, value_names=(_CN_REG_VALUE,))
        blob = vals.get(_CN_REG_VALUE)
        if isinstance(blob, bytes):
            return len(blob)
        return None

    # -- Hex export ---------------------------------------------------------

    def export_hex(self) -> str:
        """Return the blob as a hex string (for clipboard / file export)."""
        return self._blob.hex()

    @classmethod
    def from_hex(cls, hex_str: str, *, source: str = "hex") -> Optional["CnEscapeCache"]:
        """Import a blob from a hex string."""
        try:
            blob = bytes.fromhex(hex_str.strip())
        except ValueError:
            return None
        if len(blob) < 16:
            return None
        return cls(blob, source=source)

    # -- Display / debug ----------------------------------------------------

    def summary(self) -> str:
        """One-line summary of key OD values."""
        parts = [f"CNEscape {self.size}B from {self.source}"]
        for name in ("GfxclkFoffset", "UclkFmax", "VoltageOffset", "Ppt"):
            f = self.fields.get(name)
            if f:
                parts.append(f"{name}={f.display_value}")
        return "  ".join(parts)

    def dump_fields(self, groups: Optional[List[str]] = None) -> str:
        """Pretty-print all fields (or a subset of groups) for diagnostics."""
        lines = [f"CnEscapeCache: {self.size} bytes, source={self.source}, "
                 f"{len(self.fields)} fields"]
        current_group = ""
        for f in self.fields.values():
            if groups and f.group not in groups:
                continue
            if f.group != current_group:
                current_group = f.group
                lines.append(f"\n  [{current_group.upper()}]")
            val_str = f.display_value
            try:
                orig_val = struct.unpack_from(f"<{f.fmt}", self._original, f.offset)[0]
                mod = " *" if f.value != orig_val else ""
            except struct.error:
                mod = ""
            desc = f"  ({f.description})" if f.description else ""
            lines.append(
                f"    {f.name:24s}  off=0x{f.offset:04X}  "
                f"val={val_str:>8s} {f.unit:4s}{mod}{desc}"
            )
        return "\n".join(lines)

    def hexdump(self, width: int = 16) -> str:
        """Return a hex dump of the entire blob."""
        lines = []
        data = bytes(self._blob)
        for row_start in range(0, len(data), width):
            row = data[row_start:row_start + width]
            hex_str = " ".join(f"{b:02X}" for b in row)
            ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"  {row_start:04X}  {hex_str:<{width * 3}}  {ascii_str}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (f"<CnEscapeCache size={self.size} source={self.source!r} "
                f"fields={len(self.fields)}>")
