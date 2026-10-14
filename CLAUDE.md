# 3DSStream: notes for Claude

Read `README.md` and `docs/ARCHITECTURE.md` first. ARCHITECTURE is the contract between this repo
(`3ds/` receiver, `tools/`) and the sender library **S3DSKit**, a separate repo at `~/Apps/S3DSKit`
that keeps a copy as `docs/PROTOCOL.md`. Any wire or format change updates ARCHITECTURE here, the
receiver, the tools, S3DSKit and its PROTOCOL.md copy.

## Hard rules (learned on hardware, each one crashed or broke the 3DS)

- H.264 must be **exactly one slice per frame**. Multi-slice frames crash the `mvd` sysmodule.
  x264 needs `sliced-threads=0:slices=1`; VideoToolbox needs `NumberOfSlices = 1` (S3DSKit does it).
- Alternating 240×400 pre-rotated eyes, IDR only on LEFT frames, Baseline, no B-frames,
  ≤ 256 KiB per AU, 1.5 Mbps default.
- Feed SPS/PPS to MVD once (dedupe in `3ds/source/decode.c`).
- The receiver queue (`net.c` MAX_PENDING 12) and the audio rate controller (`audio.c`) were tuned
  on the device. Don't tighten them without a hardware test.
- HOME banner: raw `.cgfx` to `bannertool -ci` (never pre-compress). HOME clips with an oval and
  spins the model 360°.

## Build and verify

```sh
# 3DS (devkitPro in /opt/devkitpro; makerom/bannertool in 3ds/.tools via scripts/get_tools.sh)
cd 3ds && export DEVKITPRO=/opt/devkitpro DEVKITARM=/opt/devkitpro/devkitARM PATH="$PWD/.tools:$PATH"
make && make cia && python3 scripts/verify_cia_banner.py 3DSStream.cia resource/banner.cgfx

# Tools smoke test (fake 3DS + fake sender on localhost)
python3 tools/recv_sim.py --no-play --out /tmp/x.h264 & python3 tools/send_test.py --host 127.0.0.1
```

Zero warnings in all builds. Test on the device by serving the CIA with `tools/serve_cia.sh`
(FBI → Remote Install → QR). After a crash, the 3DS uploads the previous session's log on the
next connection (`tools/logs/`).

## Device workflow (New 3DS XL, Luma3DS + FBI)

- Install a build: `tools/serve_cia.sh`, then `open 3ds/build/cia_qr.png`. On the 3DS: FBI → Remote
  Install → Scan QR Code. Afterwards stop the server with `pkill -f "http.server 8080"`.
- The 3DS's IP is shown on its bottom screen. Stream to it with
  `python3 tools/send_test.py --host <IP> [--source X_sbs.mp4 --sbs --loop]`. The user's
  spatial video, proven on the device:
  `python3 tools/send_test.py --host <IP> --source Video/<name>_sbs.mp4 --sbs --loop --bitrate 1500k`
  (`Video/` is gitignored; new spatial videos go through `tools/spatial_to_3ds.sh` first).
- Files to the SD card: the user opens ftpd (port 5000), then
  `curl --ftp-create-dirs -T file ftp://<IP>:5000/<path>`. Converted spatial videos go in
  `/videos/` and play locally with topos.
- After a crash: ask for a photo of the Luma exception screen (process, PC, FAR). The previous
  session's log comes back on the next connection, saved by send_test in `tools/logs/`. To fetch
  the log without sending video (and crashing again), connect and only read LOG (0x20) messages.
- `sudo` installs (devkitPro packages) have to be run by the user in Terminal.app; the `!`
  prefix gives sudo no TTY.

## Visual assets

For the banner, the logo or anything visual: regenerate previews first
(`3ds/scripts/make_banner_model.py --previews`), open `3ds/preview/banner.usdz` in Quick Look
(`qlmanage -p`), and **build/serve the CIA only after the user approves**. USDZ is the only preview
the user finds realistic: they have no 3D display.

## Status (2026-10-14)

Published: github.com/NSCabezon/3DSStream (release v1.0.0 with the CIA). Sender library:
github.com/NSCabezon/S3DSKit (release 0.1.0), local checkout `~/Apps/S3DSKit`.

Confirmed on hardware: the receiver (video + Opus audio + 3D HOME banner), the tools, and S3DSKit
streaming from a real Vision Pro app. Fixed after that first real-app test: the right eye showed an
older moment than the left. The cause was the VideoToolbox SPS lacking bitstream_restriction; the
receiver now patches every SPS (`sps.c`, commit bf56fe5) and S3DSKit rewrites its own.

Open:
- In that real-app test the 3DS showed ~88 decoded fps (44 pairs) at 2.4 Mbps with drops, and audio
  arriving faster than real time (rate pegged at +2 %, hundreds of audio drops). The app had been
  built against the pre-split S3DSKit. S3DSKit now has a deadline throttle and audio-rate stats
  (`audioInputFramesPerSecond`, `audioPacketsPerSecond`). Retest with a fresh build before
  changing the receiver.
- Whether the Vision Pro's hardware encoder honours `NumberOfSlices = 1` (a DEBUG build asserts if
  not).
- Don't modify the user's app repos without their OK.

## Delegation

- Delegation to `fast-worker` is fine for: tools/ scripts, docs, banner art.
- Not for: `3ds/source` (MVD, cache maintenance, threads). Use `general-purpose` or
  `deep-reasoner` there.
