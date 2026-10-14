# 3DSStream (New 3DS receiver)

Homebrew for the **New Nintendo 3DS / New 2DS XL** (custom firmware) that listens on TCP
port 7777, decodes the pre-rotated stereo H.264 stream with the hardware decoder (MVD) and
shows it in 3D on the top screen. Two layouts, chosen per AU by the VIDEO_AU flags:
**alternating** (default; one 240x400 eye per frame, L,R,L,R… at 2x fps, decoded straight
into the eye framebuffers, swapped after each right eye) and **stacked** (240x800, both
eyes per frame; kept for comparison). Protocol and video format: `../docs/ARCHITECTURE.md`.

## Build

You need devkitPro with the 3DS toolchain:

```sh
sudo dkp-pacman -S 3ds-dev
export DEVKITPRO=/opt/devkitpro DEVKITARM=/opt/devkitpro/devkitARM
make            # -> 3DSStream.3dsx
```

### Installable .cia

`makerom` and `bannertool` are not part of devkitPro. Fetch them into `3ds/.tools/`
(no sudo; `.tools/` should stay out of git):

```sh
./scripts/get_tools.sh   # makerom (Project_CTR release) + bannertool (diasurgical fork, built locally)
make cia                 # -> 3DSStream.cia
```

The Makefile uses `makerom` / `bannertool` from `PATH` if they are there, otherwise
`.tools/`. CIA metadata: product code `CTR-P-S3DS`, unique id `0xF3D5A`
(title id `000400000F3D5A00`, homebrew range). The spec is `resource/app.rsf`; it must keep
`mvd:STD` + the mvd module dependency, or hardware decoding fails in the .cia build.

## Install and run

- **.cia (HOME Menu icon):** copy `3DSStream.cia` to the SD card and install it with **FBI**
  (or use FBI's *Remote Install* / a QR code). Launch it from the HOME Menu.
- **.3dsx:** copy `3DSStream.3dsx` to `sdmc:/3ds/` and start it from the Homebrew Launcher,
  or push it over WiFi: open the Homebrew Launcher, press Y (netloader), then
  `3dslink -a <3DS IP> 3DSStream.3dsx`.

Old 3DS models are refused at startup (no MVD hardware decoder).

## Use

The bottom screen shows the 3DS IP and port, connection state, sender name, decoded FPS,
receive bitrate, dropped AUs, decode errors and the 3D slider position.

| Button | Action |
|---|---|
| START | exit |
| Y | ask the sender for a keyframe |
| X | swap left/right eyes (debugging) |

Test without the Vision Pro, from the Mac (same WiFi):

```sh
python3 tools/send_test.py --host <3DS IP>                    # alternating
python3 tools/send_test.py --host <3DS IP> --layout stacked   # 240x800
```

## Code map

- `source/main.c`: setup, main loop (decode + present), status screen.
- `source/net.c`: soc:U, network thread, S3DS framing, AU queue and drop/keyframe policy.
- `source/decode.c`: MVD wrapper (Annex-B NAL split, cache maintenance, render to a
  240x800 BGR565 buffer). It has a comment on the 800x240 GPU fallback.
- `resource/`: CIA spec, icon, banner, banner sound.
