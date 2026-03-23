"""
SPPT cache: read, parse, modify, and prepare PP table blobs for registry
write as PP_PhmSoftPowerPlayTable.

Sources:
  - VBIOS ROM  (via upp PP table extraction)
  - SMU DMA    (TABLE_PPTABLE transfer)
  - Existing registry override (read back current blob)

The blob is the full smu_14_0_2_powerplay_table binary that the Windows
AMD driver accepts when stored under the adapter class key as
``PP_PhmSoftPowerPlayTable`` (REG_BINARY).  A reboot is required for the
driver to pick it up.

Typical flow::

    cache = SpptCache.from_vbios("bios/vbios.rom")
    cache.set_field("BoostClockAc", 3500)
    cache.set_field("Power_0_AC", 250)
    ok = cache.write_to_registry(adapter_key_path)
"""

from __future__ import annotations

import copy
import logging
import os
import struct
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

_log = logging.getLogger("overclock.sppt_cache")

# ---------------------------------------------------------------------------
# UPP import (optional, same pattern as vbios_parser.py)
# ---------------------------------------------------------------------------

_UPP_AVAILABLE = False
try:
    if not getattr(sys, "frozen", False):
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _upp_src = os.path.join(_script_dir, "..", "..", "deps", "upp", "src")
        if os.path.isdir(_upp_src) and _upp_src not in sys.path:
            sys.path.insert(0, os.path.abspath(_upp_src))
    from upp import decode as _upp_decode
    _UPP_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPPT_REG_VALUE = "PP_PhmSoftPowerPlayTable"

# $PS1 magic bytes for locating PP table in VBIOS (RDNA4 / RDNA3)
_PS1_RDNA4 = b'\x24\x50\x53\x31\xe0\x16'
_PS1_RDNA3 = b'\x24\x50\x53\x31\x50\x15'
_LEGACY_VROM_OFFSET = 0x40000

# MsgLimits Power array layout: Power[PPT_idx][AC=0/DC=1], 4 PPT slots x 2
_TEMP_NAMES = [
    "Edge", "Hotspot", "HSGFX", "HSSOC", "Mem",
    "Liquid0", "VR_GFX", "VR_SOC", "VR_MEM",
    "Liquid1", "PLX", "Spare",
]


# ---------------------------------------------------------------------------
# SpptField
# ---------------------------------------------------------------------------

@dataclass
class SpptField:
    """Metadata for a single editable field inside the PP table blob."""
    name: str
    offset: int          # byte offset from start of blob
    size: int            # field size in bytes (2 = u16, 4 = u32, etc.)
    fmt: str             # struct format char: 'H', 'h', 'I', 'i', 'B', 'b'
    value: int           # current value (parsed from blob)
    group: str           # logical group: "clock", "power", "tdc", "temp", "od", "fan", "meta"
    unit: str = ""       # display unit: "MHz", "W", "A", "C", "%", "mV"
    path: str = ""       # UPP tree path (for debugging / display)
    signed: bool = False

    @property
    def display_value(self) -> str:
        if self.signed and self.value > (1 << (self.size * 8 - 1)):
            signed_val = self.value - (1 << (self.size * 8))
            return f"{signed_val}"
        return str(self.value)


# ---------------------------------------------------------------------------
# PP table location in VBIOS ROM
# ---------------------------------------------------------------------------

def _get_pp_table_from_rom(rom_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Locate PP table in RDNA3/4 VBIOS via $PS1 magic.

    Returns (absolute_offset, pp_length) or None.
    """
    rom_offset = 0
    search_bytes = rom_bytes

    if len(rom_bytes) >= 2:
        magic = rom_bytes[:2].hex().upper()
        if magic == "AA55":
            rom_offset = _LEGACY_VROM_OFFSET
            if len(rom_bytes) < rom_offset + 64:
                return None
            search_bytes = rom_bytes[rom_offset:]

    if len(search_bytes) < 2 or search_bytes[:2].hex().upper() != "55AA":
        return None

    for magic in (_PS1_RDNA4, _PS1_RDNA3):
        idx = search_bytes.find(magic)
        if idx >= 0:
            pp_base = rom_offset + idx + 0x110
            if pp_base + 4 > len(rom_bytes):
                continue
            pp_len = struct.unpack_from("<H", rom_bytes, pp_base)[0]
            if 64 <= pp_len <= 65535 and pp_base + pp_len <= len(rom_bytes):
                return (pp_base, pp_len)
    return None


# ---------------------------------------------------------------------------
# Field extraction via UPP
# ---------------------------------------------------------------------------

def _extract_fields_upp(pp_bytes: bytearray) -> Optional[List[SpptField]]:
    """Use UPP's structured decode to extract all editable fields with offsets."""
    if not _UPP_AVAILABLE:
        return None

    try:
        data = _upp_decode.select_pp_struct(pp_bytes, rawdump=False, debug=False)
    except Exception:
        return None
    if data is None:
        return None

    def _get_info(path: str) -> Optional[dict]:
        parts = path.strip("/").split("/")
        normalized = [int(p) if p.isdigit() else p for p in parts]
        try:
            res = _upp_decode.get_value(None, normalized, data_dict=data)
            return res if res and "value" in res else None
        except (KeyError, TypeError, ValueError):
            return None

    blob_len = len(pp_bytes)
    fields: List[SpptField] = []

    # --- DriverReportedClocks ---
    _DRC_PREFIX = "smc_pptable/SkuTable/DriverReportedClocks"
    _DRC_FIELDS = [
        ("BaseClockAc",    "H", "clock", "MHz"),
        ("GameClockAc",    "H", "clock", "MHz"),
        ("BoostClockAc",   "H", "clock", "MHz"),
        ("BaseClockDc",    "H", "clock", "MHz"),
        ("GameClockDc",    "H", "clock", "MHz"),
        ("BoostClockDc",   "H", "clock", "MHz"),
        ("MaxReportedClock", "H", "clock", "MHz"),
    ]
    for fname, fmt, group, unit in _DRC_FIELDS:
        info = _get_info(f"{_DRC_PREFIX}/{fname}")
        if info is None:
            continue
        fields.append(SpptField(
            name=fname, offset=info["offset"],
            size=struct.calcsize(fmt), fmt=fmt,
            value=int(info["value"]), group=group, unit=unit,
            path=f"{_DRC_PREFIX}/{fname}",
        ))

    # --- MsgLimits ---
    _ML_PREFIX = "smc_pptable/SkuTable/MsgLimits"

    # Power[ppt_idx][ac_dc] — 4 PPT slots, each with AC (0) and DC (1)
    for ppt in range(4):
        for ac_dc, label in ((0, "AC"), (1, "DC")):
            path = f"{_ML_PREFIX}/Power/{ppt}/{ac_dc}"
            info = _get_info(path)
            if info is None:
                continue
            fields.append(SpptField(
                name=f"Power_{ppt}_{label}", offset=info["offset"],
                size=2, fmt="H", value=int(info["value"]),
                group="power", unit="W", path=path,
            ))

    # Tdc[0]=GFX, Tdc[1]=SOC
    for idx, label in ((0, "GFX"), (1, "SOC")):
        path = f"{_ML_PREFIX}/Tdc/{idx}"
        info = _get_info(path)
        if info is None:
            continue
        fields.append(SpptField(
            name=f"Tdc_{label}", offset=info["offset"],
            size=2, fmt="H", value=int(info["value"]),
            group="tdc", unit="A", path=path,
        ))

    # Temperature[0..11]
    for idx in range(12):
        path = f"{_ML_PREFIX}/Temperature/{idx}"
        info = _get_info(path)
        if info is None:
            continue
        label = _TEMP_NAMES[idx] if idx < len(_TEMP_NAMES) else f"Temp{idx}"
        fields.append(SpptField(
            name=f"Temp_{label}", offset=info["offset"],
            size=2, fmt="H", value=int(info["value"]),
            group="temp", unit="C", path=path,
        ))

    # MsgLimits fan/acoustic fields
    _ML_EXTRA = [
        ("PwmLimitMin", "B", "fan", ""),
        ("PwmLimitMax", "B", "fan", ""),
        ("FanTargetTemperature", "B", "fan", "C"),
        ("AcousticTargetRpmThresholdMin", "H", "fan", "RPM"),
        ("AcousticTargetRpmThresholdMax", "H", "fan", "RPM"),
        ("AcousticLimitRpmThresholdMin", "H", "fan", "RPM"),
        ("AcousticLimitRpmThresholdMax", "H", "fan", "RPM"),
        ("PccLimitMin", "H", "power", ""),
        ("PccLimitMax", "H", "power", ""),
        ("FanStopTempMin", "H", "fan", "C"),
        ("FanStopTempMax", "H", "fan", "C"),
        ("FanStartTempMin", "H", "fan", "C"),
        ("FanStartTempMax", "H", "fan", "C"),
    ]
    for fname, fmt, group, unit in _ML_EXTRA:
        info = _get_info(f"{_ML_PREFIX}/{fname}")
        if info is None:
            continue
        fields.append(SpptField(
            name=f"ML_{fname}", offset=info["offset"],
            size=struct.calcsize(fmt), fmt=fmt,
            value=int(info["value"]), group=group, unit=unit,
            path=f"{_ML_PREFIX}/{fname}",
        ))

    # PowerMinPpt0[0], PowerMinPpt0[1]
    for idx in range(2):
        path = f"{_ML_PREFIX}/PowerMinPpt0/{idx}"
        info = _get_info(path)
        if info is None:
            continue
        fields.append(SpptField(
            name=f"PowerMinPpt0_{idx}", offset=info["offset"],
            size=2, fmt="H", value=int(info["value"]),
            group="power", unit="W", path=path,
        ))

    # --- OverDriveLimitsBasicMax (the relevant one for current OD ceilings) ---
    _OD_PREFIX = "smc_pptable/SkuTable/OverDriveLimitsBasicMax"
    _OD_FIELDS = [
        ("FeatureCtrlMask", "I", "od", "", False),
        ("GfxclkFoffset",   "h", "od", "MHz", True),
        ("UclkFmin",        "H", "od", "MHz", False),
        ("UclkFmax",        "H", "od", "MHz", False),
        ("FclkFmin",        "H", "od", "MHz", False),
        ("FclkFmax",        "H", "od", "MHz", False),
        ("Ppt",             "h", "od", "%", True),
        ("Tdc",             "h", "od", "%", True),
        ("VddGfxVmax",      "H", "od", "mV", False),
        ("VddSocVmax",      "H", "od", "mV", False),
        ("GfxEdc",          "h", "od", "%", True),
        ("GfxPccLimitControl", "h", "od", "", True),
    ]
    for entry in _OD_FIELDS:
        fname, fmt, group, unit, signed = entry
        info = _get_info(f"{_OD_PREFIX}/{fname}")
        if info is None:
            continue
        fields.append(SpptField(
            name=f"OD_{fname}", offset=info["offset"],
            size=struct.calcsize(fmt), fmt=fmt,
            value=int(info["value"]), group=group, unit=unit,
            path=f"{_OD_PREFIX}/{fname}", signed=signed,
        ))

    # VoltageOffsetPerZoneBoundary[0..5]
    for idx in range(6):
        path = f"{_OD_PREFIX}/VoltageOffsetPerZoneBoundary/{idx}"
        info = _get_info(path)
        if info is None:
            continue
        fields.append(SpptField(
            name=f"OD_VoltOffset_Zone{idx}", offset=info["offset"],
            size=2, fmt="h", value=int(info["value"]),
            group="od", unit="mV", path=path, signed=True,
        ))

    # --- Header metadata (read-only display) ---
    for fname, fmt, group, unit in [
        ("golden_pp_id", "I", "meta", ""),
        ("golden_revision", "I", "meta", ""),
        ("format_id", "H", "meta", ""),
        ("thermal_controller_type", "B", "meta", ""),
    ]:
        info = _get_info(fname)
        if info is None:
            continue
        fields.append(SpptField(
            name=fname, offset=info["offset"],
            size=struct.calcsize(fmt), fmt=fmt,
            value=int(info["value"]), group=group, unit=unit,
            path=fname,
        ))

    # Filter out fields whose offsets exceed the blob
    fields = [f for f in fields if f.offset + f.size <= blob_len]
    return fields if fields else None


# ---------------------------------------------------------------------------
# SpptCache
# ---------------------------------------------------------------------------

class SpptCache:
    """Mutable PP table blob with parsed field map.

    After construction, ``fields`` is an OrderedDict of name -> SpptField.
    Call ``set_field(name, value)`` to patch individual values, then
    ``to_bytes()`` (or ``write_to_registry()``) to get the modified blob.
    """

    def __init__(self, blob: bytes, *, source: str = "unknown"):
        self._blob = bytearray(blob)
        self._original = bytes(blob)
        self.source = source
        self.fields: OrderedDict[str, SpptField] = OrderedDict()
        self._parse()

    # -- Construction helpers -----------------------------------------------

    @classmethod
    def from_vbios(cls, rom_path: str) -> Optional["SpptCache"]:
        """Read PP table blob from a VBIOS ROM file.

        Handles VBEN-encoded files (XOR storage used to keep the raw PP table
        pattern out of the page cache) transparently.
        """
        if not rom_path:
            return None
        p = rom_path
        if not os.path.isabs(p):
            proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            p = os.path.join(proj, p)

        try:
            from src.io.vbios_storage import read_vbios_decoded
            rom_bytes, _ = read_vbios_decoded(p)
            if rom_bytes is None:
                _log.warning("from_vbios: read_vbios_decoded returned None for %s", p)
                return None
        except ImportError:
            try:
                with open(p, "rb") as f:
                    rom_bytes = f.read()
            except OSError as e:
                _log.warning("from_vbios: cannot read %s: %s", p, e)
                return None

        return cls.from_vbios_bytes(rom_bytes, source=f"vbios:{rom_path}")

    @classmethod
    def from_vbios_bytes(cls, rom_bytes: bytes, *, source: str = "vbios") -> Optional["SpptCache"]:
        """Extract PP table blob from in-memory VBIOS ROM image.

        Expects already-decoded bytes (VBEN decoding is handled by from_vbios).
        """
        result = _get_pp_table_from_rom(rom_bytes)
        if result is None:
            _log.warning("from_vbios_bytes: no PP table found in ROM (%d bytes)", len(rom_bytes))
            return None

        pp_offset, pp_len = result
        blob = rom_bytes[pp_offset:pp_offset + pp_len]
        _log.info("from_vbios_bytes: PP table at 0x%04X, %d bytes", pp_offset, pp_len)
        return cls(blob, source=source)

    @classmethod
    def from_smu(cls, smu, virt: int, **kwargs) -> Optional["SpptCache"]:
        """Read PP table blob from SMU DMA transfer (TABLE_PPTABLE=0).

        Requires an initialized hardware context (smu + mapped DMA buffer).
        """
        try:
            from src.io.pptable_sources import read_smu_pptable_blob
        except ImportError:
            _log.warning("from_smu: pptable_sources not available")
            return None

        raw = read_smu_pptable_blob(smu, virt, **kwargs)
        if raw is None:
            _log.warning("from_smu: SMU transfer returned None")
            return None

        # The raw DMA buffer is 256 KB; the actual PP table is smaller.
        # Try to determine real size from the header structsize field.
        if len(raw) >= 4:
            hdr_size = struct.unpack_from("<H", raw, 0)[0]
            if 64 <= hdr_size <= len(raw):
                raw = raw[:hdr_size]
                _log.info("from_smu: trimmed to header size %d bytes", hdr_size)

        return cls(raw, source="smu")

    @classmethod
    def from_registry(
        cls,
        vendor_id: Union[int, str] = 0x1002,
        device_id: Union[int, str, None] = None,
        *,
        adapter_key: Optional[str] = None,
    ) -> Optional["SpptCache"]:
        """Read existing PP_PhmSoftPowerPlayTable override from registry."""
        try:
            from src.io.pptable_sources import (
                read_registry_pptable_blob,
                read_registry_values,
                find_display_adapter_class_keys,
            )
        except ImportError:
            return None

        if adapter_key:
            vals = read_registry_values(adapter_key, value_names=(_SPPT_REG_VALUE,))
            blob = vals.get(_SPPT_REG_VALUE)
            if isinstance(blob, bytes) and len(blob) >= 64:
                return cls(blob, source=f"registry:{adapter_key}")
            return None

        if device_id is not None:
            blobs = read_registry_pptable_blob(vendor_id, device_id)
            blob = blobs.get(_SPPT_REG_VALUE)
            if isinstance(blob, bytes) and len(blob) >= 64:
                return cls(blob, source="registry")

        return None

    @classmethod
    def from_registry_scan(cls) -> Optional["SpptCache"]:
        """Scan all AMD adapter keys for PP_PhmSoftPowerPlayTable.

        Useful when the primary adapter key doesn't hold the SPPT blob
        (Adrenalin / the driver may store it on a different subkey).
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
            vals = read_registry_values(kp, value_names=(_SPPT_REG_VALUE,))
            blob = vals.get(_SPPT_REG_VALUE)
            if isinstance(blob, bytes) and len(blob) >= 64:
                _log.info("from_registry_scan: found %s at %s (%d bytes)",
                          _SPPT_REG_VALUE, kp, len(blob))
                return cls(blob, source=f"registry:{kp}")

        _log.info("from_registry_scan: %s not found on any AMD adapter",
                   _SPPT_REG_VALUE)
        return None

    @classmethod
    def from_bytes(cls, blob: bytes, *, source: str = "raw") -> "SpptCache":
        """Wrap an arbitrary PP table blob (e.g. loaded from a file)."""
        return cls(blob, source=source)

    # -- Parsing ------------------------------------------------------------

    def _parse(self) -> None:
        """Populate self.fields from the blob using UPP or fallback."""
        self.fields.clear()

        fields = _extract_fields_upp(self._blob)
        if fields:
            for f in fields:
                self.fields[f.name] = f
            _log.info("Parsed %d fields via UPP from %d-byte blob", len(self.fields), len(self._blob))
            return

        self._parse_fallback()

    def _parse_fallback(self) -> None:
        """Byte-offset fallback when UPP is not available.

        Uses the known ctypes struct layout from smu_v14_0_2_navi40.py to
        compute offsets at import time.  This covers the primary fields in
        DriverReportedClocks and MsgLimits.
        """
        try:
            from upp.atom_gen.smu_v14_0_2_navi40 import (
                struct_smu_14_0_2_powerplay_table as PPTop,
                struct_PPTable_t,
                struct_SkuTable_t as SkuT,
                struct_DriverReportedClocks_t as DRC,
                struct_MsgLimits_t as ML,
                struct_OverDriveLimits_t as ODL,
            )
        except ImportError:
            _log.warning("Fallback parse failed: upp structs not importable")
            return

        blob_len = len(self._blob)

        def _top_offset(field_name: str) -> int:
            return getattr(PPTop, field_name).offset

        def _sku_offset(field_name: str) -> int:
            smc_off = _top_offset("smc_pptable")
            pfe_size = getattr(struct_PPTable_t, "SkuTable").offset
            return smc_off + pfe_size + getattr(SkuT, field_name).offset

        def _drc_offset(field_name: str) -> int:
            return _sku_offset("DriverReportedClocks") + getattr(DRC, field_name).offset

        def _ml_offset(field_name: str) -> int:
            return _sku_offset("MsgLimits") + getattr(ML, field_name).offset

        def _odmax_offset(field_name: str) -> int:
            return _sku_offset("OverDriveLimitsBasicMax") + getattr(ODL, field_name).offset

        def _read(offset: int, fmt: str) -> int:
            sz = struct.calcsize(fmt)
            if offset + sz > blob_len:
                return 0
            return struct.unpack_from(f"<{fmt}", self._blob, offset)[0]

        def _add(name, offset, fmt, group, unit, signed=False, path=""):
            sz = struct.calcsize(fmt)
            if offset + sz > blob_len:
                return
            val = _read(offset, fmt)
            self.fields[name] = SpptField(
                name=name, offset=offset, size=sz,
                fmt=fmt, value=val, group=group, unit=unit,
                path=path, signed=signed,
            )

        # DriverReportedClocks
        for fname, unit in [
            ("BaseClockAc", "MHz"), ("GameClockAc", "MHz"), ("BoostClockAc", "MHz"),
            ("BaseClockDc", "MHz"), ("GameClockDc", "MHz"), ("BoostClockDc", "MHz"),
            ("MaxReportedClock", "MHz"),
        ]:
            _add(fname, _drc_offset(fname), "H", "clock", unit)

        # MsgLimits.Power — flat array c_uint16 * 2 * 4
        ml_power_base = _ml_offset("Power")
        for ppt in range(4):
            for ac_dc, label in ((0, "AC"), (1, "DC")):
                off = ml_power_base + (ppt * 4 + ac_dc * 2)
                _add(f"Power_{ppt}_{label}", off, "H", "power", "W")

        # MsgLimits.Tdc
        ml_tdc_base = _ml_offset("Tdc")
        _add("Tdc_GFX", ml_tdc_base, "H", "tdc", "A")
        _add("Tdc_SOC", ml_tdc_base + 2, "H", "tdc", "A")

        # MsgLimits.Temperature
        ml_temp_base = _ml_offset("Temperature")
        for idx in range(12):
            label = _TEMP_NAMES[idx] if idx < len(_TEMP_NAMES) else f"Temp{idx}"
            _add(f"Temp_{label}", ml_temp_base + idx * 2, "H", "temp", "C")

        # OverDriveLimitsBasicMax
        for fname, fmt, unit, signed in [
            ("FeatureCtrlMask", "I", "", False),
            ("GfxclkFoffset", "h", "MHz", True),
            ("UclkFmin", "H", "MHz", False),
            ("UclkFmax", "H", "MHz", False),
            ("Ppt", "h", "%", True),
            ("Tdc", "h", "%", True),
        ]:
            _add(f"OD_{fname}", _odmax_offset(fname), fmt, "od", unit, signed=signed)

        _log.info("Parsed %d fields via fallback from %d-byte blob", len(self.fields), blob_len)

    # -- Field access -------------------------------------------------------

    def get_field(self, name: str) -> Optional[SpptField]:
        """Return field metadata, or None if not found."""
        return self.fields.get(name)

    def get_value(self, name: str) -> Optional[int]:
        """Return current value of a named field."""
        f = self.fields.get(name)
        return f.value if f else None

    def set_field(self, name: str, value: int) -> bool:
        """Modify a field value in the blob.

        Returns True if the field was found and written, False otherwise.
        """
        f = self.fields.get(name)
        if f is None:
            _log.warning("set_field: unknown field '%s'", name)
            return False

        if f.offset + f.size > len(self._blob):
            _log.error("set_field: offset %d + size %d exceeds blob (%d)",
                       f.offset, f.size, len(self._blob))
            return False

        struct.pack_into(f"<{f.fmt}", self._blob, f.offset, value)
        f.value = value
        return True

    def set_field_at_offset(self, offset: int, fmt: str, value: int) -> bool:
        """Write a value at an arbitrary byte offset (for advanced / raw editing)."""
        sz = struct.calcsize(fmt)
        if offset + sz > len(self._blob):
            return False
        struct.pack_into(f"<{fmt}", self._blob, offset, value)
        for f in self.fields.values():
            if f.offset == offset:
                f.value = value
                break
        return True

    # -- Bulk field queries -------------------------------------------------

    def fields_by_group(self, group: str) -> List[SpptField]:
        """Return all fields in a logical group."""
        return [f for f in self.fields.values() if f.group == group]

    def clock_fields(self) -> List[SpptField]:
        return self.fields_by_group("clock")

    def power_fields(self) -> List[SpptField]:
        return self.fields_by_group("power")

    def tdc_fields(self) -> List[SpptField]:
        return self.fields_by_group("tdc")

    def temp_fields(self) -> List[SpptField]:
        return self.fields_by_group("temp")

    def od_fields(self) -> List[SpptField]:
        return self.fields_by_group("od")

    def all_editable_fields(self) -> List[SpptField]:
        """Return all fields except 'meta' (read-only header info)."""
        return [f for f in self.fields.values() if f.group != "meta"]

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
        """True if any field has been changed since load."""
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

    def clone(self) -> "SpptCache":
        """Return an independent copy."""
        return SpptCache(bytes(self._blob), source=self.source)

    # -- Registry I/O -------------------------------------------------------

    def write_to_registry(self, adapter_key_path: str) -> bool:
        """Write the current blob to registry as PP_PhmSoftPowerPlayTable.

        Args:
            adapter_key_path: Registry path relative to HKLM
                (e.g. ``SYSTEM\\...\\Class\\{guid}\\0000``).

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
                   len(blob), adapter_key_path, _SPPT_REG_VALUE)
        return write_registry_binary(adapter_key_path, _SPPT_REG_VALUE, blob)

    @staticmethod
    def delete_from_registry(adapter_key_path: str) -> bool:
        """Delete PP_PhmSoftPowerPlayTable from registry (revert to VBIOS)."""
        try:
            from src.io.pptable_sources import delete_registry_value
        except ImportError:
            return False
        return delete_registry_value(adapter_key_path, _SPPT_REG_VALUE)

    @staticmethod
    def read_registry_status(adapter_key_path: str) -> Optional[int]:
        """Check if SPPT override exists in registry. Returns size or None."""
        try:
            from src.io.pptable_sources import read_registry_values
        except ImportError:
            return None
        vals = read_registry_values(adapter_key_path, value_names=(_SPPT_REG_VALUE,))
        blob = vals.get(_SPPT_REG_VALUE)
        if isinstance(blob, bytes):
            return len(blob)
        return None

    # -- Display / debug ----------------------------------------------------

    def summary(self) -> str:
        """One-line summary of key values."""
        parts = [f"SPPT {self.size}B from {self.source}"]
        for name in ("BaseClockAc", "GameClockAc", "BoostClockAc"):
            f = self.fields.get(name)
            if f:
                parts.append(f"{name}={f.value}")
        for name in ("Power_0_AC", "Tdc_GFX"):
            f = self.fields.get(name)
            if f:
                parts.append(f"{name}={f.value}")
        return "  ".join(parts)

    def dump_fields(self, groups: Optional[List[str]] = None) -> str:
        """Pretty-print all fields (or a subset of groups) for diagnostics."""
        lines = [f"SpptCache: {self.size} bytes, source={self.source}, "
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
            lines.append(
                f"    {f.name:30s}  off=0x{f.offset:04X}  "
                f"val={val_str:>8s} {f.unit:4s}{mod}"
            )
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<SpptCache size={self.size} source={self.source!r} fields={len(self.fields)}>"
