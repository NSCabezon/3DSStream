#pragma once

#include <3ds.h>

#define NET_PORT        7777
#define NET_MAX_PAYLOAD (256 * 1024)  // a 240x400 IDR is ~20 KiB
#define NET_NAME_MAX    64

typedef enum {
    NET_IDLE,       // listening, no sender
    NET_CONNECTED,  // sender connected
    NET_ERROR,      // socket setup failed (see net_last_error)
} NetState;

// VIDEO_AU flags (header byte 5)
#define NET_FLAG_KEYFRAME    0x01  // AU contains an IDR
#define NET_FLAG_EYE_RIGHT   0x02  // alternating layout: 0 = left eye, 1 = right eye
#define NET_FLAG_ALTERNATING 0x04  // 1 = alternating 240x400 eyes, 0 = stacked 240x800

typedef struct {
    u8*  data;
    u32  size;
    u8   flags;     // NET_FLAG_*
    bool keyframe;
} NetAU;

typedef struct {
    NetState state;
    char     sender[NET_NAME_MAX + 1];
    u64      bytes_rx;      // total payload+header bytes received
    u32      dropped_aus;   // AUs dropped by the latency / keyframe policy
} NetStats;

// Text sent to every new sender as LOG (0x20) messages, chunked to <= 64 KiB.
// Must be called before net_start; the buffer must stay valid until net_stop.
void net_set_postmortem(const char* text, u32 len);
const void* net_soc_buffer(void);
u32 net_soc_buffer_size(void);

// Starts soc:U and the network thread. Returns false on fatal error.
bool net_start(void);
// Stops the thread, closes sockets, socExit. Safe to call after a failed start.
void net_stop(void);

// Pops the oldest queued AU, waiting up to timeout_ns. Returns NULL on timeout.
// The AU stays valid until net_release(au).
NetAU* net_pop(s64 timeout_ns);
void   net_release(NetAU* au);

// Asks the sender for an IDR and drops non-key AUs until one arrives.
// Thread-safe; the actual send happens on the network thread so the main thread
// never blocks on the socket service.
void net_request_keyframe(void);

void net_get_stats(NetStats* out);
