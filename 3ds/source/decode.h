#pragma once

#include <3ds.h>

// Both layouts are pre-rotated so decoded BGR565 is in framebuffer layout (ARCHITECTURE):
//  - DEC_LAYOUT_ALTERNATING: each frame is one eye, 240x400.
//  - DEC_LAYOUT_STACKED: each frame is 240x800 (left eye rows 0..399, right 400..799).
#define DEC_EYE_BYTES   (240 * 400 * 2)   // 192000, one top framebuffer

typedef enum {
    DEC_LAYOUT_STACKED     = 0,
    DEC_LAYOUT_ALTERNATING = 1,
} DecLayout;

// Diagnostic decode strategies (selected on the start screen).
typedef enum {
    DEC_MODE_A,  // NAL by NAL, MVD writes straight into the eye framebuffer
    DEC_MODE_B,  // NAL by NAL, MVD writes a per-eye linear buffer, memcpy to framebuffer
    DEC_MODE_X,  // topos-style: whole AU per ProcessVideoFrame, marker-pixel detection, BGR565
    DEC_MODE_Y,  // as X but MVD outputs ABGR32, converted to BGR565 on the CPU
} DecMode;

typedef enum {
    DEC_NO_FRAME,   // AU consumed (e.g. parameter sets only), nothing new
    DEC_FRAME,      // a new picture is in the destination
    DEC_ERROR,      // MVD rejected the stream; caller should request a keyframe
} DecResult;

typedef struct {
    Result      last_error;
    const char* last_step;   // which call produced last_error
    u32         workbuf_size;
    u8          level_idc;   // level MVD was sized for (H.264 level_idc, e.g. 31)
    bool        ready;
} DecInfo;

bool        decode_init(DecMode mode);   // allocates buffers; MVD starts at the first keyframe
void        decode_exit(void);
const char* decode_mode_name(DecMode mode);

// Decodes one access unit.
//  alternating: `dst` is the eye's top framebuffer; on DEC_FRAME it holds the picture
//               (written by MVD in mode A, copied there in B/X/Y; cache already flushed).
//  stacked:     `dst` is ignored; on DEC_FRAME the BGR565 240x800 picture is in
//               decode_stacked_output().
DecResult   decode_au(const u8* au, u32 size, DecLayout layout, bool keyframe, int eye, u8* dst);
const u8*   decode_stacked_output(void);
void        decode_get_info(DecInfo* out);
