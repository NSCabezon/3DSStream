#!/usr/bin/env python3
"""Fake 3DS receiver: listens for an S3DS sender, validates framing, and shows the
de-rotated side-by-side stream in ffplay. Handles both layouts (VIDEO_AU flags bit2):
stacked 240x800 frames, or alternating 240x400 eye frames (L,R,L,R...)."""
import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s3ds_proto as P  # noqa: E402

FFPLAY = "/opt/homebrew/bin/ffplay"
if not os.path.exists(FFPLAY):
    FFPLAY = "ffplay"

VF_STACKED = ("split[a][b];"
              "[a]crop=240:400:0:0,transpose=cclock[l];"
              "[b]crop=240:400:0:400,transpose=cclock[r];"
              "[l][r]hstack,scale=1600:480")
# Alternating: even frames are left, odd are right (the stream fed to ffplay always starts
# at a left IDR). Pair them back up into one SBS picture per stereo pair.
VF_ALTERNATING = ("split[a][b];"
                  "[a]select='not(mod(n,2))',setpts=N/(30*TB),transpose=cclock[l];"
                  "[b]select='mod(n,2)',setpts=N/(30*TB),transpose=cclock[r];"
                  "[l][r]hstack,scale=1600:480")

stop = threading.Event()


class Sink:
    def __init__(self, out_path, play):
        self.lock = threading.Lock()
        self.out = open(out_path, "wb") if out_path else None
        self.play = None
        self.want_play = play
        self.layout = None

    def _start_player(self, alternating):
        vf = VF_ALTERNATING if alternating else VF_STACKED
        self.play = subprocess.Popen(
            [FFPLAY, "-loglevel", "warning", "-fflags", "nobuffer", "-flags", "low_delay",
             "-framedrop", "-probesize", "32", "-window_title", "recv_sim (3DS)",
             "-vf", vf, "-f", "h264", "-i", "-"],
            stdin=subprocess.PIPE)

    def write(self, au, alternating):
        with self.lock:
            if self.layout is None:
                self.layout = alternating
                print("[recv] layout: %s" % ("alternating 240x400" if alternating else "stacked 240x800"),
                      flush=True)
                if self.want_play:
                    self._start_player(alternating)
            elif self.layout != alternating:
                print("[recv] layout changed mid-run; restart recv_sim to view it", flush=True)
            if self.out:
                self.out.write(au)
            if self.play and self.play.stdin:
                try:
                    self.play.stdin.write(au)
                    self.play.stdin.flush()
                except (BrokenPipeError, OSError):
                    print("[recv] ffplay closed", flush=True)
                    self.play = None

    def close(self):
        with self.lock:
            if self.out:
                self.out.close()
            if self.play:
                try:
                    self.play.stdin.close()
                except OSError:
                    pass
                try:
                    self.play.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.play.terminate()


class Conn:
    def __init__(self, sock, addr, sink):
        self.sock, self.addr, self.sink = sock, addr, sink
        self.replaced = threading.Event()
        self.t0 = time.time()
        self.frames = self.bytes = self.keys = self.drops = 0
        self.eye_errors = 0
        self.apkts = 0
        self.abad = 0
        self.last_ts = None

    def tick(self):
        now = time.time()
        dt = now - self.t0
        if dt >= 1.0:
            print("[recv] %.1f fps  %d kbps  keyframes %d  dropped %d  audio %.1f pkt/s  bad audio %d"
                  % (self.frames / dt, self.bytes * 8 / dt / 1000, self.keys, self.drops,
                     self.apkts / dt, self.abad), flush=True)
            self.t0 = now
            self.frames = self.bytes = self.keys = self.drops = self.apkts = 0

    def read(self, n):
        buf = b""
        while len(buf) < n:
            if stop.is_set() or self.replaced.is_set():
                raise EOFError("closed")
            try:
                d = self.sock.recv(n - len(buf))
            except socket.timeout:
                self.tick()
                continue
            if not d:
                raise EOFError("peer closed")
            buf += d
        return buf

    def run(self):
        print("[recv] connection from %s:%d" % self.addr, flush=True)
        try:
            self.sock.settimeout(1.0)
            self.sock.sendall(P.pack_message(P.TYPE_KEYFRAME_REQ))
            got_key = False
            next_eye = 0  # alternating: expected eye of the next AU (0 = left)
            while True:
                hdr = self.read(P.HEADER_LEN)
                try:
                    t, flags, n = P.unpack_header(hdr)
                except ValueError as e:
                    print("[recv] protocol error: %s -- closing" % e, flush=True)
                    return
                payload = self.read(n) if n else b""
                if t == P.TYPE_HELLO:
                    print("[recv] HELLO: %s" % payload.decode("utf-8", "replace"), flush=True)
                elif t == P.TYPE_VIDEO_AU:
                    key = bool(flags & P.FLAG_KEYFRAME)
                    alternating = bool(flags & P.FLAG_ALTERNATING)
                    eye = 1 if flags & P.FLAG_EYE_RIGHT else 0
                    if not got_key and not key:
                        self.drops += 1
                        self.tick()
                        continue
                    if alternating:
                        if key and eye != 0:
                            print("[recv] ERROR: keyframe on a right-eye frame", flush=True)
                        if key:
                            next_eye = 0  # sender may have dropped frames before an IDR
                        if eye != next_eye:
                            self.eye_errors += 1
                            print("[recv] eye sequence error (got %s, expected %s), total %d"
                                  % ("R" if eye else "L", "R" if next_eye else "L", self.eye_errors),
                                  flush=True)
                        next_eye = 1 - eye
                    got_key = True
                    self.frames += 1
                    self.bytes += n
                    self.keys += 1 if key else 0
                    self.sink.write(payload, alternating)
                elif t == P.TYPE_AUDIO:
                    # u64 BE timestamp (us) + one Opus packet; timestamps must increase.
                    ok = 8 < n <= P.AUDIO_MAX_PAYLOAD
                    if ok:
                        ts = int.from_bytes(payload[:8], "big")
                        ok = self.last_ts is None or ts > self.last_ts
                        self.last_ts = ts
                    if not ok:
                        self.abad += 1
                    self.apkts += 1
                    self.bytes += n
                elif t == P.TYPE_LOG:
                    pass  # 3DS->sender only; nothing to do in the fake receiver
                else:
                    print("[recv] unknown type 0x%02x, ignored" % t, flush=True)
                self.tick()
        except (EOFError, OSError) as e:
            if not self.replaced.is_set():
                print("[recv] connection ended: %s" % e, flush=True)
        finally:
            try:
                self.sock.close()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Fake 3DS receiver for 3DSStream")
    ap.add_argument("--port", type=int, default=P.PORT)
    ap.add_argument("--out", metavar="FILE", help="write received AUs to a raw .h264 file")
    ap.add_argument("--no-play", action="store_true", help="do not launch ffplay")
    args = ap.parse_args()

    sink = Sink(args.out, not args.no_play)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    srv.settimeout(0.5)
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    print("[recv] listening on :%d" % args.port, flush=True)
    cur = None
    try:
        while not stop.is_set():
            try:
                s, addr = srv.accept()
            except socket.timeout:
                continue
            if cur:
                cur.replaced.set()
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            cur = Conn(s, addr, sink)
            threading.Thread(target=cur.run, daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        time.sleep(0.2)
        srv.close()
        sink.close()
        print("[recv] bye", flush=True)


if __name__ == "__main__":
    main()
