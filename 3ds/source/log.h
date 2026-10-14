#pragma once

#include <3ds.h>

// Crash-safe diagnostic log on the SD card (sdmc:/3ds/3DSStream/log.txt). Every line is
// written with O_SYNC (FS_WRITE_FLUSH), so it survives a sysmodule crash / hard reboot.
// The previous run's log is kept as log_prev.txt.
#define LOG_DIR       "sdmc:/3ds/3DSStream"
#define LOG_PATH      LOG_DIR "/log.txt"
#define LOG_PREV_PATH LOG_DIR "/log_prev.txt"
#define MODE_PATH     LOG_DIR "/mode.txt"

void log_open(void);    // rotates log.txt -> log_prev.txt, then opens a fresh log.txt
void log_close(void);
void log_line(const char* fmt, ...) __attribute__((format(printf, 1, 2)));  // thread-safe

// Reads the last `max` bytes of log_prev.txt into a malloc'd buffer (caller frees).
// Returns NULL if there is no previous log.
char* log_read_prev(u32 max, u32* len_out);
