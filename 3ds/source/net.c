#include "net.h"
#include "log.h"

#include <errno.h>
#include <fcntl.h>
#include <malloc.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#define SOC_ALIGN      0x1000
#define SOC_BUFSIZE    0x100000
#define NUM_SLOTS      16  // MAX_PENDING queued + 1 being decoded + 1 being received + slack
// WiFi delivers AUs in bursts: with only 2 allowed, every burst flushed to the next IDR
// (~1 fps on hardware). 12 AUs = 200 ms at 60 fps before we give up on latency.
#define MAX_PENDING    12
#define HDR_SIZE       12
#define POLL_MS        20

#define MSG_VIDEO_AU     0x01
#define MSG_HELLO        0x02
#define MSG_KEYFRAME_REQ 0x10
#define MSG_LOG          0x20
#define LOG_CHUNK        (64 * 1024)

static u32* soc_buf;
static Thread net_thread_handle;
static volatile bool running;
static const char* postmortem;
static u32 postmortem_len;

void net_set_postmortem(const char* text, u32 len) { postmortem = text; postmortem_len = len; }
const void* net_soc_buffer(void) { return soc_buf; }
u32 net_soc_buffer_size(void) { return SOC_BUFSIZE; }

// ---- shared state, guarded by lock ----
static LightLock lock;
static CondVar   cv;
static NetAU     slots[NUM_SLOTS];
static bool      slot_free[NUM_SLOTS];
static int       queue[NUM_SLOTS];   // FIFO of slot indices
static int       q_count;
static bool      need_keyframe;      // drop non-key AUs until an IDR arrives
static bool      keyframe_req;       // network thread should send KEYFRAME_REQ
static NetStats  stats;

// ---- receive state, network thread only ----
typedef struct {
    u8     hdr[HDR_SIZE];
    u32    hdr_got;
    u8     type, flags;
    u32    len, got;
    u8*    dst;      // NULL = discard payload
    u32    dst_cap;
    NetAU* slot;
    char   name[NET_NAME_MAX];
} Rx;

static u8 discard_buf[4096];

// ---------------------------------------------------------------------------
// Queue helpers (call with lock held)

static NetAU* slot_acquire_locked(void)
{
    for (int i = 0; i < NUM_SLOTS; i++)
        if (slot_free[i]) { slot_free[i] = false; return &slots[i]; }
    return NULL;
}

static void slot_free_locked(NetAU* au)
{
    slot_free[au - slots] = true;
}

// Drops queued AUs; keeps keyframes if keep_keys. Returns number dropped.
static int drop_queued_locked(bool keep_keys)
{
    int kept = 0, dropped = 0;
    for (int i = 0; i < q_count; i++) {
        int s = queue[i];
        if (keep_keys && slots[s].keyframe) {
            queue[kept++] = s;
        } else {
            slot_free[s] = true;
            dropped++;
        }
    }
    q_count = kept;
    stats.dropped_aus += dropped;
    return dropped;
}

static void enqueue_locked(NetAU* au)
{
    queue[q_count++] = au - slots;
    CondVar_Signal(&cv);
}

// Latency / keyframe policy for a fully received VIDEO_AU.
static void deliver_au(NetAU* au)
{
    LightLock_Lock(&lock);
    if (au->keyframe) {
        // An IDR makes everything older obsolete; flush if we are backed up.
        if (q_count >= MAX_PENDING) drop_queued_locked(false);
        need_keyframe = false;
        enqueue_locked(au);
    } else if (need_keyframe) {
        slot_free_locked(au);
        stats.dropped_aus++;
    } else if (q_count >= MAX_PENDING) {
        int n = drop_queued_locked(true);
        slot_free_locked(au);
        stats.dropped_aus++;
        need_keyframe = true;
        keyframe_req = true;
        log_line("net: queue overflow, dropped %d queued + incoming, waiting for keyframe", n);
    } else {
        enqueue_locked(au);
    }
    LightLock_Unlock(&lock);
}

// ---------------------------------------------------------------------------
// Public API (main thread)

NetAU* net_pop(s64 timeout_ns)
{
    NetAU* au = NULL;
    LightLock_Lock(&lock);
    if (q_count == 0)
        CondVar_WaitTimeout(&cv, &lock, timeout_ns);
    if (q_count > 0) {
        au = &slots[queue[0]];
        q_count--;
        memmove(&queue[0], &queue[1], q_count * sizeof(queue[0]));
    }
    LightLock_Unlock(&lock);
    return au;
}

void net_release(NetAU* au)
{
    if (!au) return;
    LightLock_Lock(&lock);
    slot_free_locked(au);
    LightLock_Unlock(&lock);
}

void net_request_keyframe(void)
{
    log_line("net: keyframe requested by decoder/user");
    LightLock_Lock(&lock);
    need_keyframe = true;
    keyframe_req = true;
    drop_queued_locked(true);
    LightLock_Unlock(&lock);
}

void net_get_stats(NetStats* out)
{
    LightLock_Lock(&lock);
    *out = stats;
    LightLock_Unlock(&lock);
}

// ---------------------------------------------------------------------------
// Network thread

static void set_nonblocking(int fd)
{
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
}

static int open_listener(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (fd < 0) return -1;

    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(NET_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) != 0 || listen(fd, 2) != 0) {
        close(fd);
        return -1;
    }
    set_nonblocking(fd);
    return fd;
}

// Sends the 12-byte KEYFRAME_REQ. Returns false only if it should be retried later.
static bool send_keyframe_req(int fd)
{
    const u8 msg[HDR_SIZE] = { 'S', '3', 'D', 'S', MSG_KEYFRAME_REQ, 0, 0, 0, 0, 0, 0, 0 };
    u32 sent = 0;
    for (int tries = 0; sent < HDR_SIZE && tries < 50; tries++) {
        int r = send(fd, msg + sent, HDR_SIZE - sent, 0);
        if (r > 0) {
            sent += r;
        } else if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (sent == 0) return false;     // nothing sent yet: retry next loop
            svcSleepThread(1000000);         // mid-message: finish it
        } else {
            return true;                     // socket error; recv side will notice
        }
    }
    return true;
}

// Blocking-style send on a non-blocking socket (bounded to ~3 s).
static bool send_all(int fd, const void* data, u32 len)
{
    const u8* p = data;
    for (int idle = 0; len > 0 && idle < 3000;) {
        int r = send(fd, p, len, 0);
        if (r > 0) {
            p += r;
            len -= r;
            idle = 0;
        } else if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            svcSleepThread(1000000);
            idle++;
        } else {
            return false;
        }
    }
    return len == 0;
}

static void send_postmortem(int fd)
{
    if (!postmortem || postmortem_len == 0) return;
    u32 sent = 0;
    while (sent < postmortem_len) {
        u32 n = postmortem_len - sent;
        if (n > LOG_CHUNK) n = LOG_CHUNK;
        u8 hdr[HDR_SIZE] = { 'S', '3', 'D', 'S', MSG_LOG, 0, 0, 0,
                             (u8)(n >> 24), (u8)(n >> 16), (u8)(n >> 8), (u8)n };
        if (!send_all(fd, hdr, HDR_SIZE) || !send_all(fd, postmortem + sent, n)) {
            log_line("net: sending previous log failed after %lu bytes", (unsigned long)sent);
            return;
        }
        sent += n;
    }
    log_line("net: sent previous log (%lu bytes)", (unsigned long)sent);
}

static void rx_reset(Rx* rx)
{
    if (rx->slot) {
        LightLock_Lock(&lock);
        slot_free_locked(rx->slot);
        LightLock_Unlock(&lock);
    }
    memset(rx, 0, sizeof(*rx));
}

// Header complete: validate and pick a destination for the payload.
static bool rx_begin_payload(Rx* rx)
{
    const u8* h = rx->hdr;
    if (memcmp(h, "S3DS", 4) != 0) {
        log_line("net: bad magic, closing");
        return false;
    }
    rx->type  = h[4];
    rx->flags = h[5];
    rx->len   = ((u32)h[8] << 24) | ((u32)h[9] << 16) | ((u32)h[10] << 8) | h[11];
    rx->got   = 0;
    rx->dst   = NULL;
    rx->dst_cap = 0;
    if (rx->len > NET_MAX_PAYLOAD) {
        log_line("net: payload too large (%lu), closing", (unsigned long)rx->len);
        return false;
    }

    if (rx->type == MSG_VIDEO_AU) {
        bool key = rx->flags & NET_FLAG_KEYFRAME;
        LightLock_Lock(&lock);
        if (!key && need_keyframe) {
            stats.dropped_aus++;           // skip without buffering
        } else if ((rx->slot = slot_acquire_locked()) != NULL) {
            rx->slot->keyframe = key;
            rx->slot->flags = rx->flags;
            rx->dst = rx->slot->data;
            rx->dst_cap = NET_MAX_PAYLOAD;
        } else {                           // should not happen: NUM_SLOTS > MAX_PENDING + 2
            stats.dropped_aus++;
            need_keyframe = true;
            keyframe_req = true;
        }
        LightLock_Unlock(&lock);
    } else if (rx->type == MSG_HELLO) {
        memset(rx->name, 0, sizeof(rx->name));
        rx->dst = (u8*)rx->name;
        rx->dst_cap = NET_NAME_MAX;        // longer names are truncated
    }
    return true;
}

static void rx_finish_message(Rx* rx)
{
    if (rx->type == MSG_VIDEO_AU && rx->slot) {
        rx->slot->size = rx->len;
        deliver_au(rx->slot);
        rx->slot = NULL;
    } else if (rx->type == MSG_HELLO) {
        LightLock_Lock(&lock);
        u32 n = rx->len < NET_NAME_MAX ? rx->len : NET_NAME_MAX;
        memcpy(stats.sender, rx->name, n);
        stats.sender[n] = '\0';
        LightLock_Unlock(&lock);
        log_line("net: HELLO from '%s'", stats.sender);
    }
    rx->hdr_got = 0;
}

// Reads everything currently available. Returns false if the connection must be closed.
static bool rx_pump(int fd, Rx* rx)
{
    u32 total = 0;
    while (total < 256 * 1024) {           // bounded so we get back to poll / sends
        int r;
        if (rx->hdr_got < HDR_SIZE) {
            r = recv(fd, rx->hdr + rx->hdr_got, HDR_SIZE - rx->hdr_got, 0);
            if (r > 0) {
                rx->hdr_got += r;
                if (rx->hdr_got == HDR_SIZE) {
                    if (!rx_begin_payload(rx)) return false;
                    if (rx->len == 0) rx_finish_message(rx);
                }
            }
        } else {
            u32 want = rx->len - rx->got;
            u8* dst;
            if (rx->dst && rx->got < rx->dst_cap) {
                dst = rx->dst + rx->got;
                if (want > rx->dst_cap - rx->got) want = rx->dst_cap - rx->got;
            } else {
                dst = discard_buf;
                if (want > sizeof(discard_buf)) want = sizeof(discard_buf);
            }
            r = recv(fd, dst, want, 0);
            if (r > 0) {
                rx->got += r;
                if (rx->got == rx->len) rx_finish_message(rx);
            }
        }

        if (r == 0) return false;          // peer closed
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            return false;
        }
        total += r;
    }

    LightLock_Lock(&lock);
    stats.bytes_rx += total;
    LightLock_Unlock(&lock);
    return true;
}

static void set_state(NetState s)
{
    LightLock_Lock(&lock);
    stats.state = s;
    if (s != NET_CONNECTED) stats.sender[0] = '\0';
    LightLock_Unlock(&lock);
}

static void on_new_client(void)
{
    LightLock_Lock(&lock);
    drop_queued_locked(false);
    need_keyframe = true;
    keyframe_req = true;                   // ARCHITECTURE: sent on connect
    stats.state = NET_CONNECTED;
    stats.sender[0] = '\0';
    LightLock_Unlock(&lock);
}

static void net_thread(void* arg)
{
    (void)arg;
    int lsock = -1, csock = -1;
    Rx rx;
    memset(&rx, 0, sizeof(rx));

    while (running) {
        if (lsock < 0) {
            lsock = open_listener();
            if (lsock < 0) {               // e.g. WiFi off: retry every second
                set_state(NET_ERROR);
                svcSleepThread(1000000000LL);
                continue;
            }
            set_state(NET_IDLE);
        }

        struct pollfd fds[2] = {
            { .fd = lsock, .events = POLLIN },
            { .fd = csock, .events = POLLIN },
        };
        int n = poll(fds, 2, POLL_MS);
        if (n < 0) {
            // Listener died (e.g. WiFi dropped); rebuild everything.
            if (csock >= 0) { close(csock); csock = -1; rx_reset(&rx); }
            close(lsock);
            lsock = -1;
            svcSleepThread(100000000LL);
            continue;
        }

        if (fds[0].revents & POLLIN) {
            int fd = accept(lsock, NULL, NULL);
            if (fd >= 0) {
                if (csock >= 0) close(csock);   // new sender replaces the old one
                csock = fd;
                set_nonblocking(csock);
                int rcvbuf = 256 * 1024;
                setsockopt(csock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
                rx_reset(&rx);
                on_new_client();
                log_line("net: sender connected");
                send_postmortem(csock);
            }
        }

        if (csock >= 0 && fds[1].fd == csock && (fds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
            if (!rx_pump(csock, &rx)) {
                log_line("net: sender disconnected");
                close(csock);
                csock = -1;
                rx_reset(&rx);
                set_state(NET_IDLE);
            }
        }

        if (csock >= 0) {
            LightLock_Lock(&lock);
            bool want = keyframe_req;
            LightLock_Unlock(&lock);
            if (want && send_keyframe_req(csock)) {
                log_line("net: KEYFRAME_REQ sent");
                LightLock_Lock(&lock);
                keyframe_req = false;
                LightLock_Unlock(&lock);
            }
        }
    }

    if (csock >= 0) close(csock);
    if (lsock >= 0) close(lsock);
    rx_reset(&rx);
}

// ---------------------------------------------------------------------------

bool net_start(void)
{
    LightLock_Init(&lock);
    CondVar_Init(&cv);
    memset(&stats, 0, sizeof(stats));
    q_count = 0;
    need_keyframe = true;
    keyframe_req = false;

    for (int i = 0; i < NUM_SLOTS; i++) {
        slots[i].data = malloc(NET_MAX_PAYLOAD);
        if (!slots[i].data) return false;
        slot_free[i] = true;
    }

    soc_buf = memalign(SOC_ALIGN, SOC_BUFSIZE);
    if (!soc_buf) return false;
    if (R_FAILED(socInit(soc_buf, SOC_BUFSIZE))) {
        free(soc_buf);
        soc_buf = NULL;
        return false;
    }

    // Lower priority than the main (decode) thread; it mostly sleeps in poll().
    s32 prio = 0x30;
    svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
    if (prio < 0x3F) prio++;

    running = true;
    // Prefer a core other than the main thread's: core 2 (New 3DS app core),
    // then core 1 (needs an app CPU time limit), then the default core.
    net_thread_handle = threadCreate(net_thread, NULL, 32 * 1024, prio, 2, false);
    if (!net_thread_handle) {
        APT_SetAppCpuTimeLimit(30);
        net_thread_handle = threadCreate(net_thread, NULL, 32 * 1024, prio, 1, false);
    }
    if (!net_thread_handle)
        net_thread_handle = threadCreate(net_thread, NULL, 32 * 1024, prio, -2, false);
    if (!net_thread_handle) {
        running = false;
        return false;
    }
    return true;
}

void net_stop(void)
{
    running = false;
    if (net_thread_handle) {
        threadJoin(net_thread_handle, U64_MAX);
        threadFree(net_thread_handle);
        net_thread_handle = NULL;
    }
    if (soc_buf) {
        socExit();
        free(soc_buf);
        soc_buf = NULL;
    }
    for (int i = 0; i < NUM_SLOTS; i++) {
        free(slots[i].data);
        slots[i].data = NULL;
    }
}
