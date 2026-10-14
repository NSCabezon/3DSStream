#!/usr/bin/env python3
"""Sanity-checks the HOME Menu banner inside a .cia (or a raw banner.bnr).

Checks (3dbrew CBMD / CGFX / BCWAV notes):
  - ExeFS 'banner' is a CBMD; common CGFX at the declared offset decompresses (LZ11)
    to a well-formed CGFX: magic, BOM, header size, file size, DATA block, dictionaries
    whose entries point inside the file, and < 512 KiB.
  - Compressed exactly once (a CGFX that is itself LZ11 data crashes HOME Menu).
  - Optional: matches the given reference .cgfx byte for byte.
  - CWAV present (PCM16 / 2 channels / <= 3 s reported as warnings).
Usage: python3 scripts/verify_cia_banner.py 3DSStream.cia [resource/banner.cgfx]
Adapted from the topos project's scripts/verify_cia_banner.py (audio checks).
"""
import struct
import sys

CGFX_MAX = 0x80000


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def align64(v):
    return (v + 0x3F) & ~0x3F


def banner_from_cia(cia):
    hdr, cert, tick, tmd = u32(cia, 0), u32(cia, 8), u32(cia, 0xC), u32(cia, 0x10)
    cont = align64(align64(align64(align64(hdr) + cert) + tick) + tmd)
    if cia[cont + 0x100:cont + 0x104] != b"NCCH":
        raise SystemExit(f"FAIL: no NCCH at 0x{cont:X}")
    exefs = cont + u32(cia, cont + 0x1A0) * 0x200
    for i in range(10):
        base = exefs + i * 0x10
        if cia[base:base + 8].rstrip(b"\0") == b"banner":
            off, size = u32(cia, base + 8), u32(cia, base + 12)
            return cia[exefs + 0x200 + off:exefs + 0x200 + off + size]
    raise SystemExit("FAIL: ExeFS has no 'banner'")


def lz11_decompress(src):
    if src[0] != 0x11:
        raise ValueError(f"not LZ11 (type byte 0x{src[0]:02X})")
    size = src[1] | src[2] << 8 | src[3] << 16
    pos = 4
    if size == 0:
        size, pos = u32(src, 4), 8
    out = bytearray()
    while len(out) < size:
        flags = src[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if not flags & (0x80 >> bit):
                out.append(src[pos])
                pos += 1
                continue
            b0 = src[pos]
            ind = b0 >> 4
            if ind == 0:
                n = (((b0 & 0xF) << 4) | (src[pos + 1] >> 4)) + 0x11
                disp = ((src[pos + 1] & 0xF) << 8 | src[pos + 2]) + 1
                pos += 3
            elif ind == 1:
                n = (((b0 & 0xF) << 12) | (src[pos + 1] << 4) | (src[pos + 2] >> 4)) + 0x111
                disp = ((src[pos + 2] & 0xF) << 8 | src[pos + 3]) + 1
                pos += 4
            else:
                n = ind + 1
                disp = ((b0 & 0xF) << 8 | src[pos + 1]) + 1
                pos += 2
            if disp > len(out):
                raise ValueError("LZ11 back-reference before start")
            for _ in range(n):
                out.append(out[-disp])
    return bytes(out[:size]), pos


def check_cgfx(cg, problems):
    if cg[:4] != b"CGFX":
        problems.append(f"CGFX magic missing (got {cg[:4]!r})")
        return
    if u16(cg, 4) != 0xFEFF:
        problems.append("CGFX BOM is not little-endian 0xFEFF")
    if u16(cg, 6) != 0x14:
        problems.append(f"CGFX header size 0x{u16(cg, 6):X} != 0x14")
    fsize, nblocks = u32(cg, 0xC), u32(cg, 0x10)
    if fsize != len(cg):
        problems.append(f"CGFX file size field {fsize} != data length {len(cg)}")
    if len(cg) >= CGFX_MAX:
        problems.append(f"CGFX is {len(cg)} bytes (HOME Menu limit {CGFX_MAX})")
    if cg[0x14:0x18] != b"DATA":
        problems.append("DATA block not at 0x14")
        return
    dsize = u32(cg, 0x18)
    print(f"CGFX: {len(cg)} bytes, revision 0x{u32(cg, 8):08X}, {nblocks} block(s), DATA {dsize} bytes")
    names = ["models", "textures", "LUTs", "materials", "shaders", "cameras", "lights",
             "fogs", "scenes", "skel anims", "mat anims", "vis anims", "cam anims",
             "light anims", "emitters"]
    for i, name in enumerate(names):
        o = 0x1C + i * 8
        count, rel = u32(cg, o), u32(cg, o + 4)
        if not count:
            continue
        dict_off = o + 4 + rel
        if cg[dict_off:dict_off + 4] != b"DICT":
            problems.append(f"{name}: DICT magic missing at 0x{dict_off:X}")
            continue
        n = u32(cg, dict_off + 8)
        entries = []
        for e in range(n):  # entries follow the root node; 0x10 bytes each
            eo = dict_off + 0x1C + e * 0x10
            name_off = eo + 8 + struct.unpack_from("<i", cg, eo + 8)[0]
            data_off = eo + 12 + struct.unpack_from("<i", cg, eo + 12)[0]
            if not (0 < name_off < len(cg) and 0 < data_off < len(cg)):
                problems.append(f"{name}: entry {e} points outside the file")
                continue
            entries.append(cg[name_off:cg.index(b"\0", name_off)].decode("ascii", "replace"))
        if n != count:
            problems.append(f"{name}: DICT count {n} != header count {count}")
        print(f"  {name:11s} {count}: {', '.join(entries)}")
    if nblocks >= 2 and b"IMAG" not in cg[0x14 + dsize - 0x40:]:
        problems.append("IMAG block announced but not found")


def check_cwav(cw, problems, warnings):
    if cw[:4] != b"CWAV":
        problems.append(f"no CWAV (got {cw[:4]!r})")
        return
    info = u16(cw, 6)
    enc, loop, sr = cw[info + 8], cw[info + 9], u32(cw, info + 0xC)
    lend, ch = u32(cw, info + 0x14), u32(cw, info + 0x1C)
    print(f"CWAV: encoding={enc} loop={loop} {sr} Hz, {ch} ch, {lend / sr if sr else 0:.2f} s")
    # audio quirks don't break the banner image: warnings only
    if enc != 1:
        warnings.append("CWAV not PCM16")
    if ch != 2:
        warnings.append("CWAV is not 2 channels (3dbrew: banner sound should be stereo)")
    if sr and lend > sr * 3:
        warnings.append("CWAV longer than 3 s")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    raw = open(sys.argv[1], "rb").read()
    bnr = raw if raw[:4] == b"CBMD" else banner_from_cia(raw)
    problems = []
    if bnr[:4] != b"CBMD":
        print(f"FAIL: banner is not CBMD (got {bnr[:4]!r})")
        return 1
    cg_off, cw_off = u32(bnr, 8), u32(bnr, 0x84)
    print(f"CBMD: {len(bnr)} bytes, common CGFX at 0x{cg_off:X}, CWAV at 0x{cw_off:X}")
    regional = [u32(bnr, 0xC + 4 * i) for i in range(13)]
    if any(regional):
        print(f"  note: region-specific CGFX offsets set: {[hex(r) for r in regional if r]}")
    comp = bnr[cg_off:cw_off if cw_off > cg_off else len(bnr)]
    try:
        cg, used = lz11_decompress(comp)
        print(f"LZ11: {used} compressed -> {len(cg)} bytes")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: common CGFX does not decompress: {e}")
        return 1
    if cg[:1] == b"\x11" and cg[:4] != b"CGFX":
        problems.append("CGFX is LZ11-compressed twice (crashes HOME Menu)")
    check_cgfx(cg, problems)
    if len(sys.argv) > 2:
        ref = open(sys.argv[2], "rb").read()
        if ref == cg:
            print(f"matches {sys.argv[2]} byte for byte")
        else:
            problems.append(f"embedded CGFX differs from {sys.argv[2]}")
    warnings = []
    check_cwav(bnr[cw_off:], problems, warnings)
    print()
    for w in warnings:
        print(f"  warn: {w}")
    for p in problems:
        print(f"  !! {p}")
    print("SUMMARY:", "OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
