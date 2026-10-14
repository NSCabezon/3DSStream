#include "decode.h"

#include <stdio.h>
#include <string.h>

#include "log.h"

#define IN_BUF_SIZE      0x100000
#define OUT_BUF_SIZE     (240 * 800 * 2)   // one stacked BGR565 frame
#define MIN_LEVEL_IDC    31                // never size MVD below level 3.1
#define RENDER_RETRIES   1000              // x 100 us
#define PARAM_MAX        256

// FALLBACK (ARCHITECTURE "Fallback"): if MVD cannot handle the pre-rotated layouts at all,
// switch the sender to plain 800x240 SBS (or 400x240 alternating unrotated), decode into
// `out_buf` at those dims, then upload it as GPU textures (citro3d/citro2d) and draw each
// 400x240 eye to the left / right top-screen render targets instead of writing the
// framebuffers directly.

static DecMode mode;
static u8* in_buf;
static u8* out_buf[2];       // per-eye MVD targets (mode B); stacked uses [0]
static const u8* stacked_out;
// Last SPS / PPS handed to MVD (modes A/B). Senders repeat them before every IDR, but MVD
// must see each parameter set only once: re-feeding identical SPS/PPS every keyframe was
// suspected of crashing the mvd sysmodule.
static u8  last_param[2][PARAM_MAX];
static u32 last_param_len[2];
static MVDSTD_Config cfg;
static DecLayout cur_layout;
static u32 out_w, out_h;
static u32 au_index;
static DecInfo info = { .last_step = "-" };

static void set_error(const char* step, Result r)
{
    info.last_step = step;
    info.last_error = r;
    log_line("ERROR %s ret=0x%08lx", step, (unsigned long)r);
}

const char* decode_mode_name(DecMode m)
{
    switch (m) {
    case DEC_MODE_A: return "A direct-to-fb";
    default:         return "B eye-buf+memcpy";
    }
}

static void log_buf(const char* name, const void* p, u32 size)
{
    log_line("buf %-8s va=%08lx pa=%08lx size=%lu", name, (unsigned long)(u32)p,
             (unsigned long)osConvertVirtToPhys(p), (unsigned long)size);
}

bool decode_init(DecMode m)
{
    mode = m;
    // Linear memory: MVD accesses buffers by physical address.
    in_buf     = linearMemAlign(IN_BUF_SIZE, 0x40);
    out_buf[0] = linearMemAlign(OUT_BUF_SIZE, 0x40);
    out_buf[1] = linearMemAlign(OUT_BUF_SIZE, 0x40);
    if (!in_buf || !out_buf[0] || !out_buf[1]) {
        set_error("linearAlloc", -1);
        return false;
    }
    log_buf("in_buf", in_buf, IN_BUF_SIZE);
    log_buf("out0", out_buf[0], OUT_BUF_SIZE);
    log_buf("out1", out_buf[1], OUT_BUF_SIZE);
    log_line("linearSpaceFree=%lu", (unsigned long)linearSpaceFree());
    return true;
}

static void mvd_stop(void)
{
    if (info.ready) {
        log_line("mvdstdExit");
        mvdstdExit();
    }
    info.ready = false;
}

void decode_exit(void)
{
    mvd_stop();
    if (in_buf) linearFree(in_buf);
    for (int i = 0; i < 2; i++)
        if (out_buf[i]) linearFree(out_buf[i]);
    in_buf = out_buf[0] = out_buf[1] = NULL;
}

const u8* decode_stacked_output(void) { return stacked_out; }
void decode_get_info(DecInfo* out) { *out = info; }

static u8 level_code(u8 idc)
{
    switch (idc) {
    case 9:  return MVD_H264_LEVEL_1_0B;
    case 10: return MVD_H264_LEVEL_1_0;
    case 11: return MVD_H264_LEVEL_1_1;
    case 12: return MVD_H264_LEVEL_1_2;
    case 13: return MVD_H264_LEVEL_1_3;
    case 20: return MVD_H264_LEVEL_2_0;
    case 21: return MVD_H264_LEVEL_2_1;
    case 22: return MVD_H264_LEVEL_2_2;
    case 30: return MVD_H264_LEVEL_3_0;
    case 31: return MVD_H264_LEVEL_3_1;
    case 32: return MVD_H264_LEVEL_3_2;
    case 40: return MVD_H264_LEVEL_4_0;
    case 41: return MVD_H264_LEVEL_4_1;
    case 42: return MVD_H264_LEVEL_4_2;
    case 50: return MVD_H264_LEVEL_5_0;
    case 51: return MVD_H264_LEVEL_5_1;
    default: return MVD_H264_LEVEL_5_2;
    }
}

// (Re)starts MVD with a work buffer sized for this resolution and level.
static bool mvd_start(DecLayout layout, u8 level_idc)
{
    mvd_stop();

    out_w = 240;
    out_h = layout == DEC_LAYOUT_ALTERNATING ? 400 : 800;
    MVDSTD_OutputFormat fmt = MVD_OUTPUT_BGR565;

    MVDSTD_CalculateWorkBufSizeConfig calc;
    memset(&calc, 0, sizeof(calc));
    calc.level.enable = 1;
    calc.level.flag = MVD_CALC_WITH_LEVEL_FLAG_ENABLE_CALC | MVD_CALC_WITH_LEVEL_FLAG_ENABLE_EXTRA_OP |
                      MVD_CALC_WITH_LEVEL_FLAG_UNK;
    calc.level.level = level_code(level_idc);
    calc.width = out_w;
    calc.height = out_h;

    u32 calc_size = 0;
    Result r = mvdstdCalculateBufferSize(&calc, &calc_size);
    log_line("calcbufsize level_idc=%u code=%u %lux%lu ret=0x%08lx size=%lu", level_idc,
             calc.level.level, (unsigned long)out_w, (unsigned long)out_h, (unsigned long)r,
             (unsigned long)calc_size);
    u32 size = R_FAILED(r) ? 0 : calc_size;
    // Never go below the libctru default: a larger work buffer only costs linear memory.
    if (size < MVD_DEFAULT_WORKBUF_SIZE) size = MVD_DEFAULT_WORKBUF_SIZE;

    // Bracket the work buffer libctru allocates inside mvdstdInit (it is not exposed).
    void* probe = linearAlloc(64);
    log_line("pre-init probe=%08lx linearSpaceFree=%lu", (unsigned long)(u32)probe,
             (unsigned long)linearSpaceFree());
    linearFree(probe);

    log_line("-> mvdstdInit size=%lu fmt=0x%08lx", (unsigned long)size, (unsigned long)fmt);
    r = mvdstdInit(MVDMODE_VIDEOPROCESSING, MVD_INPUT_H264, fmt, size, NULL);
    log_line("<- mvdstdInit ret=0x%08lx", (unsigned long)r);

    probe = linearAlloc(64);
    log_line("post-init probe=%08lx linearSpaceFree=%lu", (unsigned long)(u32)probe,
             (unsigned long)linearSpaceFree());
    linearFree(probe);

    if (R_FAILED(r)) {
        set_error("mvdstdInit", r);
        return false;
    }

    // Rotation trick: the 3DS top framebuffer is stored rotated (240 px per column), so a
    // stream encoded pre-rotated as 240 wide makes MVD emit framebuffer-ready pixels and
    // the CPU never rotates anything. The output address is set per AU.
    mvdstdGenerateDefaultConfig(&cfg, out_w, out_h, out_w, out_h, NULL, (u32*)out_buf[0], (u32*)out_buf[0]);
    cfg.output_type = fmt;

    last_param_len[0] = last_param_len[1] = 0;   // fresh MVD instance needs SPS/PPS again
    info.ready = true;
    info.workbuf_size = size;
    info.level_idc = level_idc;
    cur_layout = layout;
    return true;
}

// Index of the first byte of the next 00 00 01 at or after i, or n if none.
static u32 next_start_code(const u8* b, u32 i, u32 n)
{
    for (; i + 2 < n; i++)
        if (b[i] == 0 && b[i + 1] == 0 && b[i + 2] == 1) return i;
    return n;
}

// Calls fn for every NAL (without start code). Stops early if fn returns nonzero.
typedef int (*NalFn)(const u8* nal, u32 len, void* ctx);
static int for_each_nal(const u8* au, u32 size, NalFn fn, void* ctx)
{
    u32 sc = next_start_code(au, 0, size);
    while (sc < size) {
        u32 start = sc + 3;
        u32 next = next_start_code(au, start, size);
        u32 end = next;
        // Trailing zeros belong to the next 4-byte start code (or are trailing_zero_8bits).
        while (end > start && au[end - 1] == 0) end--;
        if (end > start) {
            int r = fn(au + start, end - start, ctx);
            if (r) return r;
        }
        sc = next;
    }
    return 0;
}

static int find_level(const u8* nal, u32 len, void* ctx)
{
    if ((nal[0] & 0x1F) == 7 && len >= 4) {   // SPS: profile_idc, constraints, level_idc
        *(u8*)ctx = nal[3];
        return 1;
    }
    return 0;
}

// One mvdstdProcessVideoFrame call on in_buf[0..len), logged before and after.
static Result process(u32 len, const char* what)
{
    // Write back the CPU cache so MVD (reading physical memory) sees the input.
    GSPGPU_FlushDataCache(in_buf, len);
    MVDSTD_ProcessNALUnitOut o;
    memset(&o, 0, sizeof(o));
    log_line("-> process %s len=%lu flag=0", what, (unsigned long)len);
    Result r = mvdstdProcessVideoFrame(in_buf, len, 0, &o);
    log_line("<- process ret=0x%08lx end_va=%08lx end_pa=%08lx remaining=%lu", (unsigned long)r,
             (unsigned long)o.end_vaddr, (unsigned long)o.end_physaddr, (unsigned long)o.remaining_size);
    return r;
}

static Result set_config(u8* target)
{
    cfg.physaddr_outdata0 = osConvertVirtToPhys(target);
    Result r = MVDSTD_SetConfig(&cfg);
    log_line("setconfig out_va=%08lx out_pa=%08lx ret=0x%08lx", (unsigned long)(u32)target,
             (unsigned long)cfg.physaddr_outdata0, (unsigned long)r);
    return r;
}

static Result render_frame(MVDSTD_Config* c)
{
    Result r = MVD_STATUS_BUSY;
    int i = 0;
    log_line("-> render cfg=%s", c ? "cfg" : "NULL");
    for (; i < RENDER_RETRIES && r == MVD_STATUS_BUSY; i++) {
        r = mvdstdRenderVideoFrame(c, false);
        if (r == MVD_STATUS_BUSY) svcSleepThread(100000);
    }
    log_line("<- render ret=0x%08lx tries=%d", (unsigned long)r, i);
    return r;
}

// ---------------------------------------------------------------------------
// Modes A / B: one NAL per ProcessVideoFrame (devkitPro mvd example).

typedef struct {
    u8*  target;
    bool frame;
} NalCtx;

static int feed_nal(const u8* nal, u32 len, void* p)
{
    NalCtx* ctx = p;
    if (len + 3 > IN_BUF_SIZE) return 0;

    u8 type = nal[0] & 0x1F;
    // Only parameter sets and slices; SEI/AUD/filler are skipped so they never trigger
    // a render of a stale picture.
    if (type != 1 && type != 5 && type != 7 && type != 8) {
        log_line("skip nal type=%u len=%lu", type, (unsigned long)len);
        return 0;
    }

    if (type == 7 || type == 8) {
        int k = type - 7;
        if (len <= PARAM_MAX && len == last_param_len[k] && memcmp(nal, last_param[k], len) == 0) {
            log_line("skip nal type=%u len=%lu (same as last fed)", type, (unsigned long)len);
            return 0;
        }
        if (len <= PARAM_MAX) {
            memcpy(last_param[k], nal, len);
            last_param_len[k] = len;
        }
    }

    // MVD wants each NAL with a 3-byte 00 00 01 prefix (as in the devkitPro mvd example),
    // regardless of whether the AU used 3- or 4-byte start codes.
    in_buf[0] = 0; in_buf[1] = 0; in_buf[2] = 1;
    memcpy(in_buf + 3, nal, len);

    char what[24];
    snprintf(what, sizeof(what), "nal type=%u", type);
    Result ret = process(len + 3, what);
    if (!MVD_CHECKNALUPROC_SUCCESS(ret)) {
        set_error("process", ret);
        return -1;
    }

    // A slice completes a picture (single slice per frame per the contract).
    if ((type == 1 || type == 5) && ret != MVD_STATUS_PARAMSET && ret != MVD_STATUS_INCOMPLETEPROCESSING) {
        ret = render_frame(&cfg);
        if (ret != MVD_STATUS_OK) {
            set_error("render", ret);
            return -1;
        }
        ctx->frame = true;
    }
    return 0;
}

static DecResult decode_nal_by_nal(const u8* au, u32 size, u8* target, u32 bytes)
{
    // MVD writes `target` by physical address. Discard (not write back) any cached lines
    // of it first: a dirty line evicted later would overwrite part of the decoded picture.
    GSPGPU_InvalidateDataCache(target, bytes);
    // Point MVD at the target before feeding NALs: it may write the picture during
    // mvdstdProcessVideoFrame, not only in mvdstdRenderVideoFrame.
    Result r = set_config(target);
    if (r != MVD_STATUS_OK) {
        set_error("setconfig", r);
        return DEC_ERROR;
    }
    NalCtx ctx = { target, false };
    if (for_each_nal(au, size, feed_nal, &ctx) < 0) return DEC_ERROR;
    return ctx.frame ? DEC_FRAME : DEC_NO_FRAME;
}

// ---------------------------------------------------------------------------

DecResult decode_au(const u8* au, u32 size, DecLayout layout, bool keyframe, int eye, u8* dst)
{
    au_index++;
    log_line("AU #%lu eye=%c key=%d size=%lu layout=%s", (unsigned long)au_index,
             layout == DEC_LAYOUT_STACKED ? 'S' : (eye ? 'R' : 'L'), keyframe, (unsigned long)size,
             layout == DEC_LAYOUT_ALTERNATING ? "alt" : "stacked");

    if (keyframe) {
        u8 level = 0;
        for_each_nal(au, size, find_level, &level);
        if (level < MIN_LEVEL_IDC) level = MIN_LEVEL_IDC;
        if (!info.ready || layout != cur_layout || level > info.level_idc)
            if (!mvd_start(layout, level)) return DEC_ERROR;
    } else if (!info.ready || layout != cur_layout) {
        log_line("non-key AU before MVD start, dropped");
        return DEC_ERROR;   // need an IDR to (re)start
    }

    bool alt = layout == DEC_LAYOUT_ALTERNATING;
    u8* buf = out_buf[alt ? (eye ? 1 : 0) : 0];
    DecResult r;

    u8* target = (mode == DEC_MODE_A && alt) ? dst : buf;
    r = decode_nal_by_nal(au, size, target, out_w * out_h * 2);
    if (r != DEC_FRAME) return r;
    if (target == dst) return DEC_FRAME;   // mode A: MVD wrote the framebuffer itself

    GSPGPU_InvalidateDataCache(buf, out_w * out_h * 2);   // MVD wrote it behind the cache
    if (alt) {
        memcpy(dst, buf, DEC_EYE_BYTES);
        GSPGPU_FlushDataCache(dst, DEC_EYE_BYTES);   // framebuffer is cached linear memory
    } else {
        stacked_out = buf;
    }
    return DEC_FRAME;
}
