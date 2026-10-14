#include "audio.h"

#include <string.h>
#include <opus/opus.h>

#include "log.h"

#define SAMPLE_RATE   48000
#define CHANNELS      2
#define FRAME_MS      20       // ARCHITECTURE: 20 ms Opus packets
// WiFi delivers audio in bursts, so the buffer level is steered with the playback rate
// instead of dropping/inserting: hard limits are only for extremes.
#define TARGET_MS     200      // buffer level the rate control steers to
#define START_MS      150      // jitter buffer filled before playback (re)starts
#define MAX_MS        600      // above this, drop the oldest packets down to TARGET_MS
#define RATE_K        0.04f    // rate offset = K * (level - target) / target: +100 ms -> +2%
#define RATE_MAX      0.02f    // clamp: +-2% (inaudible pitch change)
#define EMA_MS        1000.0f  // smoothing time constant of the level
#define FADE_FRAMES   240      // 5 ms fade-in after silence / drops / start
#define SUMMARY_MS    10000    // log a summary line this often
#define PKT_SLOTS     64       // 1.28 s of packets
#define NUM_WBUF      8
#define WBUF_SAMPLES  2880     // per buffer: up to 60 ms, tolerates longer Opus frames

typedef struct {
    u16 len;
    u8  data[AUDIO_MAX_PAYLOAD];
} Packet;

static LightLock   lock;          // guards pkts/head/count/stats
static Packet      pkts[PKT_SLOTS];
static int         head, count;
static AudioStats  stats;
static volatile bool reset_req;

static LightEvent  event;         // packet arrived / DSP frame done / quit
static volatile bool running;
static Thread      thread;
static bool        dsp_ok;
static OpusDecoder* dec;
static s16*        pcm;           // NUM_WBUF * WBUF_SAMPLES stereo frames, linear memory
static ndspWaveBuf wbuf[NUM_WBUF];

static void set_state(AudioState s)
{
    LightLock_Lock(&lock);
    stats.state = s;
    LightLock_Unlock(&lock);
}

const char* audio_state_name(AudioState s)
{
    switch (s) {
    case AUDIO_OFF:       return "off";
    case AUDIO_NO_DSP:    return "no DSP firmware";
    case AUDIO_ERROR:     return "init error";
    case AUDIO_IDLE:      return "idle";
    case AUDIO_BUFFERING: return "buffering";
    default:              return "playing";
    }
}

void audio_get_stats(AudioStats* out)
{
    LightLock_Lock(&lock);
    *out = stats;
    LightLock_Unlock(&lock);
}

void audio_push(const u8* payload, u32 len)
{
    if (len <= 8 || len > AUDIO_MAX_PAYLOAD) return;
    LightLock_Lock(&lock);
    stats.packets++;
    if (dsp_ok && running) {
        if (count == PKT_SLOTS) {          // full: drop the oldest
            head = (head + 1) % PKT_SLOTS;
            count--;
            stats.dropped++;
        }
        Packet* p = &pkts[(head + count) % PKT_SLOTS];
        p->len = len;
        memcpy(p->data, payload, len);
        count++;
    }
    LightLock_Unlock(&lock);
    LightEvent_Signal(&event);
}

void audio_reset(void)
{
    reset_req = true;
    LightEvent_Signal(&event);
}

static void dsp_callback(void* arg)
{
    (void)arg;
    LightEvent_Signal(&event);
}

static bool wbuf_free(const ndspWaveBuf* w)
{
    return w->status == NDSP_WBUF_FREE || w->status == NDSP_WBUF_DONE;
}

static void do_reset(void)
{
    reset_req = false;
    ndspChnWaveBufClear(0);
    for (int i = 0; i < NUM_WBUF; i++) wbuf[i].status = NDSP_WBUF_DONE;
    opus_decoder_ctl(dec, OPUS_RESET_STATE);
    LightLock_Lock(&lock);
    head = count = 0;
    stats.state = AUDIO_IDLE;
    stats.buffered_ms = 0;
    LightLock_Unlock(&lock);
}

static void submit(ndspWaveBuf* w, u32 frames)
{
    w->nsamples = frames;   // stereo frames
    // The DSP reads physical memory: write back the samples first.
    DSP_FlushDataCache(w->data_pcm16, frames * CHANNELS * sizeof(s16));
    ndspChnWaveBufAdd(0, w);
}

static void fade_in(s16* p, u32 frames)
{
    u32 n = frames < FADE_FRAMES ? frames : FADE_FRAMES;
    for (u32 i = 0; i < n; i++) {
        p[2 * i]     = (s16)(p[2 * i] * (s32)i / (s32)n);
        p[2 * i + 1] = (s16)(p[2 * i + 1] * (s32)i / (s32)n);
    }
}

static void audio_thread(void* arg)
{
    (void)arg;
    static Packet pkt;   // decoded outside the lock
    AudioState state = AUDIO_IDLE;
    u32 logged_errors = 0;
    bool need_fade = true;
    float level = 0.0f;      // smoothed buffer level, ms
    float rate = 0.0f;       // current relative rate offset
    float applied_rate = -1.0f;
    u64 last_t = osGetTime();
    u64 next_summary = last_t + SUMMARY_MS;

    while (running) {
        LightEvent_WaitTimeout(&event, 20 * 1000000LL);
        if (!running) break;
        if (reset_req) {
            do_reset();
            state = AUDIO_IDLE;
            need_fade = true;
        }

        u32 inflight_ms = 0;
        for (int i = 0; i < NUM_WBUF; i++)
            if (!wbuf_free(&wbuf[i])) inflight_ms += wbuf[i].nsamples * 1000 / SAMPLE_RATE;

        LightLock_Lock(&lock);
        if (count * FRAME_MS + inflight_ms > MAX_MS) {   // extreme backlog: drop the oldest
            while (count > 0 && count * FRAME_MS + inflight_ms > TARGET_MS) {
                head = (head + 1) % PKT_SLOTS;
                count--;
                stats.dropped++;
            }
            need_fade = true;
        }
        u32 queued_ms = count * FRAME_MS;
        LightLock_Unlock(&lock);

        if (state != AUDIO_PLAYING && queued_ms >= START_MS) {
            state = AUDIO_PLAYING;
            level = (float)(queued_ms + inflight_ms);   // seed the EMA at the start level
        }

        if (state == AUDIO_PLAYING) {
            for (int i = 0; i < NUM_WBUF; i++) {
                if (!wbuf_free(&wbuf[i])) continue;

                bool have = false;
                LightLock_Lock(&lock);
                if (count > 0) {
                    pkt = pkts[head];
                    head = (head + 1) % PKT_SLOTS;
                    count--;
                    have = true;
                }
                LightLock_Unlock(&lock);

                if (!have) {
                    if (inflight_ms < FRAME_MS) {
                        // Underrun: bridge with 20 ms of silence, then rebuffer to START_MS.
                        memset(wbuf[i].data_pcm16, 0, SAMPLE_RATE * FRAME_MS / 1000 * CHANNELS * sizeof(s16));
                        submit(&wbuf[i], SAMPLE_RATE * FRAME_MS / 1000);
                        LightLock_Lock(&lock);
                        stats.underruns++;
                        LightLock_Unlock(&lock);
                        state = AUDIO_BUFFERING;
                        need_fade = true;
                    }
                    break;
                }

                // Payload = u64 BE timestamp (unused in v1: play on arrival) + Opus packet.
                int n = opus_decode(dec, pkt.data + 8, pkt.len - 8, wbuf[i].data_pcm16, WBUF_SAMPLES, 0);
                if (n <= 0) {
                    LightLock_Lock(&lock);
                    stats.decode_errors++;
                    LightLock_Unlock(&lock);
                    if (logged_errors++ < 5) log_line("audio: opus_decode error %d", n);
                    continue;
                }
                if (need_fade) {   // avoid a click when resuming after silence or a gap
                    fade_in(wbuf[i].data_pcm16, (u32)n);
                    need_fade = false;
                }
                submit(&wbuf[i], (u32)n);
                inflight_ms += (u32)n * 1000 / SAMPLE_RATE;
            }
        }

        // Rate control: play slightly faster when the (smoothed) buffer is above target and
        // slower when below. Also absorbs clock drift between sender and the 3DS DSP.
        u64 now = osGetTime();
        float dt = (float)(now - last_t);
        last_t = now;
        LightLock_Lock(&lock);
        u32 buffered = count * FRAME_MS + inflight_ms;
        LightLock_Unlock(&lock);
        float a = dt / EMA_MS;
        level += ((float)buffered - level) * (a > 1.0f ? 1.0f : a);
        if (state == AUDIO_PLAYING) {
            rate = RATE_K * (level - TARGET_MS) / TARGET_MS;
            if (rate > RATE_MAX) rate = RATE_MAX;
            if (rate < -RATE_MAX) rate = -RATE_MAX;
        } else {
            rate = 0.0f;
        }
        if (rate - applied_rate > 0.0002f || applied_rate - rate > 0.0002f) {
            ndspChnSetRate(0, SAMPLE_RATE * (1.0f + rate));
            applied_rate = rate;
        }

        LightLock_Lock(&lock);
        stats.state = state;
        stats.buffered_ms = buffered;
        stats.level_ms = (u32)(level + 0.5f);
        stats.rate_permille = (s32)(rate * 1000.0f + (rate >= 0 ? 0.5f : -0.5f));
        AudioStats snap = stats;
        LightLock_Unlock(&lock);

        if (now >= next_summary) {
            next_summary = now + SUMMARY_MS;
            log_line("audio: %s level=%lums (now %lums) rate=%+ld permille underruns=%lu drops=%lu pkts=%lu",
                     audio_state_name(snap.state), (unsigned long)snap.level_ms,
                     (unsigned long)snap.buffered_ms, (long)snap.rate_permille,
                     (unsigned long)snap.underruns, (unsigned long)snap.dropped, (unsigned long)snap.packets);
        }
    }
}

void audio_start(void)
{
    LightLock_Init(&lock);
    LightEvent_Init(&event, RESET_ONESHOT);
    memset(&stats, 0, sizeof(stats));

    Result r = ndspInit();
    if (R_FAILED(r)) {
        log_line("audio: ndspInit failed 0x%08lx (missing sdmc:/3ds/dspfirm.cdc?)", (unsigned long)r);
        set_state(AUDIO_NO_DSP);
        return;
    }
    dsp_ok = true;

    int err = 0;
    dec = opus_decoder_create(SAMPLE_RATE, CHANNELS, &err);
    pcm = linearAlloc(NUM_WBUF * WBUF_SAMPLES * CHANNELS * sizeof(s16));
    if (!dec || !pcm) {
        log_line("audio: init failed (opus err %d, pcm %p)", err, (void*)pcm);
        set_state(AUDIO_ERROR);
        return;
    }

    ndspSetOutputMode(NDSP_OUTPUT_STEREO);
    ndspChnReset(0);
    ndspChnSetInterp(0, NDSP_INTERP_LINEAR);
    ndspChnSetRate(0, SAMPLE_RATE);
    ndspChnSetFormat(0, NDSP_FORMAT_STEREO_PCM16);
    float mix[12] = { 1.0f, 1.0f };   // front left / right
    ndspChnSetMix(0, mix);

    memset(wbuf, 0, sizeof(wbuf));
    for (int i = 0; i < NUM_WBUF; i++) {
        wbuf[i].data_pcm16 = pcm + i * WBUF_SAMPLES * CHANNELS;
        wbuf[i].status = NDSP_WBUF_DONE;
    }
    ndspSetCallback(dsp_callback, NULL);

    // Higher priority than the main (decode) thread: audio must never wait behind MVD.
    s32 prio = 0x30;
    svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
    prio = prio > 0x18 ? prio - 1 : 0x18;

    running = true;
    int core = 2;   // New 3DS app core first, then the system core, then the default core
    thread = threadCreate(audio_thread, NULL, 64 * 1024, prio, core, false);
    if (!thread) {
        APT_SetAppCpuTimeLimit(30);
        core = 1;
        thread = threadCreate(audio_thread, NULL, 64 * 1024, prio, core, false);
    }
    if (!thread) {
        core = -2;
        thread = threadCreate(audio_thread, NULL, 64 * 1024, prio, core, false);
    }
    if (!thread) {
        running = false;
        log_line("audio: threadCreate failed");
        set_state(AUDIO_ERROR);
        return;
    }
    set_state(AUDIO_IDLE);
    log_line("audio: started (ndsp ok, opus %s, thread core %d prio 0x%lx)", opus_get_version_string(),
             core, (unsigned long)prio);
}

void audio_stop(void)
{
    if (running) {
        running = false;
        LightEvent_Signal(&event);
        threadJoin(thread, U64_MAX);
        threadFree(thread);
        thread = NULL;
    }
    if (dsp_ok) {
        ndspSetCallback(NULL, NULL);
        ndspChnWaveBufClear(0);
        ndspExit();
        dsp_ok = false;
    }
    if (dec) opus_decoder_destroy(dec);
    dec = NULL;
    if (pcm) linearFree(pcm);
    pcm = NULL;
}
