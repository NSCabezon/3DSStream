# 3DSStream Mac test tools

Python 3 stdlib only, plus `ffmpeg`/`ffplay` (Homebrew, `/opt/homebrew/bin`).
Protocol and video format: see `../docs/ARCHITECTURE.md`.

## recv_sim.py — fake 3DS

    python3 tools/recv_sim.py [--port 7777] [--out recv.h264] [--no-play]

Listens on TCP, sends KEYFRAME_REQ on connect, validates headers, drops non-key AUs until
the first keyframe, prints HELLO name and per-second stats. Plays the stream in ffplay
de-rotated to side-by-side 800x240 (scaled 2x). `--out` saves the received raw H.264.

## send_test.py — fake Vision Pro

    python3 tools/send_test.py --host 127.0.0.1                      # test pattern
    python3 tools/send_test.py --host 192.168.1.50 --source movie.mp4 --loop
    python3 tools/send_test.py --host 127.0.0.1 --source sbs.mp4 --sbs
    python3 tools/send_test.py --host 127.0.0.1 --layout stacked     # 240x800 (default: alternating 240x400 eyes at 2x fps)

Options: `--port 7777 --fps 30 --bitrate 1500k --loop --dump FILE`.
Pattern = testsrc2 with 10 px disparity plus a moving red box (16 px disparity).
Without `--sbs` a file is shown flat (same image in both eyes). Reconnects with backoff.

## Typical loops

- Sender app (simulator) -> `recv_sim.py` on 127.0.0.1:7777.
- `send_test.py --host <3DS IP>` to test the 3DS receiver alone.
- Both tools together on localhost for a smoke test.

## serve_cia.sh — install the CIA by QR

    tools/serve_cia.sh        # PORT=8080 IP=<mac ip> overridable

Serves `3ds/3DSStream.cia` over HTTP and prints a QR. On the 3DS: FBI > Remote Install >
Scan QR Code. Mac and 3DS must be on the same WiFi.
