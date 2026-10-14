#!/usr/bin/env python3
"""Fake Vision Pro sender: encodes a pre-rotated stereo test stream and sends it to a 3DS
(or recv_sim.py) using the S3DS protocol.

Layouts:
  alternating (default): one eye per frame, 240x400, L,R,L,R... at 2x fps. IDRs only on
                         left frames (even index); flags carry the eye.
  stacked:               240x800 per frame, left eye on top, at fps."""
import argparse
import os
import select
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s3ds_proto as P  # noqa: E402

FFMPEG = "/opt/homebrew/bin/ffmpeg"
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"


def parse_bitrate(s):
    s = s.strip().lower()
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1000000, s[:-1]
    return int(float(s) * mult)


def combine(args, left, right):
    """Filter graph tail that turns the two pre-rotated eye pads into [v]."""
    if args.layout == "stacked":
        return "[%s][%s]vstack[v]" % (left, right)
    # Left frame n gets pts 2n, right frame n gets 2n+1 (time base 1/(2*fps)); interleave
    # then merges by pts, so the output is strictly L,R,L,R... starting with left.
    rate = 2 * args.fps
    return ("[%s]setpts=2*N/(%d*TB)[li];[%s]setpts=(2*N+1)/(%d*TB)[ri];"
            "[li][ri]interleave[v]" % (left, rate, right, rate))


def build_ffmpeg_cmd(args):
    fps = args.fps
    out_fps = 2 * fps if args.layout == "alternating" else fps
    # Even GOP length, no scene-cut IDRs: every keyframe lands on a left (even) frame.
    keyint = out_fps
    bps = parse_bitrate(args.bitrate)
    eye_tail = "scale=400:240,transpose=clock"
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if args.source == "pattern":
        fc = (
            "[0:v]split[a][b];"
            "[1:v]split[b1][b2];"
            "[a][b1]overlay=x='mod(t*100,340)+8':y=100[l0];"
            "[b]crop=390:240:0:0,pad=400:240:10:0[r1];"
            "[r1][b2]overlay=x='mod(t*100,340)-8':y=100[r0];"
            "[l0]%s[lr];[r0]%s[rr];" % (eye_tail, eye_tail)
        ) + combine(args, "lr", "rr")
        cmd += ["-re", "-f", "lavfi", "-i", "testsrc2=s=400x240:r=%d" % fps,
                "-re", "-f", "lavfi", "-i", "color=c=red:s=40x40:r=%d" % fps]
        cmd += ["-filter_complex", fc]
    else:
        cmd += ["-re"]
        if args.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", args.source]
        if args.sbs:
            fc = (
                "[0:v]fps=%d,split[a][b];"
                "[a]crop=iw/2:ih:0:0,%s[lr];"
                "[b]crop=iw/2:ih:iw/2:0,%s[rr];" % (fps, eye_tail, eye_tail)
            ) + combine(args, "lr", "rr")
        else:
            fc = "[0:v]fps=%d,%s,split[lr][rr];" % (fps, eye_tail) + combine(args, "lr", "rr")
        cmd += ["-filter_complex", fc]
    cmd += [
        "-map", "[v]", "-an",
        "-c:v", "libx264", "-profile:v", "baseline", "-preset", "ultrafast",
        "-tune", "zerolatency", "-bf", "0", "-g", str(keyint), "-r", str(out_fps),
        "-b:v", str(bps), "-maxrate", str(bps), "-bufsize", str(bps // 2),
        "-pix_fmt", "yuv420p",
        # sliced-threads=0: zerolatency otherwise splits every frame into one slice per
        # thread, and the 3DS MVD decoder only handles single-slice pictures.
        "-x264-params", "aud=1:repeat-headers=1:keyint=%d:min-keyint=%d:scenecut=0:"
        "sliced-threads=0:slices=1" % (keyint, keyint),
        "-f", "h264", "-",
    ]
    return cmd


class AUParser:
    """Incrementally splits an Annex-B stream into AUs using AUD (type 9) as boundary.
    AUD NALs are stripped. Emitted AUs keep 4-byte start codes. A NAL is only consumed
    once the next start code is visible (so it is known to be complete)."""

    def __init__(self):
        self.buf = b""
        self.cur = []

    def feed(self, data):
        self.buf += data
        out = []
        scs = P.find_start_codes(self.buf)
        if len(scs) < 2:
            return out
        for k in range(len(scs) - 1):
            nal = self.buf[scs[k][1]:scs[k + 1][0]]
            if not nal:
                continue
            if P.nal_type(nal) == 9:
                if self.cur:
                    out.append(P.join_nals(self.cur))
                    self.cur = []
            else:
                self.cur.append(nal)
        self.buf = self.buf[scs[-1][0]:]
        return out


class Stats:
    def __init__(self):
        self.t0 = time.time()
        self.frames = 0
        self.bytes = 0
        self.keys = 0
        self.drops = 0

    def tick(self, force=False):
        now = time.time()
        dt = now - self.t0
        if dt >= 1.0 or force:
            print("[send] %.1f fps  %d kbps  keyframes %d  dropped %d"
                  % (self.frames / dt, self.bytes * 8 / dt / 1000, self.keys, self.drops), flush=True)
            self.t0 = now
            self.frames = self.bytes = self.keys = self.drops = 0


LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class LogSink:
    """Collects the LOG (0x20) chunks the 3DS sends right after connect (its previous
    run's log_prev.txt) into tools/logs/3ds_<timestamp>.txt."""

    def __init__(self):
        self.path = None
        self.f = None
        self.done = False

    def add(self, chunk):
        if self.f is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            self.path = os.path.join(LOG_DIR, "3ds_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
            self.f = open(self.path, "wb")
        self.f.write(chunk)
        self.f.flush()

    def finish(self):
        if self.f is None or self.done:
            return
        self.done = True
        self.f.close()
        with open(self.path, "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()
        print("[send] 3DS previous log saved: %s (%d lines). Last 30:" % (self.path, len(lines)), flush=True)
        for ln in lines[-30:]:
            print("    " + ln)
        sys.stdout.flush()


def sock_reader(sock, dead):
    logsink = LogSink()
    try:
        buf = b""
        while not dead.is_set():
            d = sock.recv(4096)
            if not d:
                break
            buf += d
            while len(buf) >= P.HEADER_LEN:
                try:
                    t, f, n = P.unpack_header(buf[:P.HEADER_LEN])
                except ValueError as e:
                    print("[send] bad header from peer: %s" % e, flush=True)
                    dead.set()
                    return
                if len(buf) < P.HEADER_LEN + n:
                    break
                payload = buf[P.HEADER_LEN:P.HEADER_LEN + n]
                buf = buf[P.HEADER_LEN + n:]
                if t == P.TYPE_LOG:
                    logsink.add(payload)
                    continue
                logsink.finish()  # the log is always sent before anything else
                if t == P.TYPE_KEYFRAME_REQ:
                    print("[send] KEYFRAME_REQ received (next keyframe within 1s)", flush=True)
    except OSError:
        pass
    finally:
        logsink.finish()
        dead.set()


def stop_ffmpeg(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_session(args, sock, dump):
    dead = threading.Event()
    threading.Thread(target=sock_reader, args=(sock, dead), daemon=True).start()
    sock.sendall(P.pack_message(P.TYPE_HELLO, 0, b"send_test.py"))
    proc = subprocess.Popen(build_ffmpeg_cmd(args), stdout=subprocess.PIPE)
    parser = AUParser()
    stats = Stats()
    got_key = False
    waiting_key = False
    alternating = args.layout == "alternating"
    index = -1  # AU index since ffmpeg start; alternating: even = left eye
    try:
        while not dead.is_set():
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                print("[send] ffmpeg ended", flush=True)
                return "eof"
            if dump:
                dump.write(data)
            for au in parser.feed(data):
                index += 1
                key = P.au_is_keyframe(au)
                right = alternating and index % 2 == 1
                if key and right:
                    # Must not happen with an even keyint and scenecut=0; drop until an
                    # IDR lands on a left frame again so the receiver's pairs stay aligned.
                    print("[send] IDR on a right-eye frame (#%d), dropping until aligned" % index,
                          flush=True)
                    waiting_key = True
                    stats.drops += 1
                    continue
                if not got_key and not key:
                    continue
                got_key = True
                if len(au) > P.MAX_PAYLOAD:
                    print("[send] AU too large (%d), dropped" % len(au), flush=True)
                    waiting_key = True
                    stats.drops += 1
                    continue
                if key:
                    waiting_key = False
                elif waiting_key:
                    stats.drops += 1
                    continue
                elif not select.select([], [sock], [], 0)[1]:
                    waiting_key = True  # send buffer backed up: drop until next keyframe
                    stats.drops += 1
                    continue
                flags = P.FLAG_KEYFRAME if key else 0
                if alternating:
                    flags |= P.FLAG_ALTERNATING | (P.FLAG_EYE_RIGHT if right else 0)
                sock.sendall(P.pack_message(P.TYPE_VIDEO_AU, flags, au))
                stats.frames += 1
                stats.bytes += len(au)
                stats.keys += 1 if key else 0
                stats.tick()
        return "dead"
    finally:
        stop_ffmpeg(proc)


def main():
    ap = argparse.ArgumentParser(description="Fake Vision Pro sender for 3DSStream")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=P.PORT)
    ap.add_argument("--source", default="pattern", help='"pattern" or a video file path')
    ap.add_argument("--sbs", action="store_true", help="input file is full side-by-side (left half = left eye)")
    ap.add_argument("--layout", choices=["alternating", "stacked"], default="alternating",
                    help="alternating: one 240x400 eye per frame at 2x fps (default); "
                         "stacked: 240x800 both eyes per frame")
    ap.add_argument("--fps", type=int, default=30, help="stereo pairs per second")
    ap.add_argument("--bitrate", default="1500k")
    ap.add_argument("--loop", action="store_true", help="loop the input file")
    ap.add_argument("--dump", metavar="FILE", help="write the raw Annex-B stream to FILE")
    args = ap.parse_args()

    dump = open(args.dump, "wb") if args.dump else None
    backoff = 0.5
    try:
        while True:
            try:
                sock = socket.create_connection((args.host, args.port), timeout=5)
            except OSError as e:
                print("[send] connect failed: %s (retry in %.1fs)" % (e, backoff), flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("[send] connected to %s:%d" % (args.host, args.port), flush=True)
            backoff = 0.5
            try:
                res = run_session(args, sock, dump)
            except OSError as e:
                print("[send] connection error: %s" % e, flush=True)
                res = "dead"
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            if res == "eof":
                break
            time.sleep(backoff)
    except KeyboardInterrupt:
        print("\n[send] interrupted", flush=True)
    finally:
        if dump:
            dump.close()


if __name__ == "__main__":
    main()
