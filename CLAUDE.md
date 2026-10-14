# 3DSStream: notes for Claude

Read `README.md` and `docs/ARCHITECTURE.md` first. ARCHITECTURE is the contract between the three
parts (`3ds/`, `visionos/`, `tools/`). Any wire or format change updates ARCHITECTURE and every
part in the same change.

## Hard rules (learned on hardware, each one crashed or broke the 3DS)

- H.264 must be **exactly one slice per frame**. Multi-slice frames crash the `mvd` sysmodule.
  x264 needs `sliced-threads=0:slices=1`; VideoToolbox needs `NumberOfSlices = 1`. Never remove
  the per-frame slice check in `H264Encoder.swift`.
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

# S3DSKit
cd visionos && xcodebuild test -scheme S3DSKit -destination 'platform=visionOS Simulator,name=Apple Vision Pro'

# Tools smoke test (fake 3DS + fake sender on localhost)
python3 tools/recv_sim.py --no-play --out /tmp/x.h264 & python3 tools/send_test.py --host 127.0.0.1
```

Zero warnings in all builds. Test on the device by serving the CIA with `tools/serve_cia.sh`
(FBI → Remote Install → QR). After a crash, the 3DS uploads the previous session's log on the
next connection (`tools/logs/`).

## Delegation

- Delegation to `fast-worker` is fine for: tools/ scripts, docs, banner art.
- Not for: `3ds/source` (MVD, cache maintenance, threads), `H264Encoder` / `StreamEngine`
  (concurrency, VideoToolbox). Use `general-purpose` or `deep-reasoner` there.
