# Driver Binaries

Adrenalift uses two third-party kernel drivers to access GPU hardware from user space. Neither driver is included in this repository — you must supply them yourself in the `drivers/` folder before building.

| File | Source | Purpose |
|------|--------|---------|
| `inpoutx64.dll` | [highrez.co.uk](https://www.highrez.co.uk/downloads/inpout32/) | Physical memory read/write, I/O port access |
| `WinRing0x64.dll` | [WinRing0 (open source)](https://github.com/QCute/WinRing0) | User-mode interface to WinRing0 kernel driver |
| `WinRing0x64.sys` | Same as above | Original WinRing0 kernel driver |
| `WinRing0x64_patched.sys` | Patched from original (see below) | WinRing0 with 1 MB physical memory restriction removed |

## How Adrenalift uses these drivers

### InpOut32 (`inpoutx64.dll`)

InpOut32 is a signed, self-installing driver that provides:

- **Physical memory writes** via `SetPhysLong` — this is how Adrenalift writes to GPU MMIO registers and patches the driver's PP table in RAM.
- **Physical memory reads** via `GetPhysLong` — used as a fallback when WinRing0 is unavailable.
- **I/O port access** — used for legacy PCI config space access (CF8/CFC).
- **ECAM discovery** — finds the PCI Express memory-mapped config base from ACPI MCFG tables, enabling PCI config access on non-root buses.

InpOut32 is the primary write backend. It works with Windows signature enforcement enabled (no test signing required).

### WinRing0 (`WinRing0x64.dll` + `.sys`)

WinRing0 is an open-source driver originally by [OpenLibSys](https://openlibsys.org/). Adrenalift uses it for:

- **PCI config space read/write** — device enumeration, BAR discovery, extended config registers.
- **Physical memory reads** — reading GPU MMIO registers at their BAR physical address.
- **I/O port access** — reading/writing I/O BAR ports for MMIO register writes.

The DLL loads the `.sys` kernel driver as a Windows service (`WinRing0_1_2_0`) on first use.

---

## The Patched Driver (`WinRing0x64_patched.sys`)

### Why it exists

The original `WinRing0x64.sys` contains a **1 MB restriction** on physical memory access. The `IOCTL_OLS_READ_MEMORY` handler calls `MmMapIoSpace()` but rejects any request targeting a physical address above 0x100000 (1 MB). This was a safety check in the original WinRing0 code.

GPU MMIO BARs are mapped at high physical addresses (typically above 0xC0000000), so the original driver cannot read them. The patched driver removes this restriction so Adrenalift can read GPU registers via physical memory.

### What was changed

The patched `.sys` is a binary patch of the original — **same PE timestamp, same compiler, same build.** Only the `.text` section contains modifications; all other sections (`.rdata`, `.data`, `.pdata`, `INIT`, `.rsrc`) are byte-identical.

**155 total bytes differ**, falling into four categories:

#### 1. PE header bookkeeping (6 bytes)

| Offset | Field | Original | Patched | Reason |
|--------|-------|----------|---------|--------|
| 0x0138 | PE checksum | 0x1908 | 0x1226 | Recalculated after binary edit |
| 0x018C | Certificate table size | 0x1ED0 | 0x0588 | Different signature size |
| 0x01F0 | `.text` VirtualSize | 0x06C6 | 0x0800 | Expanded to cover code cave |

#### 2. MmMapIoSpace 1 MB restriction removal (15 bytes)

Three small patches in the `.text` section:

**Site A** — file offset 0x0649, RVA 0x1249 (11 bytes):

```
Original:  3D 04 61 40 9C  0F 84 60 01 00 00
           CMP EAX,imm32   JE +0x160

Patched:   E9 78 04 00 00  90 90 90 90 90 90
           JMP +0x478       NOP NOP NOP NOP NOP NOP
```

Replaces the IOCTL dispatch comparison for WRITE_MEMORY with a jump to the code cave. The original conditional path (which included the address limit check) is bypassed.

**Site B** — file offset 0x0948, RVA 0x1548 (2 bytes):

```
Original:  7C 77       JL +0x77   (reject if size < lower bound)
Patched:   90 90       NOP NOP
```

**Site C** — file offset 0x0957, RVA 0x1557 (2 bytes):

```
Original:  7F 68       JG +0x68   (reject if size > upper bound)
Patched:   90 90       NOP NOP
```

Sites B and C remove the size-range validation on `MmMapIoSpace` calls, allowing mappings of any size at any physical address.

#### 3. Code cave — WriteMemory IOCTL handler (134 bytes)

New code written into zero-padded space at the end of the `.text` section (file offset 0x0AC6–0x0B5F). This implements a `WRITE_MEMORY` IOCTL using `MmMapIoSpace` + write + `MmUnmapIoSpace`.

> **This code is NOT used by Adrenalift.** It contains a bug that causes `KMODE_EXCEPTION_NOT_HANDLED` blue screens. All physical memory writes go through InpOut32's `SetPhysLong` instead.

#### 4. Authenticode signature

| | Size | Description |
|---|---:|---|
| Original | 7,888 bytes | Real Authenticode signature from WinRing0 author |
| Patched | 1,416 bytes | Test certificate (requires `bcdedit /set testsigning on`) |

The original signature is invalidated by the binary patch. The patched driver is re-signed with a test certificate, which is why test signing mode must be enabled.

### What is NOT changed

- **`.rdata`** section: byte-identical
- **`.data`** section: byte-identical
- **`.pdata`** section: byte-identical
- **`INIT`** section: byte-identical
- **`.rsrc`** section: byte-identical
- No new imports, exports, or resources
- No changes to the driver's initialization or unload routines

---

## Verifying the patch yourself

A verification script is included at `tools/verify_patch.py`. It performs a complete byte-for-byte comparison and classifies every difference.

### Running the verification

Place both `.sys` files in `drivers/` and run:

```bash
python tools/verify_patch.py
```

Or specify a custom directory:

```bash
python tools/verify_patch.py --drivers-dir /path/to/drivers
```

### Expected output

The script will:

1. Print SHA-256 hashes of both files.
2. Compare all PE section bytes (everything before the Authenticode certificate).
3. Show each differing region with hex context and `^^` markers under changed bytes.
4. Classify every changed byte into the four categories above.
5. Verify all non-`.text` sections are byte-identical.
6. Print **VERIFICATION PASSED** if all changes match the documented patch sites, or **WARNING** if any unexpected bytes differ.

### File hashes (reference)

| File | Size | SHA-256 |
|------|-----:|---------|
| `WinRing0x64.sys` (original) | 14,544 | `11bd2c9f9e2397c9a16e0990e4ed2cf0679498fe0fd418a3dfdac60b5c160ee5` |
| `WinRing0x64_patched.sys` | 8,072 | `de555dac3dcc23ff58eea10e72d3988dd1d656b9f9f52db33e8ab0117876d02a` |

The size difference (14,544 vs 8,072 bytes) is entirely due to the Authenticode signature: the original has a 7,888-byte certificate while the patched version has a 1,416-byte test certificate. The actual PE content (first 6,656 bytes) is the same size in both files.

---

## Test signing requirement

The patched driver requires Windows test signing mode because the original Authenticode signature is no longer valid:

```powershell
bcdedit /set testsigning on
```

A reboot is required after enabling test signing. Windows will display a "Test Mode" watermark on the desktop.

If test signing is not enabled, the patched driver will fail to load and Adrenalift will fall back to InpOut32-only mode (which still works for most operations but may lack full physical memory read support at high addresses).

---

## Fallback behavior

Adrenalift attempts to load drivers in this order:

1. **WinRing0 (patched) + InpOut32** — full capability (read + write at any physical address).
2. **WinRing0 (original) + InpOut32** — PCI config works, physical memory reads limited to 1 MB, writes via InpOut32.
3. **InpOut32 only** — works without test signing. Uses PowerShell/WMI for safe PCI device discovery instead of raw bus scanning.

The application prints which backend was loaded during startup (check the log or console output for `[WR0]` and `[InpOut32]` messages).
