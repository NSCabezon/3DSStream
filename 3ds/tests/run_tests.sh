#!/bin/sh
# Host tests for the SPS rewriter (source/sps.c). Needs cc and ffmpeg (Homebrew build, for
# libx264 and h264_videotoolbox). Run from anywhere: 3ds/tests/run_tests.sh, or `make test-host`.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../source"
OUT="$HERE/../build/tests"
mkdir -p "$OUT"

cc -std=gnu11 -Wall -Wextra -Wshadow -Werror -O2 -o "$OUT/sps_test" "$HERE/sps_test.c" "$SRC/sps.c"
"$OUT/sps_test"

FF="ffmpeg -nostdin -hide_banner -loglevel error -y"
SRCVID="testsrc2=s=240x400:r=60"

# Checks that IN and its rewritten copy decode to identical frames, and prints the SPS fields
# of the rewritten stream that matter for zero output delay.
check_file() {
    name=$1 in=$2
    out="$OUT/${name}_rewritten.h264"
    "$OUT/sps_test" "$in" "$out"
    a=$(ffmpeg -nostdin -v error -i "$in" -f framemd5 - | grep -v '^#' | md5)
    b=$(ffmpeg -nostdin -v error -i "$out" -f framemd5 - | grep -v '^#' | md5)
    [ "$a" = "$b" ] || { echo "FAIL $name: decoded frames differ"; exit 1; }
    hdr=$(ffmpeg -nostdin -v trace -f h264 -i "$out" -c copy -bsf:v trace_headers -frames:v 1 -f null - 2>&1)
    echo "$hdr" | grep -q "bitstream_restriction_flag *1 = 1" || { echo "FAIL $name: no bitstream_restriction"; exit 1; }
    echo "$hdr" | grep -q "max_num_reorder_frames *1 = 0" || { echo "FAIL $name: max_num_reorder_frames != 0"; exit 1; }
    echo "$hdr" | grep -E "pic_order_cnt_type|max_num_ref_frames|max_dec_frame_buffering" | head -3 | sed 's/^.*\] */    /'
    echo "  $name: frames identical, bitstream_restriction_flag=1, max_num_reorder_frames=0"
}

# x264 (already has the restriction: must pass through unchanged).
$FF -f lavfi -i "$SRCVID" -frames:v 120 -c:v libx264 -profile:v baseline -preset ultrafast \
    -tune zerolatency -x264-params "repeat-headers=1:keyint=60:sliced-threads=0:slices=1" -f h264 "$OUT/x264.h264"
check_file x264 "$OUT/x264.h264"

# VideoToolbox via ffmpeg: no VUI at all, POC type 0, several refs -> minimal VUI added.
$FF -f lavfi -i "$SRCVID" -frames:v 120 -c:v h264_videotoolbox -profile:v baseline -g 60 -f h264 "$OUT/vt.h264"
check_file vt "$OUT/vt.h264"

echo "all host tests passed"
