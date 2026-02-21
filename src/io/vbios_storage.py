"""
VBIOS storage with encode-on-write / decode-on-read.

Stores VBIOS in XOR-encoded form so the file on disk never contains the raw
PP table pattern. The memory scanner would otherwise find and patch the pattern
in the page cache, corrupting the file.
"""

from __future__ import annotations

import os
from typing import Tuple

_VBIOS_ENC_MAGIC = b"VBEN"
_VBIOS_ENC_KEY = b"RDNA4_VBIOS_ENCODED_v1"


def encode_vbios(rom_bytes: bytes) -> bytes:
    """Encode raw VBIOS so the file on disk never contains the PP table pattern."""
    key = _VBIOS_ENC_KEY
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(rom_bytes))


def decode_vbios(encoded: bytes) -> bytes:
    """Decode VBIOS (XOR is symmetric)."""
    return encode_vbios(encoded)


def read_vbios_decoded(path: str) -> Tuple[bytes | None, bool]:
    """Read VBIOS from disk, decoding if stored in encoded format.

    Returns (decoded_bytes, was_encoded). was_encoded is True if file had VBEN magic.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, False
    if len(data) >= 4 and data[:4] == _VBIOS_ENC_MAGIC:
        return decode_vbios(data[4:]), True
    return data, False


def write_vbios_encoded(path: str, rom_bytes: bytes) -> bool:
    """Write VBIOS in encoded form so page cache never holds raw PP table pattern."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(_VBIOS_ENC_MAGIC + encode_vbios(rom_bytes))
        return True
    except OSError:
        return False
