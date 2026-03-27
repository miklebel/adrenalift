"""
settings.py -- Unified persistent settings for Adrenalift.

Stores all user-facing configuration in a single ``settings.json``
located next to the executable (frozen) or in the project root (dev).

On first load the module automatically migrates the legacy
``.dma_offset_cache.json`` file so nothing is lost on upgrade.
"""

import json
import logging
import os
import sys
import threading

_log = logging.getLogger("overclock.settings")

# ---------------------------------------------------------------------------
# Resolve settings directory (same logic as overclock_engine._project_root)
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _SETTINGS_DIR = os.path.dirname(sys.executable)
else:
    _SETTINGS_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

_SETTINGS_PATH = os.path.join(_SETTINGS_DIR, "settings.json")
_LEGACY_DMA_CACHE_PATH = os.path.join(_SETTINGS_DIR, ".dma_offset_cache.json")

# ---------------------------------------------------------------------------
# Default schema
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "dma_cache": {},
    "pptable_cache": {},
    "defaults": {
        "simple_clock_mhz": 3500,
        "scan_workers": None,
        "scan_on_startup": False,
        "apply_after_scan_on_startup": False,
    },
}


class Settings:
    """Thread-safe, lazily-loaded wrapper around ``settings.json``.

    Usage::

        cfg = Settings()
        cfg.get("defaults.simple_clock_mhz")   # -> 3500
        cfg.set("defaults.simple_clock_mhz", 3200)

    Dot-separated keys navigate nested dicts.
    """

    def __init__(self, path: str = _SETTINGS_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict | None = None  # lazy

    # -- public API ---------------------------------------------------------

    def load(self) -> dict:
        """Return the full settings dict (deep copy). Creates the file
        with defaults + migration if it doesn't exist yet."""
        with self._lock:
            self._ensure_loaded()
            return json.loads(json.dumps(self._data))  # cheap deep copy

    def save(self, data: dict) -> None:
        """Replace the entire settings dict and flush to disk."""
        with self._lock:
            self._data = data
            self._flush()

    def get(self, key: str, default=None):
        """Read a value by dot-separated key.

        ``settings.get("defaults.simple_clock_mhz")``
        """
        with self._lock:
            self._ensure_loaded()
            return self._walk(key, default)

    def set(self, key: str, value) -> None:
        """Write a value by dot-separated key and flush to disk.

        ``settings.set("defaults.simple_clock_mhz", 3200)``
        """
        with self._lock:
            self._ensure_loaded()
            parts = key.split(".")
            node = self._data
            for p in parts[:-1]:
                if p not in node or not isinstance(node[p], dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value
            self._flush()

    # -- internals ----------------------------------------------------------

    def _ensure_loaded(self):
        """Load from disk once (caller holds ``_lock``)."""
        if self._data is not None:
            return
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                _log.info("Settings loaded from %s", self._path)
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Corrupt settings file, resetting: %s", exc)
                self._data = None

        if self._data is None:
            self._data = json.loads(json.dumps(_DEFAULTS))
            self._migrate_legacy_dma_cache()
            self._flush()

        self._backfill_defaults(self._data, _DEFAULTS)

    def _migrate_legacy_dma_cache(self):
        """Import ``.dma_offset_cache.json`` into ``dma_cache`` key."""
        if not os.path.isfile(_LEGACY_DMA_CACHE_PATH):
            return
        try:
            with open(_LEGACY_DMA_CACHE_PATH, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            if isinstance(legacy, dict) and legacy.get("offset"):
                self._data["dma_cache"] = legacy
                _log.info(
                    "Migrated legacy DMA cache (offset=0x%X) into settings.json",
                    legacy["offset"],
                )
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Could not migrate legacy DMA cache: %s", exc)

    @staticmethod
    def _backfill_defaults(data: dict, defaults: dict):
        """Ensure every key in *defaults* exists in *data* without
        overwriting user values."""
        for k, v in defaults.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
            elif isinstance(v, dict) and isinstance(data[k], dict):
                Settings._backfill_defaults(data[k], v)

    def _walk(self, key: str, default):
        """Traverse ``_data`` along dot-separated *key*."""
        node = self._data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def _flush(self):
        """Write ``_data`` to disk (caller holds ``_lock``)."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError as exc:
            _log.error("Failed to write settings: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton for convenience
# ---------------------------------------------------------------------------

settings = Settings()
