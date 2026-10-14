#!/bin/sh
# Convert an iPhone / Vision Pro spatial video (MV-HEVC .MOV) for the 3DS:
#   <name>_sbs.mp4  side-by-side 1600x480 (5:3 per eye), for tools/send_test.py --sbs
#   <name>_3ds.mp4  800x240 SBS H.264 + AAC, for local playback with topos on the 3DS
# Optionally uploads the _3ds.mp4 to the SD card over FTP (ftpd on the 3DS).
#
#   tools/spatial_to_3ds.sh Video/IMG_1234.MOV [3DS_IP[:PORT]]
set -eu
IN="$1"
FTP="${2:-}"
BASE="${IN%.*}"

# Each eye is cropped from 16:9 to the 3DS's 5:3 (1080 * 5/3 = 1800 wide), centered.
ffmpeg -v error -stats -y -i "$IN" -filter_complex \
  "[0:v:view:0]crop=ih*5/3:ih,scale=800:480[l];[0:v:view:1]crop=ih*5/3:ih,scale=800:480[r];[l][r]hstack[v]" \
  -map "[v]" -map "0:a:0?" -c:v libx264 -crf 18 -preset fast -c:a aac "${BASE}_sbs.mp4"

# topos (New 3DS MVD) settings: 800x240, High@3.1, single slice (x264 default without zerolatency).
ffmpeg -v error -stats -y -i "${BASE}_sbs.mp4" \
  -vf "scale=800:240:flags=spline,format=yuv420p,setsar=1" \
  -c:v libx264 -preset slow -crf 16 -profile:v high -level 3.1 -fps_mode cfr -x264-params "ref=4:bframes=4" \
  -c:a aac -b:a 128k -ac 2 -ar 48000 "${BASE}_3ds.mp4"

echo "Made ${BASE}_sbs.mp4 and ${BASE}_3ds.mp4"

if [ -n "$FTP" ]; then
  case "$FTP" in *:*) ;; *) FTP="$FTP:5000" ;; esac
  curl --ftp-create-dirs -T "${BASE}_3ds.mp4" "ftp://$FTP/videos/$(basename "${BASE}_3ds.mp4")"
  echo "Uploaded to sdmc:/videos/ — play it with topos"
fi
