// 3DSStream: receives a pre-rotated stereo H.264 stream over TCP and shows it on the
// New 3DS top screen in 3D. See docs/ARCHITECTURE.md for the contract.
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>

#include <3ds.h>

#include "audio.h"
#include "decode.h"
#include "log.h"
#include "net.h"

static bool swap_eyes;
static DecMode dec_mode = DEC_MODE_A;
static bool slow_motion;   // diagnostics: sleep 200 ms after each AU

static void wait_for_start(void)
{
    printf("\nPress START to exit.\n");
    while (aptMainLoop()) {
        gspWaitForVBlank();
        hidScanInput();
        if (hidKeysDown() & KEY_START) break;
    }
}

// gfxScreenSwapBuffers only flips at the next VBlank; until then the "back" buffers are
// still on screen, so wait before writing a new picture into them (bounded, so a stuck
// flag can never freeze the main loop).
static void wait_present_done(void)
{
    for (int i = 0; i < 3 && gspIsPresentPending(GSP_SCREEN_TOP); i++)
        gspWaitForVBlank();
}

static void swap_top(void)
{
    gfxScreenSwapBuffers(GFX_TOP, true);   // true = show both eyes (3D)
}

// Stacked layout: copy the decoded 240x800 frame into the two top back buffers.
static void present_stacked(const u8* frame)
{
    wait_present_done();
    u8* left  = gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL);
    u8* right = gfxGetFramebuffer(GFX_TOP, GFX_RIGHT, NULL, NULL);
    const u8* l = frame;
    const u8* r = frame + DEC_EYE_BYTES;
    if (swap_eyes) { const u8* t = l; l = r; r = t; }
    memcpy(left, l, DEC_EYE_BYTES);
    memcpy(right, r, DEC_EYE_BYTES);
    // Framebuffers are in cached linear memory: write back before the LCD reads them.
    GSPGPU_FlushDataCache(left, DEC_EYE_BYTES);
    GSPGPU_FlushDataCache(right, DEC_EYE_BYTES);
    swap_top();
}

static void line(int row, const char* fmt, ...) __attribute__((format(printf, 2, 3)));
static void line(int row, const char* fmt, ...)
{
    char buf[64];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    printf("\x1b[%d;1H%-39.39s", row, buf);   // overwrite the whole row
}

typedef struct {
    u32 pairs, decoded, errors;      // totals
    u32 win_pairs, win_decoded;      // current 1 s window
    float pair_fps, dec_fps, kbps;
    bool alternating;                // layout of the last AU
    bool pair_left;                  // alternating: left eye of the current pair decoded
} UiStats;

static void draw_status(const UiStats* ui)
{
    NetStats ns;
    net_get_stats(&ns);

    struct in_addr ip = { .s_addr = (in_addr_t)gethostid() };
    const char* state = ns.state == NET_CONNECTED ? "connected"
                      : ns.state == NET_IDLE      ? "waiting for sender"
                                                  : "no network (WiFi on?)";

    line(1,  "3DSStream - stereo stream receiver");
    line(2,  "Mode: %s%s", decode_mode_name(dec_mode), slow_motion ? " SLOW" : "");
    line(3,  "IP:     %s", ip.s_addr ? inet_ntoa(ip) : "-");
    line(4,  "Port:   %d", NET_PORT);
    line(5,  "State:  %s", state);
    line(6,  "Sender: %s", ns.sender[0] ? ns.sender : "-");
    line(7,  "Layout: %s", ui->alternating ? "alternating 240x400" : "stacked 240x800");
    line(9,  "Stereo FPS: %.1f  (decoded %.1f)", (double)ui->pair_fps, (double)ui->dec_fps);
    line(10, "Kbps:   %.0f", (double)ui->kbps);
    line(11, "Pairs:  %lu", (unsigned long)ui->pairs);
    line(12, "Dropped AUs:   %lu", (unsigned long)ns.dropped_aus);
    line(13, "Decode errors: %lu", (unsigned long)ui->errors);

    DecInfo di;
    decode_get_info(&di);
    if (di.ready)
        line(15, "MVD: level %u.%u, workbuf %lu KB", di.level_idc / 10, di.level_idc % 10,
             (unsigned long)(di.workbuf_size / 1024));
    else
        line(15, "MVD: waiting for keyframe");
    line(16, "Last err: %s 0x%08lx", di.last_step, (unsigned long)di.last_error);

    AudioStats as;
    audio_get_stats(&as);
    if (as.state == AUDIO_NO_DSP)
        line(14, "Audio: no DSP firmware (dspfirm.cdc)");
    else {
        // The console font has no per-mille sign: "+3/1000".
        line(14, "Audio: %s %lums (now %lums)", audio_state_name(as.state),
             (unsigned long)as.level_ms, (unsigned long)as.buffered_ms);
        line(17, "Audio rate: %+ld/1000  U%lu D%lu", (long)as.rate_permille,
             (unsigned long)as.underruns, (unsigned long)as.dropped);
    }
    line(18, "3D slider: %3d%%  Eyes: %s", (int)(osGet3DSliderState() * 100.0f),
         swap_eyes ? "swapped" : "normal");
    line(20, "START exit  Y keyframe  X swap eyes");
}

static void handle_au(const NetAU* au, UiStats* ui)
{
    bool key = au->flags & NET_FLAG_KEYFRAME;
    DecResult r;
    ui->alternating = au->flags & NET_FLAG_ALTERNATING;

    if (ui->alternating) {
        // One eye per frame. MVD writes it straight into that eye's back framebuffer (as
        // in the devkitPro mvd example); the pair is shown after the RIGHT eye completes.
        // Every frame is decoded even if its pair is incomplete: later frames reference it.
        bool right = au->flags & NET_FLAG_EYE_RIGHT;
        if (!right) wait_present_done();
        gfx3dSide_t side = (right != swap_eyes) ? GFX_RIGHT : GFX_LEFT;
        u8* fb = gfxGetFramebuffer(GFX_TOP, side, NULL, NULL);
        r = decode_au(au->data, au->size, DEC_LAYOUT_ALTERNATING, key, right ? 1 : 0, fb);
        if (r == DEC_FRAME) {
            if (!right) {
                ui->pair_left = true;
            } else {
                if (ui->pair_left) {
                    swap_top();
                    ui->pairs++;
                    ui->win_pairs++;
                }
                ui->pair_left = false;
            }
        } else if (r == DEC_ERROR) {
            ui->pair_left = false;
        }
    } else {
        r = decode_au(au->data, au->size, DEC_LAYOUT_STACKED, key, 0, NULL);
        if (r == DEC_FRAME) {
            present_stacked(decode_stacked_output());
            ui->pairs++;
            ui->win_pairs++;
        }
    }

    if (r == DEC_FRAME) {
        ui->decoded++;
        ui->win_decoded++;
    } else if (r == DEC_ERROR) {
        ui->errors++;
        net_request_keyframe();   // queue then drops non-key AUs until the next (left) IDR
    }
}

static void log_fb(const char* name, gfxScreen_t scr, gfx3dSide_t side)
{
    u16 w = 0, h = 0;
    u8* fb = gfxGetFramebuffer(scr, side, &w, &h);
    log_line("buf %-8s va=%08lx pa=%08lx size=%u", name, (unsigned long)(u32)fb,
             (unsigned long)osConvertVirtToPhys(fb), (unsigned)(w * h * 2));
}

static int read_mode_file(void)
{
    FILE* f = fopen(MODE_PATH, "r");
    if (!f) return -1;
    int c = fgetc(f);
    fclose(f);
    switch (c) {
    case 'A': return DEC_MODE_A;
    case 'B': return DEC_MODE_B;
    case 'X': return DEC_MODE_X;
    case 'Y': return DEC_MODE_Y;
    default:  return -1;
    }
}

static void write_mode_file(DecMode m)
{
    FILE* f = fopen(MODE_PATH, "w");
    if (!f) return;
    fputc("ABXY"[m], f);
    fclose(f);
}

// Prints the last `n` lines of text (without the log timestamp), cut to the console width.
static void print_tail(const char* text, u32 len, int n)
{
    u32 end = len;
    while (end > 0 && text[end - 1] == '\n') end--;
    u32 start = end;
    for (int lines = 0; start > 0; start--)
        if (text[start - 1] == '\n' && ++lines == n) break;
    for (const char* p = text + start; p < text + end;) {
        const char* e = memchr(p, '\n', text + end - p);
        int l = e ? (int)(e - p) : (int)(text + end - p);
        const char* q = p;
        if (l > 11 && p[6] == '.' && p[10] == ' ') { q += 11; l -= 11; }   // "SSSSSS.mmm "
        printf("%.*s\n", l < 39 ? l : 39, q);
        if (!e) break;
        p = e + 1;
    }
}

// Start screen: previous run's log tail + strategy selection. Starts mode B by itself
// after 1.5 s unless a button is pressed. Returns false on START.
static bool select_mode(const char* prev, u32 prev_len)
{
    int last = read_mode_file();
    u64 deadline = osGetTime() + 1500;   // 0 once the countdown is cancelled
    consoleClear();
    if (prev) {
        printf("\x1b[33mPrevious log (log_prev.txt):\x1b[0m\n");
        print_tail(prev, prev_len, 18);
    } else {
        printf("No previous log.\n");
    }
    printf("\n\x1b[36mA\x1b[0m direct-to-fb  \x1b[36mB\x1b[0m eye buf+memcpy\n");
    printf("\x1b[36mX\x1b[0m topos AU 565   \x1b[36mY\x1b[0m topos AU ABGR32\n");
    printf("Hold L = slow motion. START = exit.\n");
    printf("Last choice: %s\n", last >= 0 ? decode_mode_name((DecMode)last) : "-");
    printf("Starting B in 1.5 s (any button = wait)");

    while (aptMainLoop()) {
        gspWaitForVBlank();
        hidScanInput();
        u32 down = hidKeysDown();
        if (down & KEY_START) return false;
        if (deadline && !down && !hidKeysHeld() && osGetTime() >= deadline) {
            dec_mode = DEC_MODE_B;
            slow_motion = false;
            return true;
        }
        int m = (down & KEY_A) ? DEC_MODE_A : (down & KEY_B) ? DEC_MODE_B
              : (down & KEY_X) ? DEC_MODE_X : (down & KEY_Y) ? DEC_MODE_Y : -1;
        if (down && m < 0 && deadline) {
            deadline = 0;
            printf("\r%-39s", "Waiting for A/B/X/Y...");
        }
        if (m >= 0) {
            dec_mode = (DecMode)m;
            slow_motion = hidKeysHeld() & KEY_L;
            write_mode_file(dec_mode);
            return true;
        }
    }
    return false;
}

int main(void)
{
    gfxInit(GSP_RGB565_OES, GSP_BGR8_OES, false);
    gfxSet3D(true);
    consoleInit(GFX_BOTTOM, NULL);

    bool is_new = false;
    APT_CheckNew3DS(&is_new);
    if (!is_new) {
        printf("3DSStream needs a New Nintendo 3DS\n(hardware H.264 decoder).\n");
        wait_for_start();
        gfxExit();
        return 0;
    }
    osSetSpeedupEnable(true);

    log_open();
    log_line("3DSStream build " __DATE__ " " __TIME__);

    // Clear both eyes of both top buffers to black (logging both double-buffer halves).
    for (int i = 0; i < 2; i++) {
        log_fb(i ? "topL#1" : "topL#0", GFX_TOP, GFX_LEFT);
        log_fb(i ? "topR#1" : "topR#0", GFX_TOP, GFX_RIGHT);
        memset(gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL), 0, DEC_EYE_BYTES);
        memset(gfxGetFramebuffer(GFX_TOP, GFX_RIGHT, NULL, NULL), 0, DEC_EYE_BYTES);
        gfxFlushBuffers();
        gfxSwapBuffers();
        gspWaitForVBlank();
    }

    log_fb("bottom", GFX_BOTTOM, GFX_LEFT);

    u32 prev_len = 0;
    char* prev = log_read_prev(2 * 1024 * 1024, &prev_len);
    if (!select_mode(prev, prev_len)) {
        log_line("exit from start screen");
        free(prev);
        log_close();
        gfxExit();
        return 0;
    }
    log_line("mode %s%s", decode_mode_name(dec_mode), slow_motion ? " (slow motion)" : "");

    net_set_postmortem(prev, prev_len);
    bool dec_ok = decode_init(dec_mode);
    audio_start();   // before net_start: the network thread feeds it. Failure is not fatal.
    bool net_ok = dec_ok && net_start();
    if (net_ok)
        log_line("buf %-8s va=%08lx size=%lu (heap, not linear)", "soc", (unsigned long)(u32)net_soc_buffer(),
                 (unsigned long)net_soc_buffer_size());
    if (!dec_ok || !net_ok) {
        log_line("init failed: %s", !dec_ok ? "linear memory" : "network");
        printf(!dec_ok ? "Out of linear memory.\n" : "Network init failed.\n");
        wait_for_start();
        net_stop();
        audio_stop();
        decode_exit();
        free(prev);
        log_close();
        gfxExit();
        return 0;
    }

    UiStats ui = {0};
    u64 window_bytes = 0;
    u64 window_start = osGetTime();
    consoleClear();
    draw_status(&ui);

    while (aptMainLoop()) {
        hidScanInput();
        u32 down = hidKeysDown();
        if (down & KEY_START) break;
        if (down & KEY_Y) net_request_keyframe();
        if (down & KEY_X) swap_eyes = !swap_eyes;

        // Decode as AUs arrive; the short timeout keeps input and status responsive.
        NetAU* au = net_pop(10 * 1000000LL);
        if (au) {
            handle_au(au, &ui);
            net_release(au);
            if (slow_motion) svcSleepThread(200 * 1000000LL);
        }

        u64 now = osGetTime();
        if (now - window_start >= 1000) {
            NetStats ns;
            net_get_stats(&ns);
            float secs = (now - window_start) / 1000.0f;
            ui.pair_fps = (float)ui.win_pairs / secs;
            ui.dec_fps  = (float)ui.win_decoded / secs;
            ui.kbps = (float)(ns.bytes_rx - window_bytes) * 8.0f / 1000.0f / secs;
            ui.win_pairs = ui.win_decoded = 0;
            window_bytes = ns.bytes_rx;
            window_start = now;
            draw_status(&ui);
            // The console draws into the (cached) bottom framebuffer.
            GSPGPU_FlushDataCache(gfxGetFramebuffer(GFX_BOTTOM, GFX_LEFT, NULL, NULL), 320 * 240 * 2);
        }
    }

    log_line("exit");
    net_stop();
    audio_stop();
    decode_exit();
    free(prev);
    log_close();
    gfxExit();
    return 0;
}
