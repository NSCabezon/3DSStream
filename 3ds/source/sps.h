#pragma once

// H.264 SPS rewriter: makes a decoder output every picture as soon as it is decoded.
//
// Some encoders (VideoToolbox) write an SPS whose VUI lacks bitstream_restriction. A decoder
// must then assume pictures may be reordered and can hold decoded pictures in its DPB before
// output. With the alternating L/R layout that output delay showed the right eye from an
// older moment than the left. The rewriter parses the SPS through the end of the VUI, copies
// every bit before bitstream_restriction_flag unchanged and writes a restriction block with
// max_num_reorder_frames = 0, max_dec_frame_buffering = max(1, max_num_ref_frames). Without
// a VUI it adds a minimal one. Same design as S3DSKit's SPSRewriter.swift.
//
// Plain C (no libctru), fixed-size buffers, no allocation: also built by the host tests.

#include <stddef.h>
#include <stdint.h>

#define SPS_MAX_BYTES 256   // largest SPS NAL handled (real ones are ~10-40 bytes)
#define SPS_OUT_BYTES (SPS_MAX_BYTES + 32)

typedef enum {
    SPS_REWRITTEN,      // `out` holds the patched SPS
    SPS_ALREADY_OK,     // already has bitstream_restriction with max_num_reorder_frames = 0
    SPS_PARSE_FAILED,   // not understood; use the original unchanged
} SpsResult;

typedef struct {
    uint32_t profile_idc, level_idc;
    uint32_t pic_order_cnt_type;
    uint32_t max_num_ref_frames;
    uint32_t width_mbs, height_map_units;   // pic_width_in_mbs, pic_height_in_map_units
    int      has_vui, has_restriction;
    uint32_t max_num_reorder_frames, max_dec_frame_buffering;   // valid if has_restriction
} SpsInfo;

// Parses one SPS NAL (header byte included, emulation prevention present, no start code).
// Returns 0 on success.
int sps_parse(const uint8_t* nal, size_t len, SpsInfo* info);

// Rewrites `in` into `out` (capacity out_cap, SPS_OUT_BYTES is always enough) when needed.
// `out`/`out_len` are only written for SPS_REWRITTEN. `info` (optional) describes the input.
SpsResult sps_rewrite(const uint8_t* in, size_t in_len, uint8_t* out, size_t out_cap,
                      size_t* out_len, SpsInfo* info);
