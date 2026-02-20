"""
Registry Anti-Clock-Gating Patch
=================================

Patches AMD GPU driver registry values to disable power-saving features
that cause clock gating, ULPS, and idle downclocking.  These changes are
persistent across reboots (unlike the kernel memory patches in overclock.py)
and complement the SMU-level overrides.

Values patched:
  - EnableUlps                              -> 0  (disable Ultra Low Power State)
  - PP_GPUPowerDownEnabled                  -> 0  (disable GPU power gating)
  - EnableUvdClockGating                    -> 0  (disable UVD clock gating)
  - EnableVceSwClockGating                  -> 0  (disable VCE SW clock gating)
  - KMD_EnableContextBasedPowerManagement   -> 0  (disable context-based PM)
  - EnableAspmL0s                           -> 0  (disable PCIe ASPM L0s)
  - EnableAspmL1                            -> 0  (disable PCIe ASPM L1)
  - PP_ULPSDelayIntervalInMilliSeconds      -> 0  (zero ULPS delay)
  - DisableVCEPowerGating                   -> 1  (disable VCE power gating)

Already-good values are verified and reported but not changed:
  - PP_SclkDeepSleepDisable                 == 1
  - PP_DisableVoltageIsland                 == 1
  - DisableSAMUPowerGating                  == 1
  - GCOOPTION_DisableGPIOPowerSaveMode      == 1

Usage (standalone):
  py reg_patch.py                # Show current values (dry-run)
  py reg_patch.py --apply        # Apply anti-gating patches
  py reg_patch.py --restore      # Restore from saved backup
  py reg_patch.py --adapter 0001 # Target a specific adapter subkey

Usage (as module):
  from reg_patch import RegistryPatch
  rp = RegistryPatch()                 # auto-detect AMD adapter
  report = rp.read_current()           # read current values
  rp.apply()                           # apply patches (saves backup)
  rp.restore()                         # restore from backup

Requires: Administrator privileges for write operations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import winreg
except ImportError:
    winreg = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"
_DISPLAY_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
)
AMD_VENDOR_ID = "VEN_1002"

_script_dir = os.path.dirname(os.path.abspath(__file__))
BACKUP_FILE = os.path.join(_script_dir, ".reg_backup.json")


# Values we patch: (name, target_value, description)
# target_value is what we SET to disable clock gating / power saving.
PATCH_VALUES: List[Tuple[str, int, str]] = [
    ("EnableUlps",                            0, "Ultra Low Power State"),
    ("PP_GPUPowerDownEnabled",                0, "GPU power-down gating"),
    ("EnableUvdClockGating",                  0, "UVD clock gating"),
    ("EnableVceSwClockGating",                0, "VCE software clock gating"),
    ("KMD_EnableContextBasedPowerManagement", 0, "Context-based power management"),
    ("EnableAspmL0s",                         0, "PCIe ASPM L0s link power saving"),
    ("EnableAspmL1",                          0, "PCIe ASPM L1 link power saving"),
    ("PP_ULPSDelayIntervalInMilliSeconds",    0, "ULPS delay interval"),
    ("DisableVCEPowerGating",                 1, "VCE power gating (1=disabled)"),
]

# Values we verify are already correct (name, expected_value, description).
VERIFY_VALUES: List[Tuple[str, int, str]] = [
    ("PP_SclkDeepSleepDisable",            1, "SCLK deep sleep disabled"),
    ("PP_DisableVoltageIsland",            1, "Voltage island disabled"),
    ("DisableSAMUPowerGating",             1, "SAMU power gating disabled"),
    ("GCOOPTION_DisableGPIOPowerSaveMode", 1, "GPIO power-save mode disabled"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_winreg():
    if winreg is None:
        raise RuntimeError("winreg module not available (Windows only)")


def _read_dword(key, name: str) -> Optional[int]:
    """Read a DWORD value, return None if missing."""
    try:
        value, vtype = winreg.QueryValueEx(key, name)
        if vtype in (winreg.REG_DWORD, winreg.REG_DWORD_LITTLE_ENDIAN):
            return int(value)
        # Some values may be stored as REG_SZ "0" or "1"
        if vtype == winreg.REG_SZ and isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
        return None
    except OSError:
        return None


def _write_dword(key, name: str, value: int) -> None:
    """Write a DWORD value."""
    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Adapter detection
# ---------------------------------------------------------------------------

def find_amd_adapter_keys() -> List[str]:
    """
    Find all AMD GPU adapter subkeys under the Display Class registry path.

    Returns:
        List of full registry key paths (relative to HKLM) for AMD adapters,
        e.g. ["SYSTEM\\...\\{guid}\\0000"].
    """
    _require_winreg()
    results = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_PATH, 0, winreg.KEY_READ
        ) as parent:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(parent, i)
                except OSError:
                    break
                i += 1

                # Skip non-numeric subkeys like "Configuration", "Properties"
                if not subkey_name.isdigit():
                    continue

                sub_path = _DISPLAY_CLASS_PATH + "\\" + subkey_name
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, sub_path, 0, winreg.KEY_READ
                    ) as k:
                        try:
                            mdid, _ = winreg.QueryValueEx(k, "MatchingDeviceId")
                            if isinstance(mdid, str) and AMD_VENDOR_ID in mdid.upper():
                                results.append(sub_path)
                        except OSError:
                            pass
                except OSError:
                    continue
    except OSError as e:
        raise RuntimeError(f"Cannot enumerate display adapters: {e}")

    return results


def _get_adapter_info(key_path: str) -> Dict[str, str]:
    """Read basic identification for an adapter key."""
    _require_winreg()
    info: Dict[str, str] = {"key_path": key_path}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ
        ) as k:
            for name in ("DriverDesc", "MatchingDeviceId", "DriverVersion"):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    if isinstance(v, str):
                        info[name] = v
                except OSError:
                    pass
    except OSError:
        pass
    return info


# ---------------------------------------------------------------------------
# RegistryPatch class
# ---------------------------------------------------------------------------

class RegistryPatch:
    """
    Read, patch, and restore AMD GPU driver registry values that control
    clock gating and power saving behavior.

    Args:
        adapter_key: Full registry path to the adapter subkey (relative to HKLM).
                     If None, auto-detects the first AMD adapter.
    """

    def __init__(self, adapter_key: Optional[str] = None):
        _require_winreg()

        if adapter_key is None:
            keys = find_amd_adapter_keys()
            if not keys:
                raise RuntimeError(
                    "No AMD GPU adapter found in the Display Class registry.\n"
                    "Check Device Manager for the AMD Radeon adapter."
                )
            adapter_key = keys[0]
            if len(keys) > 1:
                print(f"[reg_patch] Multiple AMD adapters found; using first: {keys[0]}")

        self.key_path = adapter_key
        self.info = _get_adapter_info(adapter_key)

    # ---- Read ----

    def read_current(self) -> Dict[str, Dict[str, Any]]:
        """
        Read all patch-relevant values from the registry.

        Returns:
            Dict with two sub-dicts:
              "patch":  {name: {"current": val, "target": val, "desc": str}, ...}
              "verify": {name: {"current": val, "expected": val, "desc": str}, ...}
        """
        result: Dict[str, Dict[str, Any]] = {"patch": {}, "verify": {}}

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, self.key_path, 0, winreg.KEY_READ
        ) as k:
            for name, target, desc in PATCH_VALUES:
                current = _read_dword(k, name)
                result["patch"][name] = {
                    "current": current,
                    "target": target,
                    "desc": desc,
                }

            for name, expected, desc in VERIFY_VALUES:
                current = _read_dword(k, name)
                result["verify"][name] = {
                    "current": current,
                    "expected": expected,
                    "desc": desc,
                }

        return result

    # ---- Backup / Restore ----

    def _save_backup(self, original_values: Dict[str, Optional[int]]) -> None:
        """Persist original values to a JSON backup file."""
        payload = {
            "schema": 1,
            "saved_at_unix": int(time.time()),
            "key_path": self.key_path,
            "adapter": self.info.get("DriverDesc", "unknown"),
            "values": {
                name: val for name, val in original_values.items()
            },
        }
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def _load_backup(self) -> Dict[str, Optional[int]]:
        """Load original values from the backup file."""
        if not os.path.isfile(BACKUP_FILE):
            raise FileNotFoundError(
                f"No backup file found at {BACKUP_FILE}\n"
                "Run --apply first to create a backup before restoring."
            )
        with open(BACKUP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("values", {})
        return {str(k): v for k, v in values.items()}

    # ---- Apply ----

    def apply(self, dry_run: bool = False) -> List[Tuple[str, Optional[int], int]]:
        """
        Apply anti-clock-gating patches to the registry.

        Saves a backup of original values before making changes.

        Args:
            dry_run: If True, only report what would change without writing.

        Returns:
            List of (name, old_value, new_value) for each changed entry.
        """
        # Read originals first (for backup)
        originals: Dict[str, Optional[int]] = {}
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, self.key_path, 0, winreg.KEY_READ
        ) as k:
            for name, target, desc in PATCH_VALUES:
                originals[name] = _read_dword(k, name)

        if not dry_run:
            self._save_backup(originals)

        changes: List[Tuple[str, Optional[int], int]] = []

        if dry_run:
            for name, target, desc in PATCH_VALUES:
                old = originals[name]
                if old != target:
                    changes.append((name, old, target))
            return changes

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, self.key_path, 0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            for name, target, desc in PATCH_VALUES:
                old = originals[name]
                if old == target:
                    continue
                _write_dword(k, name, target)
                # Verify write
                verify = _read_dword(k, name)
                changes.append((name, old, verify if verify is not None else target))

        return changes

    # ---- Restore ----

    def restore(self) -> List[Tuple[str, Optional[int], Optional[int]]]:
        """
        Restore registry values from the backup file.

        Returns:
            List of (name, current_value, restored_value) for each entry.
        """
        backup = self._load_backup()
        restored: List[Tuple[str, Optional[int], Optional[int]]] = []

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, self.key_path, 0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            for name, original in backup.items():
                current = _read_dword(k, name)
                if original is None:
                    # Value didn't exist before; skip (don't delete)
                    restored.append((name, current, None))
                    continue
                if current == original:
                    restored.append((name, current, original))
                    continue
                _write_dword(k, name, original)
                verify = _read_dword(k, name)
                restored.append((name, current, verify))

        return restored


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(rp: RegistryPatch, report: Dict[str, Dict[str, Any]]) -> None:
    """Pretty-print the current state report."""
    info = rp.info

    print(f"\n{'='*66}")
    print(f"  AMD GPU Registry -- Anti-Clock-Gating Status")
    print(f"{'='*66}")
    print(f"  Adapter:  {info.get('DriverDesc', '?')}")
    print(f"  Device:   {info.get('MatchingDeviceId', '?')}")
    print(f"  Driver:   {info.get('DriverVersion', '?')}")
    print(f"  Key:      {rp.key_path}")

    # Patch targets
    print(f"\n  --- Values to patch (clock gating / power saving) ---")
    print(f"  {'Name':46s} {'Current':>8s}  {'Target':>8s}  Status")
    print(f"  {'-'*46} {'-'*8}  {'-'*8}  {'-'*12}")
    for name, entry in report["patch"].items():
        current = entry["current"]
        target = entry["target"]
        cur_str = str(current) if current is not None else "(missing)"
        tgt_str = str(target)
        if current == target:
            status = "OK"
        elif current is None:
            status = "MISSING"
        else:
            status = "NEEDS PATCH"
        print(f"  {name:46s} {cur_str:>8s}  {tgt_str:>8s}  {status}")

    # Verify targets
    print(f"\n  --- Already-correct values (should be set) ---")
    print(f"  {'Name':46s} {'Current':>8s}  {'Expected':>8s}  Status")
    print(f"  {'-'*46} {'-'*8}  {'-'*8}  {'-'*12}")
    for name, entry in report["verify"].items():
        current = entry["current"]
        expected = entry["expected"]
        cur_str = str(current) if current is not None else "(missing)"
        exp_str = str(expected)
        if current == expected:
            status = "OK"
        elif current is None:
            status = "MISSING"
        else:
            status = "WRONG"
        print(f"  {name:46s} {cur_str:>8s}  {exp_str:>8s}  {status}")

    # Count needed
    needs_patch = sum(
        1 for e in report["patch"].values() if e["current"] != e["target"]
    )
    wrong_verify = sum(
        1 for e in report["verify"].values() if e["current"] != e["expected"]
    )

    print(f"\n  Summary: {needs_patch} value(s) need patching"
          f", {wrong_verify} verify value(s) unexpected")

    if needs_patch > 0:
        print(f"  Run with --apply to patch (requires admin).")
    else:
        print(f"  All anti-gating patches are already applied.")

    print(f"{'='*66}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AMD GPU registry anti-clock-gating patch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  py reg_patch.py                  # Show current values (dry-run)
  py reg_patch.py --apply          # Apply anti-gating patches
  py reg_patch.py --restore        # Restore original values from backup
  py reg_patch.py --adapter 0001   # Target a specific adapter subkey index
""",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply anti-gating patches to the registry (requires admin)",
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore original values from the backup file",
    )
    parser.add_argument(
        "--adapter", type=str, default=None,
        help="Adapter subkey index (e.g. '0000', '0001'). Auto-detects if omitted.",
    )
    args = parser.parse_args()

    # Build adapter key path
    adapter_key = None
    if args.adapter is not None:
        adapter_key = _DISPLAY_CLASS_PATH + "\\" + args.adapter

    try:
        rp = RegistryPatch(adapter_key=adapter_key)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.restore:
        print(f"\n  Restoring registry values from backup...")
        try:
            restored = rp.restore()
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        except PermissionError:
            print("ERROR: Access denied. Run as Administrator.", file=sys.stderr)
            return 1

        print(f"\n  {'Name':46s} {'Was':>8s}  {'Restored':>8s}")
        print(f"  {'-'*46} {'-'*8}  {'-'*8}")
        for name, was, now in restored:
            was_str = str(was) if was is not None else "(none)"
            now_str = str(now) if now is not None else "(skip)"
            print(f"  {name:46s} {was_str:>8s}  {now_str:>8s}")
        print(f"\n  Restored {len(restored)} value(s). Reboot for changes to take effect.")
        return 0

    # Read current state
    try:
        report = rp.read_current()
    except PermissionError:
        print("ERROR: Access denied reading registry. Run as Administrator.",
              file=sys.stderr)
        return 1

    _print_report(rp, report)

    if args.apply:
        print(f"\n  Applying patches...")
        try:
            changes = rp.apply(dry_run=False)
        except PermissionError:
            print("ERROR: Access denied. Run as Administrator.", file=sys.stderr)
            return 1

        if not changes:
            print("  No changes needed -- all values already at target.")
        else:
            print(f"\n  {'Name':46s} {'Old':>8s}  {'New':>8s}")
            print(f"  {'-'*46} {'-'*8}  {'-'*8}")
            for name, old, new in changes:
                old_str = str(old) if old is not None else "(none)"
                print(f"  {name:46s} {old_str:>8s}  {str(new):>8s}")
            print(f"\n  Applied {len(changes)} change(s). Backup saved to {BACKUP_FILE}")
            print(f"  Reboot for changes to take full effect.")
            print(f"  Use --restore to undo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
