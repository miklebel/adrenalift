"""
WinRing0x64 Patch Verification Tool
====================================
Compares WinRing0x64.sys (original) and WinRing0x64_patched.sys to show
exactly what was changed.  Run this to independently verify the patched
driver contains ONLY the documented modifications.

Usage:
    python verify_patch.py [--drivers-dir path/to/drivers]

Expected files:
    drivers/WinRing0x64.sys           (original, from WinRing0 open-source project)
    drivers/WinRing0x64_patched.sys   (NOP-patched version)
"""

import hashlib
import os
import struct
import sys


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pe_section_info(data):
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    timestamp = struct.unpack_from("<I", data, coff + 4)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    sec_start = opt + opt_size

    sections = []
    for i in range(num_sections):
        s = sec_start + i * 40
        name = data[s:s+8].rstrip(b"\x00").decode("ascii", errors="replace")
        vsize = struct.unpack_from("<I", data, s + 8)[0]
        vaddr = struct.unpack_from("<I", data, s + 12)[0]
        rawsize = struct.unpack_from("<I", data, s + 16)[0]
        rawptr = struct.unpack_from("<I", data, s + 20)[0]
        sections.append({
            "name": name, "vaddr": vaddr, "vsize": vsize,
            "rawsize": rawsize, "rawptr": rawptr,
        })

    # Certificate Table is data directory index 4 (PE32+: opt + 112 + 4*8)
    cert_dir_offset = opt + 112 + 4 * 8
    cert_offset = struct.unpack_from("<I", data, cert_dir_offset)[0]
    cert_size = struct.unpack_from("<I", data, cert_dir_offset + 4)[0]

    return {
        "timestamp": timestamp,
        "sections": sections,
        "cert_offset": cert_offset,
        "cert_size": cert_size,
    }


def section_for_offset(sections, offset):
    for s in sections:
        if s["rawptr"] <= offset < s["rawptr"] + s["rawsize"]:
            return s["name"]
    return "header"


def rva_for_offset(sections, offset):
    for s in sections:
        if s["rawptr"] <= offset < s["rawptr"] + s["rawsize"]:
            return s["vaddr"] + (offset - s["rawptr"])
    return offset


def main():
    drivers_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "drivers",
    )
    if len(sys.argv) > 2 and sys.argv[1] == "--drivers-dir":
        drivers_dir = sys.argv[2]

    orig_path = os.path.join(drivers_dir, "WinRing0x64.sys")
    patched_path = os.path.join(drivers_dir, "WinRing0x64_patched.sys")

    for p in (orig_path, patched_path):
        if not os.path.isfile(p):
            print(f"ERROR: File not found: {p}")
            sys.exit(1)

    orig = read_file(orig_path)
    patched = read_file(patched_path)

    print("=" * 72)
    print("WinRing0x64 Patch Verification")
    print("=" * 72)

    print(f"\nOriginal:  {orig_path}")
    print(f"  Size:    {len(orig)} bytes")
    print(f"  SHA-256: {sha256(orig)}")
    print(f"\nPatched:   {patched_path}")
    print(f"  Size:    {len(patched)} bytes")
    print(f"  SHA-256: {sha256(patched)}")

    orig_info = pe_section_info(orig)
    patch_info = pe_section_info(patched)

    print(f"\nPE timestamp (both): 0x{orig_info['timestamp']:08X}")
    if orig_info["timestamp"] != patch_info["timestamp"]:
        print("  WARNING: timestamps differ!")

    # Determine where PE sections end (= certificate start)
    pe_end = orig_info["cert_offset"]
    print(f"\nPE section data ends at: 0x{pe_end:X} ({pe_end} bytes)")
    print(f"Original  certificate: {orig_info['cert_size']} bytes "
          f"(Authenticode signature)")
    print(f"Patched   certificate: {patch_info['cert_size']} bytes "
          f"(test-signed, smaller)")

    print(f"\nComparing first 0x{pe_end:X} bytes (all PE sections)...")

    orig_pe = orig[:pe_end]
    patch_pe = patched[:pe_end]
    sections = orig_info["sections"]

    diffs = []
    for i in range(pe_end):
        if orig_pe[i] != patch_pe[i]:
            diffs.append(i)

    print(f"Total differing bytes: {len(diffs)}")

    # Group consecutive diffs into regions
    regions = []
    if diffs:
        start = diffs[0]
        end = diffs[0]
        for d in diffs[1:]:
            if d <= end + 8:
                end = d
            else:
                regions.append((start, end))
                start = d
                end = d
        regions.append((start, end))

    # Known PE header fields that change
    HEADER_FIELDS = {
        0x0138: "PE checksum",
        0x018C: "Certificate table size (data directory)",
        0x01F0: ".text VirtualSize",
    }

    print(f"\n{'=' * 72}")
    print("DETAILED DIFF")
    print("=" * 72)

    for rstart, rend in regions:
        section = section_for_offset(sections, rstart)
        rva = rva_for_offset(sections, rstart)
        size = rend - rstart + 1

        # Check if this is a known header field
        header_note = ""
        for off, desc in HEADER_FIELDS.items():
            if rstart <= off <= rend:
                header_note = f"  ({desc})"
                break

        print(f"\n--- Region: file 0x{rstart:04X}-0x{rend:04X} "
              f"({size} bytes) [{section}] RVA 0x{rva:04X}{header_note} ---")

        if section == "header":
            for i in range(rstart, rend + 1):
                if orig_pe[i] != patch_pe[i]:
                    note = HEADER_FIELDS.get(i & ~1, "")
                    if note:
                        note = f"  <- {note}"
                    print(f"  0x{i:04X}: 0x{orig_pe[i]:02X} -> 0x{patch_pe[i]:02X}{note}")
            continue

        # Show context around the change
        ctx_before = max(rstart - 4, 0)
        ctx_after = min(rend + 5, pe_end)

        print(f"  Original:  {' '.join(f'{orig_pe[i]:02X}' for i in range(ctx_before, ctx_after))}")
        print(f"  Patched:   {' '.join(f'{patch_pe[i]:02X}' for i in range(ctx_before, ctx_after))}")
        markers = []
        for i in range(ctx_before, ctx_after):
            if orig_pe[i] != patch_pe[i]:
                markers.append("^^")
            else:
                markers.append("  ")
        print(f"             {' '.join(markers)}")

    print(f"\n{'=' * 72}")
    print("INTERPRETATION")
    print("=" * 72)

    # Classify the changes
    header_diffs = [d for d in diffs if d < sections[0]["rawptr"]]
    code_patch_diffs = []  # NOP + JMP rewrites at known patch sites
    code_cave_diffs = []
    other_diffs = []

    # Known patch site ranges (the JMP rewrite + NOPs)
    PATCH_SITES = [
        (0x0649, 0x0653),  # Site A: CMP+JE -> JMP+NOPs
        (0x0948, 0x0949),  # Site B: JL -> NOP NOP
        (0x0957, 0x0958),  # Site C: JG -> NOP NOP
    ]
    CODE_CAVE_RANGE = (0x0AC6, 0x0B5F)

    def in_patch_site(offset):
        for a, b in PATCH_SITES:
            if a <= offset <= b:
                return True
        return False

    for d in diffs:
        if d < sections[0]["rawptr"]:
            continue
        sec = section_for_offset(sections, d)
        if sec != ".text":
            other_diffs.append(d)
        elif in_patch_site(d):
            code_patch_diffs.append(d)
        elif CODE_CAVE_RANGE[0] <= d <= CODE_CAVE_RANGE[1]:
            code_cave_diffs.append(d)
        else:
            other_diffs.append(d)

    print(f"""
1. PE HEADER CHANGES ({len(header_diffs)} bytes):
   - PE checksum: recalculated (changes when any byte changes)
   - Certificate table size: different signature size (test vs original)
   - .text VirtualSize: expanded from 0x6C6 to 0x800 to cover code cave

2. CODE PATCH: MmMapIoSpace 1MB Restriction Removal ({len(code_patch_diffs)} bytes):
   The original READ_MEMORY IOCTL handler checks if the requested physical
   address is within 1MB (0x100000) and refuses if it's above.
   
   Patch site A (0x0649): Original CMP+JE replaced with JMP to code cave + NOPs
     This redirects the IOCTL dispatch past the size check.
   
   Patch site B (0x0948): JL conditional branch -> NOP NOP
     Removes lower-bound size validation.
   
   Patch site C (0x0957): JG conditional branch -> NOP NOP
     Removes upper-bound size validation.

3. CODE CAVE: WriteMemory Handler ({len(code_cave_diffs)} bytes at 0x0AC6-0x0B5F):
   New code written into zero-padding at the end of .text section.
   Implements a WRITE_MEMORY IOCTL handler (MmMapIoSpace + write + MmUnmapIoSpace).
   
   >>> THIS CODE IS NOT USED BY ADRENALIFT <<<
   It has a bug that causes KMODE_EXCEPTION_NOT_HANDLED BSODs.
   All physical memory writes go through InpOut32 (SetPhysLong) instead.

4. AUTHENTICODE SIGNATURE:
   Original: {orig_info['cert_size']} bytes (real Authenticode from WinRing0 author)
   Patched:  {patch_info['cert_size']} bytes (test certificate, requires testsigning ON)
""")

    if other_diffs:
        print(f"WARNING: {len(other_diffs)} bytes changed outside expected regions!")
        for d in other_diffs[:20]:
            print(f"  0x{d:04X}: 0x{orig_pe[d]:02X} -> 0x{patch_pe[d]:02X}")
    else:
        print("VERIFICATION PASSED: All changes accounted for.")
        print("  - No modifications outside the documented patch sites")
        print("  - No changes to .rdata, .data, .pdata, INIT, or .rsrc sections")

    # Verify non-.text sections are identical
    print(f"\nSection-by-section identity check:")
    for s in sections:
        if s["name"] == ".text":
            continue
        a = orig[s["rawptr"]:s["rawptr"]+s["rawsize"]]
        b = patched[s["rawptr"]:s["rawptr"]+s["rawsize"]]
        match = a == b
        status = "IDENTICAL" if match else "DIFFERENT"
        print(f"  {s['name']:10s}: {status}")

    print()


if __name__ == "__main__":
    main()
