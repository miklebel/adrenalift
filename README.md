# Adrenalift

**Unlock the real boost clock potential of your AMD GPU on Windows.**

Adrenalift is a Windows utility that bypasses artificial clock limits imposed by the AMD Windows display driver. It locates the driver's cached PowerPlay (PP) table in memory and patches the boost clock ceiling so your GPU can reach the frequencies it is actually capable of.

> **RDNA4** is the primary target. RDNA3 support is present in the code but has not been tested.

---

## The Problem

On Linux, users have full control over GPU clocks and power through the kernel's `pp_od_clk_voltage` sysfs interface. The open-source `amdgpu` driver exposes OverDrive knobs directly — you can raise the boost clock, adjust the power limit, and the hardware will comply up to its physical limits.

On Windows, the story is very different. The AMD display driver (`amdkmdag.sys`) enforces a **clock gating policy at the driver level**: even when the silicon can sustain higher frequencies, the driver's internal limits prevent the GPU from ever reaching them. The overclocking sliders exposed by the official software are constrained to a narrow range defined by the driver's cached copy of the PowerPlay table, not by the hardware itself. In practice this means your GPU may be leaving significant performance on the table — held back purely by software.

## How Adrenalift Works

1. **Scan** — the tool scans physical memory for the driver's cached PowerPlay table, locating the exact byte offsets that define the boost clock ceiling.
2. **Patch** — it writes new values directly into the driver's in-memory cache, raising (or lowering) the maximum allowed boost clock.
3. **Apply** — the patched limits take effect immediately. No reboot is required, and the changes are non-persistent: a reboot restores stock values.

Because the patch lives only in RAM, it is inherently safe to revert — just restart the machine.

> Other features (SMU OverDrive table, D3DKMTEscape path, registry tweaks, SPPT cache editing) are work-in-progress and may not work reliably for all configurations. They are documented within the application's UI.

---

## Warning

> **USE AT YOUR OWN RISK.**
>
> This tool writes directly to physical memory and communicates with the GPU's System Management Unit. Incorrect use can cause **driver crashes, blue screens (BSOD), display corruption, or — in extreme cases — hardware damage** from running outside manufacturer-validated operating parameters.
>
> - Overclocking may void your GPU warranty.
> - Always start with small increments above stock and test for stability.
> - The authors accept **no responsibility** for any damage to hardware or data.
> - **Administrator privileges are required.** The application's manifest requests elevation automatically.

---

## Requirements

- **Windows 10+** (64-bit)
- **AMD RDNA4 GPU** (RDNA3 untested)
- **Administrator privileges**
- A VBIOS dump (`bios/vbios.rom`) — the app will prompt you to supply one if not found

---

## Quick Start (pre-built)

1. Download the latest `Adrenalift_x.x_xx.exe` from releases.
2. Place your VBIOS ROM in the `bios/` folder next to the executable (or let the app prompt you).
3. Run the executable — it will request admin elevation.
4. Use the **Simple** tab to raise the boost clock and apply.

---

## Building from Source

### Prerequisites

- **Python 3.10+**
- **pip** (ships with Python)

### Steps

1. **Install Python dependencies:**

```bash
pip install -r requirements.txt
```

2. **Clone external dependencies:**

```bash
cd deps
git clone https://github.com/sibradzic/upp.git
```

After cloning, the directory layout should look like:

```
adrenalift/
├── deps/
│   └── upp/          ← cloned repo (git-ignored)
│       └── src/
│           └── upp/
├── src/
├── build.spec
└── ...
```

3. **Place driver binaries** in `drivers/`:
   - `inpoutx64.dll`
   - `WinRing0x64.dll`
   - `WinRing0x64.sys`
   - `WinRing0x64_patched.sys` (optional)

4. **Build:**

```powershell
.\build.ps1
```

Or manually:

```bash
python -m PyInstaller --noconfirm build.spec
```

The output `.exe` is written to `dist/`.

### UPP (Uplift Power Play)

UPP provides RDNA3/RDNA4 PowerPlay table decoding (`upp.decode`, `upp.atom_gen`).

**Repository:** https://github.com/sibradzic/upp.git

- **Runtime:** `src/io/vbios_parser.py` and `src/tools/sppt_cache.py` add `deps/upp/src` to `sys.path` so `from upp import decode` resolves locally.
- **Build (PyInstaller):** `build.spec` adds the same path to `pathex` and lists UPP sub-modules in `hiddenimports` so the bundled `.exe` includes everything.
- **Fallback:** If `deps/upp` is missing the app still runs, but VBIOS parsing will be unavailable. A warning is printed at build time and at runtime.

To update UPP:

```bash
cd deps/upp
git pull
```

---

## Project Structure

```
src/
├── app/                 # PySide6 GUI, settings, background workers
│   ├── main.py          # Entry point, main window, tab layout
│   ├── workers.py       # QThread workers (scan, apply, metrics, etc.)
│   ├── settings.py      # Persistent settings (settings.json)
│   └── ...
├── engine/              # Core overclock logic
│   ├── overclock_engine.py   # Scan, patch, apply, verify, watchdog
│   ├── od_table.py           # OverDrive table structures & controller
│   ├── smu.py                # SMU mailbox protocol & message IDs
│   └── smu_metrics.py        # GPU metrics parsing
├── io/                  # Hardware & OS interfaces
│   ├── mmio.py               # WinRing0 / InpOut physical memory & MMIO
│   ├── d3dkmt_escape.py      # D3DKMTEscape (WDDM) path
│   ├── vbios_parser.py       # VBIOS ROM parsing (stock values)
│   └── ...
└── tools/               # CLI tools & reverse-engineering utilities
    ├── overclock_cli.py       # Command-line interface
    ├── reg_patch.py           # Registry tweaks (ULPS, clock gating keys)
    ├── sppt_cache.py          # PP_PhmSoftPowerPlayTable builder
    └── ...                    # Frida scripts, Ghidra helpers, probes
```

---

## License

This project is licensed under the **GNU General Public License v3.0**. See [LICENSE](LICENSE) for details.
