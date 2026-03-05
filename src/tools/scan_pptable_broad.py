"""
scan_pptable_broad.py -- Broad PPTable RAM Scan (Diagnostic)
=============================================================

Scans physical memory for the driver's PP table cache using a BROAD
set of patterns.  Instead of searching only for the exact original
clock triple (1900/2780/3320), this generates patterns for every
plausible GameClock value (2000-3800 in steps of 20 MHz) anchored
to BaseClock=1900.

For each match, it reads the surrounding fields (GameClock, BoostClock,
MsgLimits) and reports what the driver actually has in memory -- even
if Adrenalin or PP_CNEscapeInput rewrote the clock values at boot.

Usage (admin):
  py scan_pptable_broad.py
  py scan_pptable_broad.py --max-gb 16
  py scan_pptable_broad.py --base-clock 1900
"""

import sys, os, struct, time

sys.stdout.reconfigure(line_buffering=True)
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.engine.overclock_engine import (
    init_hardware, cleanup_hardware,
    scan_memory, read_msglimits, read_clock_block,
    is_valid_pptable, read_pptable_at_addr,
    ScanOptions, CLOCK_PATTERN, POWER_PATTERN,
    ORIG_BASECLOCK_AC, ORIG_GAMECLOCK_AC, ORIG_BOOSTCLOCK_AC,
)


def generate_broad_patterns(base_clock=1900, gc_min=2000, gc_max=3800, gc_step=20):
    """Generate 4-byte patterns: BaseClock(u16) + GameClock(u16) for a range of GameClocks."""
    patterns = []
    seen = set()
    for gc in range(gc_min, gc_max + 1, gc_step):
        pat = struct.pack('<2H', base_clock, gc)
        if pat not in seen:
            seen.add(pat)
            patterns.append(pat)
    return patterns


def cli_progress(pct, msg):
    print(f"    {pct:5.1f}%  {msg}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Broad PPTable RAM scan (diagnostic)")
    ap.add_argument("--max-gb", type=int, default=32, help="Max GB to scan")
    ap.add_argument("--base-clock", type=int, default=0,
                    help="BaseClock anchor (0 = auto-detect from VBIOS)")
    ap.add_argument("--gc-min", type=int, default=2000, help="GameClock range min")
    ap.add_argument("--gc-max", type=int, default=3800, help="GameClock range max")
    ap.add_argument("--gc-step", type=int, default=20, help="GameClock range step")
    args = ap.parse_args()

    print("=" * 70)
    print("  Broad PPTable RAM Scan (Diagnostic)")
    print("=" * 70)

    vbios_vals = None
    try:
        from src.io.vbios_parser import parse_vbios_or_defaults
        vbios_vals = parse_vbios_or_defaults(verbose=True)
        print(f"  VBIOS: {vbios_vals.summary()}")
    except Exception as e:
        print(f"  VBIOS parse failed: {e}")

    if args.base_clock > 0:
        base_clock = args.base_clock
    elif vbios_vals:
        base_clock = vbios_vals.baseclock_ac
    else:
        base_clock = ORIG_BASECLOCK_AC

    vbios_game = vbios_vals.gameclock_ac if vbios_vals else ORIG_GAMECLOCK_AC
    vbios_boost = vbios_vals.boostclock_ac if vbios_vals else ORIG_BOOSTCLOCK_AC

    print(f"  BaseClock anchor:  {base_clock} MHz (from {'--base-clock' if args.base_clock > 0 else 'VBIOS'})")
    print(f"  GameClock range:   {args.gc_min}-{args.gc_max} step {args.gc_step}")
    print(f"  Scan range:        {args.max_gb} GB")

    exact_vbios = struct.pack('<3H', base_clock, vbios_game, vbios_boost)
    broad_patterns = generate_broad_patterns(
        base_clock, args.gc_min, args.gc_max, args.gc_step,
    )
    print(f"  Broad patterns:    {len(broad_patterns)} (4-byte BaseClock+GameClock)")
    print(f"  + exact VBIOS:     {base_clock}/{vbios_game}/{vbios_boost}")

    all_patterns = [exact_vbios] + broad_patterns

    print("\n  Initializing hardware...")
    try:
        hw = init_hardware()
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1

    try:
        inpout = hw["inpout"]

        print(f"\n  Phase 1: Full memory scan ({args.max_gb} GB)...")
        t0 = time.perf_counter()
        raw_matches = scan_memory(
            inpout, all_patterns, args.max_gb,
            num_threads=0,
            progress_callback=cli_progress,
        )
        elapsed = time.perf_counter() - t0

        if not raw_matches:
            print(f"\n  No matches found in {args.max_gb} GB scan.")
            return 1

        print(f"\n  Raw matches: {len(raw_matches)} (in {elapsed:.1f}s)")

        print(f"\n  Phase 2: Reading clock + MsgLimits at each match...")
        print()
        print(f"  {'Addr':>14s}  {'Base':>5s}  {'Game':>5s}  {'Boost':>5s}  "
              f"{'PPT':>4s}  {'TDC':>4s}  {'TDC_S':>5s}  "
              f"{'Edge':>4s}  {'Hot':>4s}  {'Mem':>4s}  {'VR':>4s}  "
              f"{'Valid':>5s}  Pattern")
        print(f"  {'─'*14}  {'─'*5}  {'─'*5}  {'─'*5}  "
              f"{'─'*4}  {'─'*4}  {'─'*5}  "
              f"{'─'*4}  {'─'*4}  {'─'*4}  {'─'*4}  "
              f"{'─'*5}  {'─'*20}")

        valid_results = []
        rejected_results = []

        for addr, pat in raw_matches:
            try:
                pp = read_pptable_at_addr(inpout, addr)
            except Exception:
                pp = None
            if pp is None:
                continue

            base_c = pp['baseclock_ac']
            game_c = pp['gameclock_ac']
            boost_c = pp['boostclock_ac']

            if base_c != base_clock:
                continue

            if not (500 <= game_c <= 5000) or not (500 <= boost_c <= 5000):
                continue

            valid, reasons = is_valid_pptable(pp)

            is_vbios = (pat == exact_vbios)
            pat_label = "EXACT_VBIOS" if is_vbios else f"broad(gc={struct.unpack_from('<H', pat, 2)[0]})"

            entry = {
                'addr': addr,
                'base': base_c,
                'game': game_c,
                'boost': boost_c,
                'ppt': pp['ppt0_ac'],
                'tdc': pp['tdc_gfx'],
                'tdc_soc': pp['tdc_soc'],
                'edge': pp['temp_edge'],
                'hot': pp['temp_hotspot'],
                'mem': pp['temp_mem'],
                'vr': pp['temp_vr_gfx'],
                'valid': valid,
                'reasons': reasons,
                'pat_label': pat_label,
                'full': pp,
            }

            tag = "  OK" if valid else "FAIL"

            print(f"  0x{addr:012X}  {base_c:5d}  {game_c:5d}  {boost_c:5d}  "
                  f"{pp['ppt0_ac']:4d}  {pp['tdc_gfx']:4d}  {pp['tdc_soc']:5d}  "
                  f"{pp['temp_edge']:4d}  {pp['temp_hotspot']:4d}  {pp['temp_mem']:4d}  {pp['temp_vr_gfx']:4d}  "
                  f"{tag:>5s}  {pat_label}")

            if valid:
                valid_results.append(entry)
            else:
                rejected_results.append(entry)

        # Summary
        print(f"\n{'='*70}")
        print(f"  Summary")
        print(f"{'='*70}")
        print(f"  Total raw matches:     {len(raw_matches)}")
        print(f"  Valid PP table copies:  {len(valid_results)}")
        print(f"  Rejected (bad limits):  {len(rejected_results)}")

        if valid_results:
            game_values = sorted(set(r['game'] for r in valid_results))
            boost_values = sorted(set(r['boost'] for r in valid_results))
            ppt_values = sorted(set(r['ppt'] for r in valid_results))
            tdc_values = sorted(set(r['tdc'] for r in valid_results))

            print(f"\n  Unique GameClock values found:  {game_values}")
            print(f"  Unique BoostClock values found: {boost_values}")
            print(f"  Unique PPT values found:        {ppt_values}")
            print(f"  Unique TDC_GFX values found:    {tdc_values}")

            has_vbios = any(r['game'] == vbios_game and r['boost'] == vbios_boost
                           for r in valid_results)
            has_3500 = any(r['game'] >= 3500 or r['boost'] >= 3500
                          for r in valid_results)
            has_modified = any(r['game'] != vbios_game or r['boost'] != vbios_boost
                              for r in valid_results)

            print()
            if has_vbios:
                print(f"  *** VBIOS pattern ({base_clock}/{vbios_game}/{vbios_boost}) IS present in RAM")
            else:
                print(f"  *** VBIOS pattern ({base_clock}/{vbios_game}/{vbios_boost}) NOT found in RAM")

            if has_3500:
                print(f"  *** 3500+ MHz values ARE present in RAM")
            else:
                print(f"  *** 3500+ MHz values NOT found in RAM")

            if has_modified:
                modified = [r for r in valid_results
                            if r['game'] != vbios_game or r['boost'] != vbios_boost]
                print(f"  *** {len(modified)} MODIFIED copy(ies) found (clocks differ from VBIOS)")
                for r in modified:
                    print(f"      0x{r['addr']:012X}: {r['base']}/{r['game']}/{r['boost']}")
            else:
                print(f"  *** All valid copies match VBIOS clocks (no modifications detected)")

            # Detailed dump of each valid copy
            print(f"\n{'='*70}")
            print(f"  Detailed Valid PP Table Copies")
            print(f"{'='*70}")
            for i, r in enumerate(valid_results):
                pp = r['full']
                print(f"\n  Copy #{i+1} at 0x{r['addr']:012X}  [{r['pat_label']}]")
                print(f"    Clocks:   Base={r['base']}  Game={r['game']}  Boost={r['boost']} MHz")
                print(f"    Power:    PPT0_AC={pp['ppt0_ac']}W  PPT0_DC={pp['ppt0_dc']}W  "
                      f"PPT1_AC={pp['ppt1_ac']}W  PPT1_DC={pp['ppt1_dc']}W")
                print(f"    TDC:      GFX={pp['tdc_gfx']}A  SOC={pp['tdc_soc']}A")
                print(f"    Temps:    Edge={pp['temp_edge']}  Hotspot={pp['temp_hotspot']}  "
                      f"HsGfx={pp['temp_hsgfx']}  HsSoc={pp['temp_hssoc']}  "
                      f"Mem={pp['temp_mem']}  VR_GFX={pp['temp_vr_gfx']}  VR_SOC={pp['temp_vr_soc']}")

        if rejected_results:
            print(f"\n{'='*70}")
            print(f"  Rejected Matches (first 10)")
            print(f"{'='*70}")
            for r in rejected_results[:10]:
                print(f"  0x{r['addr']:012X}  {r['base']}/{r['game']}/{r['boost']}  "
                      f"PPT={r['ppt']}  TDC={r['tdc']}  -> {r['reasons']}")

        print(f"\n{'='*70}")
        return 0

    finally:
        cleanup_hardware(hw)


if __name__ == "__main__":
    sys.exit(main())
