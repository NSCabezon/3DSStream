#pragma once

#include <3ds.h>

#define AUDIO_MAX_PAYLOAD 4096   // AUDIO message: u64 BE timestamp (us) + one Opus packet

typedef enum {
    AUDIO_OFF,        // not started
    AUDIO_NO_DSP,     // ndspInit failed (usually missing sdmc:/3ds/dspfirm.cdc)
    AUDIO_ERROR,      // other init failure (opus, memory, thread)
    AUDIO_IDLE,       // waiting for packets
    AUDIO_BUFFERING,  // filling the jitter buffer
    AUDIO_PLAYING,
} AudioState;

typedef struct {
    AudioState state;
    u32 buffered_ms;   // queued packets + audio handed to the DSP
    u32 level_ms;      // smoothed (~1 s EMA) buffer level the rate control works on
    s32 rate_permille; // playback rate adjustment, e.g. +3 = 0.3% faster
    u32 underruns;
    u32 dropped;       // packets dropped (queue full / latency cap)
    u32 packets;       // packets received
    u32 decode_errors;
} AudioStats;

// Starts ndsp + the audio thread. Video keeps working if this fails (see audio_get_stats).
void audio_start(void);
void audio_stop(void);

// Network thread: one AUDIO payload (timestamp + Opus packet). Copied; never blocks.
void audio_push(const u8* payload, u32 len);
// New connection / disconnect: drop everything and reset the Opus decoder.
void audio_reset(void);

void audio_get_stats(AudioStats* out);
const char* audio_state_name(AudioState s);
