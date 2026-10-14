"""Shared helpers for the 3DSStream wire protocol (see docs/ARCHITECTURE.md)."""
import struct

MAGIC = b"S3DS"
TYPE_VIDEO_AU = 0x01
TYPE_HELLO = 0x02
TYPE_AUDIO = 0x03  # u64 BE timestamp (us) + one Opus packet (48 kHz stereo, 20 ms)
TYPE_KEYFRAME_REQ = 0x10
TYPE_LOG = 0x20  # 3DS -> sender: previous run's diagnostic log (text, chunks <= 64 KiB)
FLAG_KEYFRAME = 0x01     # AU contains an IDR
FLAG_EYE_RIGHT = 0x02    # alternating layout: 0 = left eye, 1 = right eye
FLAG_ALTERNATING = 0x04  # 1 = alternating 240x400 eyes (L,R,L,R...), 0 = stacked 240x800
MAX_PAYLOAD = 512 * 1024
AUDIO_MAX_PAYLOAD = 4096
PORT = 7777
HEADER_LEN = 12

_HDR = struct.Struct(">4sBBHI")


def pack_header(msg_type, flags, length):
    return _HDR.pack(MAGIC, msg_type, flags, 0, length)


def pack_message(msg_type, flags=0, payload=b""):
    return pack_header(msg_type, flags, len(payload)) + payload


def unpack_header(data):
    """Returns (type, flags, length). Raises ValueError on bad magic/length."""
    if len(data) != HEADER_LEN:
        raise ValueError("short header")
    magic, mtype, flags, _res, length = _HDR.unpack(data)
    if magic != MAGIC:
        raise ValueError("bad magic %r" % (magic,))
    if length > MAX_PAYLOAD:
        raise ValueError("payload too large: %d" % length)
    return mtype, flags, length


def find_start_codes(buf):
    """Returns list of (sc_pos, nal_pos) for each Annex-B start code (3 or 4 byte)."""
    out = []
    i = 0
    while True:
        p = buf.find(b"\x00\x00\x01", i)
        if p < 0:
            break
        sc = p - 1 if p > 0 and buf[p - 1] == 0 else p
        # a 4-byte code must not overlap the previous NAL's start code
        if out and sc < out[-1][1]:
            sc = p
        out.append((sc, p + 3))
        i = p + 3
    return out


def split_nals(data):
    """Splits an Annex-B byte string into NAL units (without start codes)."""
    scs = find_start_codes(data)
    nals = []
    for k, (_sc, npos) in enumerate(scs):
        end = scs[k + 1][0] if k + 1 < len(scs) else len(data)
        nal = data[npos:end]
        if k + 1 == len(scs):
            nal = nal.rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def nal_type(nal):
    return nal[0] & 0x1F


def join_nals(nals):
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def au_is_keyframe(au):
    """True if the Annex-B access unit contains an IDR slice (NAL type 5)."""
    return any(nal_type(n) == 5 for n in split_nals(au))
