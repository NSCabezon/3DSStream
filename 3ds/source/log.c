#include "log.h"

#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int fd = -1;
static LightLock lock;
static u64 t0;
static u32 lines;

// Every line is a synchronous SD write, far too slow for per-NAL tracing at 60 fps. Trace
// the startup and first frames in full, then only errors and network events.
#define VERBOSE_LINES 60

void log_open(void)
{
    LightLock_Init(&lock);
    t0 = osGetTime();
    mkdir("sdmc:/3ds", 0777);
    mkdir(LOG_DIR, 0777);
    remove(LOG_PREV_PATH);
    rename(LOG_PATH, LOG_PREV_PATH);
    // O_SYNC makes libctru pass FS_WRITE_FLUSH on every write().
    fd = open(LOG_PATH, O_WRONLY | O_CREAT | O_TRUNC | O_SYNC, 0666);
}

void log_close(void)
{
    if (fd >= 0) close(fd);
    fd = -1;
}

void log_line(const char* fmt, ...)
{
    if (fd < 0) return;
    if (lines >= VERBOSE_LINES && strncmp(fmt, "ERROR", 5) != 0 && strncmp(fmt, "net:", 4) != 0 &&
        strncmp(fmt, "audio:", 6) != 0)
        return;
    lines++;
    char buf[320];
    u64 ms = osGetTime() - t0;
    int n = snprintf(buf, sizeof(buf), "%6lu.%03lu ", (unsigned long)(ms / 1000), (unsigned long)(ms % 1000));
    va_list ap;
    va_start(ap, fmt);
    n += vsnprintf(buf + n, sizeof(buf) - n - 1, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof(buf) - 2) n = sizeof(buf) - 2;
    buf[n++] = '\n';
    LightLock_Lock(&lock);
    write(fd, buf, n);
    LightLock_Unlock(&lock);
}

char* log_read_prev(u32 max, u32* len_out)
{
    FILE* f = fopen(LOG_PREV_PATH, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    if (size <= 0) { fclose(f); return NULL; }
    u32 len = (u32)size > max ? max : (u32)size;
    fseek(f, size - (long)len, SEEK_SET);
    char* buf = malloc(len + 1);
    if (buf) {
        len = fread(buf, 1, len, f);
        buf[len] = '\0';
        *len_out = len;
    }
    fclose(f);
    return buf;
}
