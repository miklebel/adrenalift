"""
CPU Power Plan Optimizer for Benchmarking
==========================================

Toggles Windows power plan settings between benchmark-optimized
and normal profiles. Companion to overclock.py for GPU tuning.

Usage:
  py cpu_power.py              # Show current CPU power settings
  py cpu_power.py --bench      # Apply benchmark profile
  py cpu_power.py --revert     # Revert to normal profile

Benchmark profile (--bench):
  - PERFEPP           -> 0%   (most aggressive boost hint to CPPC)
  - Idle disable      -> On   (prevent C-state drops between work)
  - USB sel. suspend  -> Off  (eliminate USB latency spikes)
  - USB 3 link PM     -> Off  (same)
  - Throttle states   -> Off  (disable OS-level throttling)

Normal profile (--revert):
  - PERFEPP           -> 0%   (keep max performance EPP)
  - Idle disable      -> Off  (restore normal idle, saves ~20W)
  - USB sel. suspend  -> On   (default)
  - USB 3 link PM     -> Moderate (default)
  - Throttle states   -> Automatic (default)

All changes target the active power scheme (AC only). Non-persistent
across power plan switches in Control Panel -- re-run if needed.
"""

import subprocess
import sys
import argparse

sys.stdout.reconfigure(line_buffering=True, errors="replace")

# ---------------------------------------------------------------------------
# Power setting definitions:  (subgroup_guid, setting_guid, alias, friendly)
# ---------------------------------------------------------------------------

SETTINGS = {
    "perfepp": {
        "sub":   "SUB_PROCESSOR",
        "guid":  "36687f9e-e3a5-4dbf-b1dc-15eb381c6863",
        "alias": "PERFEPP",
        "name":  "Energy Performance Preference",
        "unit":  "%",
        "bench": 0,
        "revert": 0,   # keep at 0 -- no reason to give up free MHz
        "values": {0: "0% (max performance)", 10: "10% (balanced perf)",
                   50: "50% (balanced)", 100: "100% (max efficiency)"},
    },
    "idle_disable": {
        "sub":   "SUB_PROCESSOR",
        "guid":  "5d76a2ca-e8c0-402f-a133-2158492d58ad",
        "alias": "IDLEDISABLE",
        "name":  "Processor Idle Disable",
        "unit":  "",
        "bench": 1,
        "revert": 0,
        "values": {0: "Enable idle (normal)", 1: "Disable idle (bench)"},
    },
    "throttle": {
        "sub":   "SUB_PROCESSOR",
        "guid":  "3b04d4fd-1cc7-4f23-ab1c-d1337819c4bb",
        "alias": "THROTTLING",
        "name":  "Allow Throttle States",
        "unit":  "",
        "bench": 0,
        "revert": 2,
        "values": {0: "Off", 1: "On", 2: "Automatic"},
    },
    "usb_suspend": {
        "sub":   "2a737441-1930-4402-8d77-b2bebba308a3",
        "guid":  "48e6b7a6-50f5-4782-a5d4-53bb8f07e226",
        "alias": "USB_SUSPEND",
        "name":  "USB Selective Suspend",
        "unit":  "",
        "bench": 0,
        "revert": 1,
        "values": {0: "Disabled", 1: "Enabled"},
    },
    "usb3_link": {
        "sub":   "2a737441-1930-4402-8d77-b2bebba308a3",
        "guid":  "d4e98f31-5ffe-4ce1-be31-1b38b384c009",
        "alias": "USB3_LINK",
        "name":  "USB 3 Link Power Mgmt",
        "unit":  "",
        "bench": 0,
        "revert": 2,
        "values": {0: "Off", 1: "Minimum savings", 2: "Moderate savings",
                   3: "Maximum savings"},
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_powercfg(*args):
    """Run powercfg.exe and return stdout. Raises on failure."""
    cmd = ["powercfg"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"powercfg failed: {r.stderr.strip()}")
    return r.stdout


def get_active_scheme():
    """Return (guid, name) of the active power scheme."""
    out = run_powercfg("/getactivescheme")
    # "Power Scheme GUID: aaaa-bbbb  (Name)"
    parts = out.strip().split("GUID: ", 1)
    if len(parts) < 2:
        raise RuntimeError("Cannot parse active scheme")
    rest = parts[1]
    guid = rest.split()[0]
    name = rest.split("(", 1)[1].rstrip(") \n") if "(" in rest else "Unknown"
    return guid, name


def read_setting(key):
    """Read the current AC value for a setting. Returns int."""
    s = SETTINGS[key]
    out = run_powercfg("/qh", "SCHEME_CURRENT", s["sub"], s["guid"])
    for line in out.splitlines():
        if "Current AC Power Setting Index" in line:
            hex_val = line.split(":")[-1].strip()
            return int(hex_val, 16)
    raise RuntimeError(f"Cannot read {s['name']}")


def write_setting(key, value):
    """Write an AC value for a setting."""
    s = SETTINGS[key]
    run_powercfg("-setacvalueindex", "SCHEME_CURRENT", s["sub"], s["guid"],
                 str(value))


def apply_scheme():
    """Activate the modified scheme so changes take effect immediately."""
    run_powercfg("-setactive", "SCHEME_CURRENT")


def friendly_value(key, raw):
    """Return a human-readable label for a raw value."""
    s = SETTINGS[key]
    label = s["values"].get(raw)
    if label:
        return f"{raw} = {label}"
    unit = s["unit"]
    return f"{raw}{unit}" if unit else str(raw)

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def show_status():
    """Print all tracked settings with current values."""
    guid, name = get_active_scheme()
    print(f"\n  Active scheme: {name}")
    print(f"  GUID:          {guid}")
    print(f"  {'-' * 52}")
    print(f"  {'Setting':<30s} {'Current Value'}")
    print(f"  {'-' * 52}")
    for key in SETTINGS:
        s = SETTINGS[key]
        try:
            val = read_setting(key)
            display = friendly_value(key, val)
            # Mark if it matches bench or revert profile
            if val == s["bench"] and val != s["revert"]:
                tag = "  [bench]"
            elif val == s["revert"] and val != s["bench"]:
                tag = "  [normal]"
            else:
                tag = ""
            print(f"  {s['name']:<30s} {display}{tag}")
        except Exception as e:
            print(f"  {s['name']:<30s} ERROR: {e}")
    print(f"  {'-' * 52}")

# ---------------------------------------------------------------------------
# Apply / Revert
# ---------------------------------------------------------------------------

def apply_profile(profile_name):
    """Apply a named profile ('bench' or 'revert') to all settings."""
    guid, name = get_active_scheme()
    is_bench = profile_name == "bench"
    label = "BENCHMARK" if is_bench else "NORMAL"

    print(f"\n{'=' * 58}")
    print(f"  Applying {label} profile to: {name}")
    print(f"{'=' * 58}")

    changes = 0
    for key in SETTINGS:
        s = SETTINGS[key]
        target = s["bench"] if is_bench else s["revert"]
        try:
            current = read_setting(key)
        except Exception:
            current = None

        if current == target:
            print(f"  {s['name']:<30s}  {friendly_value(key, target):30s}  (no change)")
            continue

        write_setting(key, target)
        old_display = friendly_value(key, current) if current is not None else "?"
        new_display = friendly_value(key, target)
        print(f"  {s['name']:<30s}  {old_display}  ->  {new_display}")
        changes += 1

    apply_scheme()

    print(f"\n  Applied {changes} change(s). Scheme re-activated.")
    if is_bench:
        print(f"\n  TIP: Run 'py cpu_power.py --revert' after benchmarking")
        print(f"       to restore idle power savings (~20W less at desktop).")
    print(f"{'=' * 58}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CPU Power Plan Optimizer for Benchmarking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  py cpu_power.py              # Show current settings
  py cpu_power.py --bench      # Apply benchmark profile
  py cpu_power.py --revert     # Revert to normal profile
""")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bench", action="store_true",
                       help="Apply benchmark-optimized profile")
    group.add_argument("--revert", action="store_true",
                       help="Revert to normal profile")
    args = parser.parse_args()

    print("=" * 58)
    print("  CPU Power Plan Optimizer")
    print("=" * 58)

    if args.bench:
        apply_profile("bench")
    elif args.revert:
        apply_profile("revert")
    else:
        show_status()
        print(f"\n  Use --bench to optimize for benchmarks")
        print(f"  Use --revert to restore normal settings")


if __name__ == "__main__":
    main()
