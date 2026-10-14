#!/usr/bin/env python3
"""Check the HOME Menu banner packed inside a built CIA.

Usage:
    python3 scripts/verify_cia_banner.py 3DSStream.cia resource/banner.cgfx   # 3D model banner
    python3 scripts/verify_cia_banner.py 3DSStream.cia                        # flat banner

Walks CIA -> content 0 (NCCH) -> ExeFS -> "banner" and checks:
  * the CBMD header (magic, CGFX offsets, CWAV offset);
  * the common CGFX is LZ11-compressed exactly once, and (when a CGFX file is given)
    decompresses to that file byte for byte;
  * the CGFX header and its DICT offsets stay inside the file, and the file is < 512 KiB;
  * the BCWAV header (magic, blocks, channel count; a mono banner sound only warns).
Prints "SUMMARY: OK" (exit 0) or "SUMMARY: FAIL" (exit 1).

Formats follow the 3dbrew.org pages "CIA", "Title metadata", "NCCH", "ExeFS", "CBMD",
"CGFX", "BCWAV" and "LZ11" (Nintendo's LZ77 variant). Standard library only.
"""

import hashlib
import struct
import sys

CGFX_MAX = 512 * 1024


class Report:
    def __init__(self):
        self.failed = False

    def ok(self, msg):
        print(f"  ok    {msg}")

    def warn(self, msg):
        print(f"  WARN  {msg}")

    def fail(self, msg):
        print(f"  FAIL  {msg}")
        self.failed = True

    def check(self, cond, msg):
        (self.ok if cond else self.fail)(msg)
        return cond


class FormatError(Exception):
    pass


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def align(n, a):
    return (n + a - 1) // a * a


# ---------------------------------------------------------------- container walking

# Signature block sizes by signature type (signature + padding to 0x40 alignment).
SIG_SIZES = {
    0x010000: 0x200 + 0x3C, 0x010003: 0x200 + 0x3C,  # RSA-4096
    0x010001: 0x100 + 0x3C, 0x010004: 0x100 + 0x3C,  # RSA-2048
    0x010002: 0x3C + 0x40, 0x010005: 0x3C + 0x40,    # ECDSA
}


def cia_content0(cia):
    """Return the raw bytes of the first content (the NCCH) of a CIA."""
    if len(cia) < 0x2020:
        raise FormatError("file too small for a CIA header")
    hdr_size, _ctype, _ver, cert_size, tik_size, tmd_size, _meta_size = \
        struct.unpack_from("<IHHIIII", cia, 0)
    content_size = u64(cia, 0x18)
    if hdr_size != 0x2020:
        raise FormatError(f"unexpected CIA header size 0x{hdr_size:x}")
    tmd_off = align(hdr_size, 64) + align(cert_size, 64) + align(tik_size, 64)
    content_off = tmd_off + align(tmd_size, 64)

    sig_type = struct.unpack_from(">I", cia, tmd_off)[0]
    if sig_type not in SIG_SIZES:
        raise FormatError(f"unknown TMD signature type 0x{sig_type:x}")
    tmd_hdr = tmd_off + 4 + SIG_SIZES[sig_type]
    count = struct.unpack_from(">H", cia, tmd_hdr + 0x9E)[0]
    if count == 0:
        raise FormatError("TMD lists no contents")
    chunk = tmd_hdr + 0xC4 + 64 * 0x24          # after header and content info records
    cid, cindex, ctype, csize = struct.unpack_from(">IHHQ", cia, chunk)
    if cindex != 0:
        raise FormatError(f"first TMD content has index {cindex}, expected 0")
    if ctype & 1:
        raise FormatError("content 0 is encrypted; cannot inspect it")
    if content_off + csize > len(cia) or csize > content_size:
        raise FormatError("content 0 runs past the end of the CIA")
    print(f"  CIA: {count} content(s), content 0 id {cid:08x}, 0x{csize:x} bytes")
    return cia[content_off:content_off + csize]


def ncch_exefs(ncch):
    if ncch[0x100:0x104] != b"NCCH":
        raise FormatError("content 0 is not an NCCH")
    flags = ncch[0x188:0x190]
    if not flags[7] & 0x04:
        raise FormatError("NCCH is encrypted (NoCrypto flag clear)")
    unit = 0x200 << flags[6]
    off, size = u32(ncch, 0x1A0) * unit, u32(ncch, 0x1A4) * unit
    if size == 0 or off + size > len(ncch):
        raise FormatError("NCCH has no ExeFS or it runs past the content")
    product = ncch[0x150:0x160].rstrip(b"\0").decode("ascii", "replace")
    print(f"  NCCH: product code {product}, ExeFS 0x{size:x} bytes at 0x{off:x}")
    return ncch[off:off + size]


def exefs_file(exefs, want, rep):
    for i in range(10):
        name, off, size = struct.unpack_from("<8sII", exefs, i * 16)
        name = name.rstrip(b"\0").decode("ascii", "replace")
        if name != want:
            continue
        start = 0x200 + off
        if start + size > len(exefs):
            raise FormatError(f"ExeFS file '{want}' runs past the ExeFS")
        data = exefs[start:start + size]
        # SHA-256 table at 0xC0, stored in reverse order of the file entries.
        stored = exefs[0x200 - 0x20 * (i + 1):0x200 - 0x20 * i]
        rep.check(hashlib.sha256(data).digest() == stored,
                  f"ExeFS '{want}': 0x{size:x} bytes, SHA-256 matches the ExeFS header")
        return data
    raise FormatError(f"ExeFS has no '{want}' file")


# ---------------------------------------------------------------- LZ11

def lz11_decompress(src, start=0):
    """Decompress one LZ11 stream. Returns (data, bytes_consumed)."""
    if src[start] != 0x11:
        raise FormatError(f"LZ11 stream must start with 0x11, got 0x{src[start]:02x}")
    out_size = u32(src, start) >> 8
    pos = start + 4
    if out_size == 0:                    # sizes >= 16 MiB use an extra 32-bit field
        out_size = u32(src, pos)
        pos += 4
    out = bytearray()
    try:
        while len(out) < out_size:
            flags = src[pos]
            pos += 1
            for bit in range(7, -1, -1):
                if len(out) >= out_size:
                    break
                if not flags >> bit & 1:
                    out.append(src[pos])
                    pos += 1
                    continue
                b0 = src[pos]
                kind = b0 >> 4
                if kind == 0:
                    b1, b2 = src[pos + 1], src[pos + 2]
                    length = ((b0 & 0xF) << 4 | b1 >> 4) + 0x11
                    disp = ((b1 & 0xF) << 8 | b2) + 1
                    pos += 3
                elif kind == 1:
                    b1, b2, b3 = src[pos + 1], src[pos + 2], src[pos + 3]
                    length = ((b0 & 0xF) << 12 | b1 << 4 | b2 >> 4) + 0x111
                    disp = ((b2 & 0xF) << 8 | b3) + 1
                    pos += 4
                else:
                    b1 = src[pos + 1]
                    length = kind + 1
                    disp = ((b0 & 0xF) << 8 | b1) + 1
                    pos += 2
                if disp > len(out):
                    raise FormatError("LZ11 back-reference before start of output")
                for _ in range(length):
                    out.append(out[-disp])
    except IndexError:
        raise FormatError("LZ11 stream truncated") from None
    if len(out) != out_size:
        raise FormatError("LZ11 output overran its declared size")
    return bytes(out), pos - start


# ---------------------------------------------------------------- CBMD / CGFX / CWAV

def check_cbmd(banner, rep):
    """Return (cgfx_offset, cgfx_end, cwav_offset) after validating the CBMD header."""
    if len(banner) < 0x88 or banner[:4] != b"CBMD":
        rep.fail("CBMD magic missing")
        raise FormatError("banner is not a CBMD file")
    rep.ok("CBMD magic")
    offsets = struct.unpack_from("<14I", banner, 0x08)  # common + 13 regional CGFX
    cwav_off = u32(banner, 0x84)
    common = offsets[0]
    rep.check(common == 0x88, f"common CGFX offset 0x{common:x} (expected 0x88)")
    regional = [o for o in offsets[1:] if o]
    if regional:
        rep.warn(f"{len(regional)} regional CGFX override(s) present; only the common one is checked")
    rep.check(common < cwav_off <= len(banner) - 0x40,
              f"CWAV offset 0x{cwav_off:x} after the CGFX and inside the banner (0x{len(banner):x})")
    ends = sorted(o for o in offsets if o) + [cwav_off]
    cgfx_end = next(o for o in ends if o > common)
    return common, cgfx_end, cwav_off


def check_cgfx(cgfx, rep):
    if not rep.check(cgfx[:4] == b"CGFX", "decompressed data has CGFX magic"):
        return
    bom, hsize, _rev, fsize, nsec = struct.unpack_from("<HHIII", cgfx, 4)
    rep.check(bom == 0xFEFF, "CGFX byte-order mark is little endian")
    rep.check(fsize == len(cgfx), f"CGFX size field 0x{fsize:x} matches data (0x{len(cgfx):x})")
    rep.check(len(cgfx) < CGFX_MAX, f"CGFX is {len(cgfx)} bytes (< {CGFX_MAX})")
    data = hsize
    if not rep.check(cgfx[data:data + 4] == b"DATA", f"DATA block at 0x{data:x}"):
        return
    dsize = u32(cgfx, data + 4)
    rep.check(data + dsize <= len(cgfx), "DATA block fits in the file")
    # Up to 16 (count, self-relative offset) DICT references. How many are present
    # depends on the CGFX revision, so the table ends where the first DICT begins.
    bad = used = 0
    table_end = data + 8 + 16 * 8
    for i in range(16):
        ref = data + 8 + i * 8
        if ref >= table_end:
            break
        count, rel = struct.unpack_from("<II", cgfx, ref)
        if count == 0 and rel == 0:
            continue
        used += 1
        dic = ref + 4 + rel              # offsets are relative to the offset field itself
        table_end = min(table_end, dic)
        if dic + 12 > len(cgfx) or cgfx[dic:dic + 4] != b"DICT" \
                or dic + u32(cgfx, dic + 4) > len(cgfx) or u32(cgfx, dic + 8) != count:
            rep.fail(f"DICT {i} (offset 0x{dic:x}, {count} entries) is out of range or malformed")
            bad += 1
            continue
        for e in range(count):           # entries follow a root node (16 bytes each)
            ent = dic + 0x1C + e * 0x10
            if ent + 16 > len(cgfx):
                rep.fail(f"DICT {i} entry {e} runs past the file")
                bad += 1
                break
            for field in (ent + 8, ent + 12):
                rel = u32(cgfx, field)
                if rel and not field + rel < len(cgfx):
                    rep.fail(f"DICT {i} entry {e} points outside the file")
                    bad += 1
    if not bad:
        rep.ok(f"{used} DICT(s) and their entries point inside the file ({nsec} section(s))")


def check_cwav(cwav, rep):
    if not rep.check(cwav[:4] == b"CWAV", "CWAV magic"):
        return
    bom, _hsize, _ver, fsize, nblocks = struct.unpack_from("<HHIIH", cwav, 4)
    rep.check(bom == 0xFEFF, "CWAV byte-order mark is little endian")
    rep.check(fsize <= len(cwav), f"CWAV size 0x{fsize:x} fits in the banner (0x{len(cwav):x} left)")
    blocks = {}
    for i in range(nblocks):
        btype, boff, bsize = struct.unpack_from("<HxxII", cwav, 0x14 + i * 12)
        blocks[btype] = (boff, bsize)
    info = blocks.get(0x7000)
    datab = blocks.get(0x7001)
    if not rep.check(info is not None and datab is not None, "CWAV has INFO and DATA blocks"):
        return
    for name, (off, size), magic in (("INFO", info, b"INFO"), ("DATA", datab, b"DATA")):
        rep.check(off + size <= fsize and cwav[off:off + 4] == magic,
                  f"{name} block at 0x{off:x} (0x{size:x} bytes) is inside the file")
    ioff = info[0]
    encoding, looped = cwav[ioff + 8], cwav[ioff + 9]
    rate = u32(cwav, ioff + 0x0C)
    end = u32(cwav, ioff + 0x14)
    channels = u32(cwav, ioff + 0x1C)
    enc = {0: "PCM8", 1: "PCM16", 2: "DSP-ADPCM", 3: "IMA-ADPCM"}.get(encoding, f"type {encoding}")
    secs = end / rate if rate else 0
    rep.ok(f"{enc}, {rate} Hz, {channels} channel(s), {secs:.2f} s, loop={'yes' if looped else 'no'}")
    if channels == 2:
        rep.ok("stereo")
    elif channels >= 1:
        rep.warn(f"banner sound has {channels} channel(s), expected stereo")
    else:
        rep.fail("CWAV declares no channels")


# ---------------------------------------------------------------- main

def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    cia_path = argv[1]
    ref_path = argv[2] if len(argv) == 3 else None
    rep = Report()
    try:
        with open(cia_path, "rb") as f:
            cia = f.read()
        print(f"{cia_path}:")
        banner = exefs_file(ncch_exefs(cia_content0(cia)), "banner", rep)

        print("CBMD:")
        cgfx_off, cgfx_end, cwav_off = check_cbmd(banner, rep)

        print("CGFX:")
        cgfx, used = lz11_decompress(banner, cgfx_off)
        rep.check(cgfx_off + used <= cgfx_end,
                  f"LZ11 stream (0x{used:x} bytes) ends before the next section")
        rep.check(cgfx[:1] != b"\x11" and cgfx[:4] == b"CGFX",
                  "compressed once (output is CGFX, not another LZ11 stream)")
        check_cgfx(cgfx, rep)
        if ref_path:
            with open(ref_path, "rb") as f:
                ref = f.read()
            if cgfx == ref:
                rep.ok(f"identical to {ref_path} ({len(ref)} bytes)")
            else:
                diff = next((i for i, (a, b) in enumerate(zip(cgfx, ref)) if a != b),
                            min(len(cgfx), len(ref)))
                rep.fail(f"differs from {ref_path} (sizes {len(cgfx)} vs {len(ref)}, "
                         f"first difference at 0x{diff:x})")
        else:
            rep.ok("flat banner mode: no reference CGFX to compare")

        print("CWAV:")
        check_cwav(banner[cwav_off:], rep)
    except (FormatError, struct.error, OSError) as e:
        rep.fail(str(e))

    print("SUMMARY: FAIL" if rep.failed else "SUMMARY: OK")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
