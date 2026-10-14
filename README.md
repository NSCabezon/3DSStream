<p align="center"><img src="branding/logo_320.png" width="160" alt="3D Streaming logo"></p>

# 3D Streaming

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: New Nintendo 3DS](https://img.shields.io/badge/platform-New%203DS-red)
![Sender: visionOS · macOS](https://img.shields.io/badge/sender-visionOS%20%C2%B7%20macOS-lightgrey)
[![Latest release](https://img.shields.io/github/v/release/NSCabezon/3DSStream)](https://github.com/NSCabezon/3DSStream/releases/latest)

Stream stereoscopic 3D video and audio to a **New Nintendo 3DS**, shown glasses-free on its
top screen. The sender is a visionOS app using the **[S3DSKit](https://github.com/NSCabezon/S3DSKit)**
Swift package, or a Mac script for testing and for playing your own spatial videos.

```
 visionOS app + S3DSKit ──┐                                 ┌── New 3DS: "3D Streaming" (CIA)
                          ├── WiFi, TCP :7777, H.264 + Opus ┤   MVD hardware decoder → both eyes
 Mac: tools/send_test.py ─┘                                 └── ndsp audio
```

Measured on a New 3DS XL: **29.9 stereo pairs/s** (60 decoded frames/s), 0 decode errors,
1.5 Mbps video + 96 kbps Opus audio without stutter.

| Directory | What |
|---|---|
| [`3ds/`](3ds/README.md) | The 3DS receiver (C, libctru). Builds `3DSStream.cia` / `.3dsx`. |
| [`tools/`](tools/README.md) | Mac tools: test sender, fake 3DS, QR install server, spatial-video converter. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Video format, wire protocol, hardware findings. Read this before changing anything on the wire. |
| `branding/` | Logo as an Icon Composer `.icon` (depth layers). |

## Requirements

- **New** 3DS / New 3DS XL / New 2DS XL with custom firmware (Luma3DS) and FBI. The original 3DS
  has no hardware H.264 decoder and is not supported.
- `sdmc:/3ds/dspfirm.cdc` on the SD card for audio (dump it once with the DSP1 homebrew).
- The 3DS and the sender on the same WiFi network.
- To build: macOS, devkitPro with `3ds-dev` and `3ds-libopus`, Python 3,
  ffmpeg (Homebrew) and qrencode (`brew install qrencode`).

## Quick start

1. **Install the receiver on the 3DS.** Either way, "3D Streaming" then shows up on the HOME Menu.
   - **No build needed:** download `3DSStream.cia` from the
     [latest release](https://github.com/NSCabezon/3DSStream/releases/latest), copy it to the SD
     card and install it with FBI (or use the `.3dsx` with the Homebrew Launcher).
   - **From source:**
     ```sh
     cd 3ds && make cia && cd ..
     tools/serve_cia.sh            # prints a QR code
     ```
     On the 3DS: FBI → Remote Install → Scan QR Code.
2. **Open it.** After 1.5 s it starts listening; the bottom screen shows its IP.
3. **Send something from the Mac**
   ```sh
   python3 tools/send_test.py --host <3DS IP>                     # test pattern + beeps
   python3 tools/send_test.py --host <3DS IP> --source movie_sbs.mp4 --sbs --loop
   ```
   Raise the 3D slider. X swaps the eyes, Y asks for a keyframe, START exits.

## Your spatial videos (iPhone / Vision Pro)

```sh
tools/spatial_to_3ds.sh Video/IMG_1234.MOV [<3DS IP>]
```

This makes `IMG_1234_sbs.mp4`, which you stream with `send_test.py --sbs`, and
`IMG_1234_3ds.mp4` (800×240 SBS) for local playback on the 3DS with
[topos](https://github.com/mockmodular/topos_3ds-video-player). With an IP, the second file is
uploaded to `sdmc:/videos/` via ftpd running on the 3DS (port 5000).

## Streaming from a visionOS app

Add **[S3DSKit](https://github.com/NSCabezon/S3DSKit)** (separate repository) to your app with Swift
Package Manager. If your app already renders one texture per eye, you need one line per frame
and one per audio buffer:

```swift
streamer.connect(host: "<3DS IP>", port: 7777)
streamer.submit(left: leftEye, right: rightEye, commandBuffer: cb)   // any size, letterboxed to 400x240
streamer.submitAudio(samples: buf, channels: 1, sampleRate: 48_000)  // safe on the audio thread
```

The defaults are the settings proven on hardware. The [S3DSKit README](https://github.com/NSCabezon/S3DSKit#readme) covers
Metal/Compositor Services, RealityKit and threading.

## What we learned on the hardware

These are the things that broke and why; the details are in
[ARCHITECTURE.md → Hardware findings](docs/ARCHITECTURE.md).

- **One slice per frame, always.** The 3DS `mvd` decoder cannot handle multi-slice H.264. A
  multi-slice frame returns INCOMPLETEPROCESSING / `0x17005`, and the next IDR **crashes the mvd
  system module**. On the Luma screen that shows as `mvd (0004013000004102)`, data abort, PC
  `0010BA4A`. Both x264 `-tune zerolatency` (sliced threads) and VideoToolbox (2–5 slices by
  default) do this, so every sender forces a single slice.
- **240×400 per eye, alternating L/R frames, pre-rotated 90°.** MVD then writes straight into the
  3DS framebuffer layout with no CPU rotation. Every IDR is a left frame.
- **SPS/PPS go to MVD once.** Senders repeat them, and the receiver skips duplicates.
- **WiFi arrives in bursts.** The receiver tolerates 12 queued frames (200 ms). With 2, it flushed
  to the next keyframe on every burst and showed ~1 fps.
- **1.5 Mbps.** At 2.5 Mbps the 3DS's WiFi stalled.
- **Audio jitter is absorbed by steering the playback rate** (±2 %) toward 200 ms of buffer, not
  by dropping or inserting audio.
- **HOME Menu banners** are clipped by an oval mask and spun 360° by the system. The banner is a
  static, double-sided CGFX badge (`3ds/scripts/make_banner_model.py`; preview it in Quick Look
  with `--previews` → `3ds/preview/banner.usdz`).

## Troubleshooting

| Symptom | Look at |
|---|---|
| Luma exception in `mvd` right after connecting | The sender is producing multi-slice frames. Check with ffprobe/NAL count. |
| Picture, but ~1 fps | Queue flushing: WiFi bursts or a slow SD log. The bottom screen shows dropped AUs. |
| "Audio: no DSP firmware" | `sdmc:/3ds/dspfirm.cdc` is missing. |
| Audio stutters | The bottom-screen `Audio:` line: level ≈ 200 ms, U (underruns) and D (drops) ≈ 0. |
| Anything else | The 3DS writes `sdmc:/3ds/3DSStream/log.txt` and uploads the previous session's log to `send_test.py` on the next connection, saved in `tools/logs/`. |

## License

MIT, see [LICENSE](LICENSE). The bundled fonts in `3ds/resource/fonts/` are under the SIL Open
Font License (see the OFL files next to them).

This project is not affiliated with or endorsed by Nintendo. "Nintendo 3DS" and "New Nintendo 3DS"
are trademarks of Nintendo. It runs on consoles with custom firmware; use it on hardware you own.
