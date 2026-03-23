"""
Adrenalift -- Kernel PPTable Patch + OD Apply (CLI)
=========================================================

CLI frontend for the overclock engine. Scans physical memory for
the AMD driver's cached PPTable, patches clock/power/TDC limits,
then applies OD table settings via SMU.

What it patches (in kernel memory):
  - GameClockAc / BoostClockAc  -> removes DPM frequency cap
  - MsgLimits.Power (PPT)       -> raises power limit ceiling
  - MsgLimits.Tdc (GFX current) -> raises current limit
  - Also applies OD table: PPT%, GfxclkFoffset, SetSoftMax

Usage:
  py overclock.py                     # Apply with defaults
  py overclock.py --clock 3500        # Custom clock target
  py overclock.py --power 250         # Custom power limit
  py overclock.py --tdc 200           # Custom TDC (amps)
  py overclock.py --offset 300        # GfxclkFoffset in MHz
  py overclock.py --scan-only         # Just scan and display, no patch
  py overclock.py --min-clock 2500    # Set minimum GFX clock floor
  py overclock.py --min-clock 2500 --watch  # Floor + persistent watchdog
  py overclock.py --list-clocks       # Show available DPM frequency ranges

Safe: Non-persistent. Reboot always restores stock values.

Reliability improvements:
  - Validates each match: rejects false positives with garbage PPT/TDC/temp
  - Re-applies OD twice (before and after kernel patch) to handle driver
    re-reading from its cache
  - Verifies patches stick after a short delay
"""

import sys, os, time, argparse

sys.stdout.reconfigure(line_buffering=True)
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.engine.overclock_engine import (
    OverclockSettings, ScanOptions, ScanResult, ODScanResult,
    init_hardware, cleanup_hardware,
    scan_for_pptable, scan_for_od_table, patch_pptable, apply_od_settings,
    verify_patches, extract_od_pattern, read_od, validate_od_candidate,
    get_gpu_state, get_dpm_ranges, read_metrics, watchdog_step,
    ORIG_BASECLOCK_AC, ORIG_GAMECLOCK_AC, ORIG_BOOSTCLOCK_AC,
    ORIG_POWER_AC, ORIG_TDC_GFX,
)
from src.engine.od_table import dump_od_table


# ---------------------------------------------------------------------------
# CLI progress callback
# ---------------------------------------------------------------------------

def cli_progress(pct, msg):
    """Print scan/patch progress for CLI output."""
    print(f"    {pct:5.1f}%  {msg}")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_scan_details(scan_result):
    """Print per-match validation details from a ScanResult."""
    ap_count = sum(1 for m in scan_result.match_details
                   if m['already_patched'])
    total = len(scan_result.all_clock_addrs)
    fresh = total - ap_count

    print(f"\n  Found {total} pattern match(es)"
          f" ({fresh} original, {ap_count} already patched).")
    print(f"  Validating MsgLimits for each match...")

    for i, m in enumerate(scan_result.match_details):
        addr = m['addr']
        tag = " [already patched]" if m['already_patched'] else ""
        status = "VALID" if m['valid'] else "REJECTED (false positive)"
        ml = m['msglimits']

        print(f"\n  Match #{i+1} at 0x{addr:012X}{tag}  [{status}]:")
        print(f"    Clocks:  Base={ORIG_BASECLOCK_AC} "
              f"Game={m['game_clock']} Boost={m['boost_clock']} MHz")
        print(f"    PPT:     {ml['ppt0_ac']}W (AC) / {ml['ppt0_dc']}W (DC)")
        print(f"    TDC:     GFX={ml['tdc_gfx']}A  SOC={ml['tdc_soc']}A")
        print(f"    Temps:   Edge={ml['temp_edge']}C  "
              f"Hotspot={ml['temp_hotspot']}C  VR={ml['temp_vr_gfx']}C")
        if not m['valid']:
            for r in m['reject_reasons']:
                print(f"    -> {r}")

    print(f"\n  Result: {len(scan_result.valid_addrs)} valid PPTable(s), "
          f"{len(scan_result.rejected_addrs)} false positive(s) rejected")


def print_patch_reports(reports):
    """Print patch results from patch_pptable()."""
    for r in reports:
        if 'extra_power' in r:
            extra = r['extra_power']
            if extra:
                print(f"\n  Found {len(extra)} additional power limit(s):")
                for ep in extra:
                    ok = "OK" if ep['ok'] else "FAIL"
                    print(f"    0x{ep['addr']:012X}: "
                          f"{ep['old']} -> {ep['new']} W  [{ok}]")
            else:
                print(f"\n  No additional copies")
            continue

        label = "  [refreshing]" if r['refreshing'] else ""
        print(f"\n  Patching at 0x{r['addr']:012X}{label}:")
        for p in r['patches']:
            ok = "OK" if p['ok'] else "FAIL"
            unit = p.get('unit', '')
            changed = "" if p['old'] == p['new'] else f" (was {p['old']})"
            print(f"    {p['field']:14s} {p['new']} {unit}{changed}  [{ok}]")


def print_od_results(results, settings):
    """Print OD/SMU command results."""
    effective_max = settings.effective_max
    min_clock = settings.effective_min_clock

    r = results.get('od_commit')
    if r is not None:
        print(f"  OD: PPT=+{settings.od_ppt}%, offset=+{settings.offset}MHz"
              f"  resp={r} {'OK' if r == 1 else 'FAIL'}")

    r = results.get('soft_max')
    if r is not None:
        print(f"  SetSoftMax({effective_max}): resp={r}")

    r = results.get('hard_max')
    if r is not None:
        print(f"  SetHardMax({effective_max}): resp={r}")

    r = results.get('soft_min')
    if r is not None:
        print(f"  SetSoftMin({min_clock}): resp={r}")

    r = results.get('hard_min')
    if r is not None:
        print(f"  SetHardMin({min_clock}): resp={r}")

    r = results.get('ppt_limit')
    if r is not None:
        print(f"  SetPptLimit({settings.power}W): resp={r}")

    r = results.get('disable_features')
    if r is not None:
        print(f"  Disable DS_GFXCLK+GFX_ULV+GFXOFF: resp={r}"
              f" {'OK' if r == 1 else 'FAIL'}")


def print_verify_results(all_ok, overwritten, details, settings):
    """Print verification results."""
    for i, d in enumerate(details):
        addr = d['addr']
        if d['ok']:
            print(f"  Copy #{i+1} at 0x{addr:012X}: OK "
                  f"(Game={d['game']} PPT={d['ppt']}W TDC={d['tdc']}A)")
        else:
            print(f"  Copy #{i+1} at 0x{addr:012X}: OVERWRITTEN!")
            print(f"    Game={d['game']} (want {settings.clock}), "
                  f"PPT={d['ppt']}W (want {settings.power}), "
                  f"TDC={d['tdc']}A (want {settings.tdc})")
            print(f"    -> Re-patching...")

    if overwritten > 0:
        print(f"\n  WARNING: {overwritten} copies were overwritten "
              f"by the driver!")
        print(f"  Re-patched them. Applying OD again to force refresh...")
    else:
        print(f"\n  All {len(details)} patches verified OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Adrenalift - Kernel PPTable Patch',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  py overclock.py                        # Defaults: clock=3500, power=250, tdc=200
  py overclock.py --clock 3600 --power 300 --tdc 250
  py overclock.py --offset 300           # Higher GfxclkFoffset
  py overclock.py --scan-only            # Just show current values
  py overclock.py --od-ppt 15            # OD PPT +15%
  py overclock.py --min-clock 2500       # Set 2500 MHz floor (one-shot)
  py overclock.py --min-clock 2500 --watch  # Floor + persistent watchdog
  py overclock.py --list-clocks          # Show DPM frequency ranges
""")
    parser.add_argument('--clock', type=int, default=3500,
                        help='Target GameClockAc/BoostClockAc MHz (default: 3500)')
    parser.add_argument('--power', type=int, default=250,
                        help='Target MsgLimits.Power watts (default: 250)')
    parser.add_argument('--tdc', type=int, default=200,
                        help='Target TDC_GFX amps (default: 200)')
    parser.add_argument('--tdc-soc', type=int, default=0,
                        help='Target TDC_SOC amps (default: 0=no change)')
    parser.add_argument('--offset', type=int, default=200,
                        help='GfxclkFoffset MHz for OD table (default: 200)')
    parser.add_argument('--od-ppt', type=int, default=10,
                        help='OD PPT percentage (default: 10)')
    parser.add_argument('--max-gb', type=int, default=32,
                        help='Max GB to scan (default: 32)')
    parser.add_argument('--threads', type=int, default=0,
                        help='Number of scan threads (default: 0=auto)')
    parser.add_argument('--fast-window-mb', type=int, default=512,
                        help='Fast pre-scan window size around cached '
                             'addresses (default: 512)')
    parser.add_argument('--scan-only', action='store_true',
                        help='Only scan and display, no modifications')
    parser.add_argument('--min-clock', type=int, default=0,
                        help='Minimum GFX clock floor MHz '
                             '(default: 0 = use --clock)')
    parser.add_argument('--lock-features', action='store_true',
                        help='Disable DS_GFXCLK/GFX_ULV/GFXOFF '
                             'to prevent idle downclocking')
    parser.add_argument('--watch', action='store_true',
                        help='After overclock, enter persistent '
                             'monitoring loop')
    parser.add_argument('--watch-interval', type=int, default=5,
                        help='Seconds between watchdog checks (default: 5)')
    parser.add_argument('--list-clocks', action='store_true',
                        help='Query and display DPM frequency ranges, '
                             'then exit')
    parser.add_argument('--dump-od', action='store_true',
                        help='Read OD table from SMU and dump to stdout, '
                             'then exit')
    parser.add_argument('--scan-od', action='store_true',
                        help='Scan RAM for OD table using SMU-extracted '
                             'pattern; requires --dump-od path or prior PPTable scan')
    args = parser.parse_args()

    settings = OverclockSettings(
        clock=args.clock,
        power=args.power,
        tdc=args.tdc,
        tdc_soc=args.tdc_soc,
        offset=args.offset,
        od_ppt=args.od_ppt,
        min_clock=args.min_clock,
        lock_features=args.lock_features or args.min_clock > 0,
    )

    scan_opts = ScanOptions(
        max_gb=args.max_gb,
        num_threads=args.threads,
        fast_window_mb=args.fast_window_mb,
    )

    print("=" * 62)
    print("  Adrenalift -- Kernel PPTable Patch + OD Apply")
    print("=" * 62)

    if not args.scan_only and not args.list_clocks:
        print(f"  Clock cap:   {ORIG_GAMECLOCK_AC}/{ORIG_BOOSTCLOCK_AC}"
              f" -> {args.clock} MHz")
        print(f"  Power (PPT): {ORIG_POWER_AC} -> {args.power} W")
        print(f"  TDC (GFX):   {ORIG_TDC_GFX} -> {args.tdc} A")
        print(f"  OD offset:   +{args.offset} MHz")
        print(f"  OD PPT:      +{args.od_ppt}%")
        if args.min_clock > 0:
            print(f"  Min clock:   {settings.effective_min_clock} MHz (floor)")
            print(f"  Lock feat:   DS_GFXCLK + GFX_ULV + GFXOFF disabled")
        if args.watch:
            print(f"  Watchdog:    every {args.watch_interval}s")

    # Initialize hardware
    print(f"\n  Initializing hardware...")
    hw = init_hardware()

    try:
        smu = hw['smu']
        virt = hw['virt']
        inpout = hw['inpout']

        # --list-clocks: query DPM frequency ranges and exit
        if args.list_clocks:
            print(f"\n{'='*62}")
            print(f"  DPM Frequency Ranges")
            print(f"{'='*62}")
            for entry in get_dpm_ranges(smu):
                if 'error' in entry:
                    print(f"  {entry['name']:10s}  FAILED ({entry['error']})")
                else:
                    print(f"  {entry['name']:10s}  min={entry['min']:5d}"
                          f"  max={entry['max']:5d} MHz")
            print(f"\n  Use --min-clock <MHz> to set a GFX clock floor.")
            print(f"{'='*62}")
            return

        # --dump-od: read OD table from SMU and dump, then exit
        if args.dump_od:
            print(f"\n{'='*62}")
            print(f"  OD Table (from SMU TransferTableSmu2Dram)")
            print(f"{'='*62}")
            od = read_od(smu, virt)
            if od is None:
                print("  ERROR: Failed to read OD table from SMU.")
                return
            dump_od_table(od)
            pattern = extract_od_pattern(smu, virt, 24)
            if pattern:
                print(f"\n  First 24 bytes (hex) for RAM search:")
                print("    " + pattern.hex(" "))
            return

        # --scan-od: scan RAM for OD table using SMU-extracted pattern
        if args.scan_od:
            print(f"\n{'='*62}")
            print(f"  OD Table RAM Scan")
            print(f"{'='*62}")
            pattern = extract_od_pattern(smu, virt, 24)
            if not pattern:
                print("  ERROR: Could not read OD table from SMU.")
                return
            print(f"  Pattern ({len(pattern)} bytes): {pattern.hex(' ')}")
            od_result = scan_for_od_table(
                inpout, pattern,
                scan_opts=scan_opts,
                progress_callback=cli_progress,
            )
            if od_result.error and not od_result.valid_addrs:
                print(f"\n  {od_result.error}")
                return
            print(f"\n  Found {len(od_result.valid_addrs)} valid OD table(s) in RAM:")
            for i, (addr, od) in enumerate(zip(od_result.valid_addrs,
                                                od_result.valid_tables)):
                print(f"\n  Copy #{i+1} at 0x{addr:012X}:")
                print(f"    GfxclkFoffset={od.GfxclkFoffset} MHz  "
                      f"Ppt={od.Ppt}%  Tdc={od.Tdc}%")
                print(f"    Uclk {od.UclkFmin}-{od.UclkFmax} MHz  "
                      f"Fclk {od.FclkFmin}-{od.FclkFmax} MHz")
            return

        # --- Phase 1: Show current state ---
        state = get_gpu_state(smu, virt)

        print(f"\n  Current GPU State:")
        print(f"    DPM GFXCLK:  min={state['fmin']} "
              f"max={state['fmax']} MHz")
        print(f"    PPT Limit:   {state['ppt_limit']} W")
        print(f"    GFXCLK:      {state['gfxclk']}/{state['gfxclk2']} MHz")

        # --- Phase 2: Scan for PPTable in kernel memory ---
        print(f"\n{'='*62}")
        print(f"  Phase 1: Scanning kernel memory for PPTable cache...")
        print(f"{'='*62}")

        scan_result = scan_for_pptable(inpout, settings, scan_opts,
                                       cli_progress)

        if scan_result.error:
            print(f"\n  ERROR: {scan_result.error}")
            if not scan_result.valid_addrs:
                print("  Make sure AMD driver is loaded (check Device Manager)")
                return

        print_scan_details(scan_result)

        if args.scan_only:
            print(f"\n  (scan-only mode, no modifications)")
            return

        # --- Phase 3: Patch kernel memory ---
        print(f"\n{'='*62}")
        print(f"  Phase 2: Patching {len(scan_result.valid_addrs)} "
              f"valid PPTable(s)...")
        print(f"{'='*62}")

        reports = patch_pptable(inpout, scan_result, settings, scan_opts,
                                cli_progress)
        print_patch_reports(reports)

        # --- Phase 4: Apply OD table via SMU ---
        print(f"\n{'='*62}")
        print(f"  Phase 3: Applying OD table settings via SMU...")
        print(f"{'='*62}")

        od_results = apply_od_settings(smu, virt, settings)
        print_od_results(od_results, settings)

        # --- Phase 5: Verify patches survived ---
        print(f"\n{'='*62}")
        print(f"  Phase 4: Verifying patches survived (2s delay)...")
        print(f"{'='*62}")

        time.sleep(2.0)
        all_ok, overwritten, details = verify_patches(inpout, scan_result,
                                                      settings)
        print_verify_results(all_ok, overwritten, details, settings)

        if not all_ok:
            od_results2 = apply_od_settings(smu, virt, settings)
            print_od_results(od_results2, settings)

        # --- Phase 6: Final results ---
        print(f"\n{'='*62}")
        print(f"  Results")
        print(f"{'='*62}")

        state = get_gpu_state(smu, virt)

        print(f"  DPM GFXCLK:  min={state['fmin']} "
              f"max={state['fmax']} MHz", end="")
        if state['fmax'] > 3500:
            print(f"  [+{state['fmax'] - 3500} MHz above stock!]")
        else:
            print()
        print(f"  PPT Limit:   {state['ppt_limit']} W")

        # Quick monitor
        print(f"\n  Quick monitor (5 seconds):")
        peak = 0
        for i in range(10):
            time.sleep(0.5)
            gfxclk, gfxclk2, metrics_ppt, temp = read_metrics(smu, virt)
            peak = max(peak, gfxclk)
            print(f"    +{(i+1)*0.5:4.1f}s: GFXCLK={gfxclk:4d}/{gfxclk2:4d}"
                  f"  PPT={metrics_ppt:3d}W  T={temp}C")

        print(f"\n  Peak GFXCLK (idle): {peak} MHz")
        print(f"\n{'='*62}")
        print(f"  Overclock applied! Run a GPU benchmark to verify.")
        print(f"  Expected max clock: ~{settings.effective_max} MHz")
        if args.min_clock > 0:
            print(f"  Min clock floor:    {settings.effective_min_clock} MHz")
        print(f"  This patch resets on reboot - run this script again.")
        print(f"{'='*62}")

        # --- Phase 7: Watchdog loop (opt-in) ---
        if args.watch:
            print(f"\n{'='*62}")
            print(f"  Phase 5: Watchdog (Ctrl+C to exit)")
            print(f"  Floor={settings.effective_min_clock} MHz"
                  f"  Interval={args.watch_interval}s")
            print(f"{'='*62}")

            iteration = 0
            try:
                while True:
                    time.sleep(args.watch_interval)
                    iteration += 1
                    elapsed = iteration * args.watch_interval

                    wd = watchdog_step(smu, virt, settings, iteration)

                    mins = elapsed // 60
                    secs = elapsed % 60
                    ts = f"{mins:02d}:{secs:02d}"
                    print(f"  [{ts}] GFXCLK={wd['gfxclk']:4d}"
                          f"/{wd['gfxclk2']:4d}  "
                          f"Floor={wd['min_clock']}  "
                          f"PPT={wd['ppt']:3d}W  "
                          f"T={wd['temp']}C  {wd['action']}")

            except KeyboardInterrupt:
                print(f"\n\n  Watchdog stopped by user (Ctrl+C)")
                print(f"  Min clock floor remains set until reboot.")

    finally:
        cleanup_hardware(hw)


if __name__ == '__main__':
    main()
