// Host tests for source/sps.c (built with the Mac's cc; see run_tests.sh / `make test-host`).
//
//   sps_test                      unit tests on fixed SPS vectors
//   sps_test IN.h264 OUT.h264     rewrites every SPS in an Annex-B file, copies everything else
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/sps.h"

static int failures;

#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
            failures++;                                                      \
        }                                                                    \
    } while (0)

static int no_start_code_emulation(const uint8_t* b, size_t n)
{
    for (size_t i = 0; i + 2 < n; i++)
        if (b[i] == 0 && b[i + 1] == 0 && b[i + 2] <= 3) return 0;
    return 1;
}

static void test_rewrite(const char* name, const uint8_t* sps, size_t len, SpsResult expected,
                         uint32_t expect_refs)
{
    uint8_t out[SPS_OUT_BYTES];
    size_t out_len = 0;
    SpsInfo before, after;
    printf("%s (%zu bytes)\n", name, len);
    SpsResult r = sps_rewrite(sps, len, out, sizeof(out), &out_len, &before);
    CHECK(r == expected);
    if (r == SPS_PARSE_FAILED) return;
    CHECK(before.max_num_ref_frames == expect_refs);
    printf("  profile %u level %u poc %u refs %u %ux%u MBs vui %d restriction %d\n", before.profile_idc,
           before.level_idc, before.pic_order_cnt_type, before.max_num_ref_frames, before.width_mbs,
           before.height_map_units, before.has_vui, before.has_restriction);
    if (r != SPS_REWRITTEN) return;

    printf("  rewritten: %zu bytes:", out_len);
    for (size_t i = 0; i < out_len; i++) printf(" %02x", out[i]);
    printf("\n");
    CHECK(no_start_code_emulation(out, out_len));
    CHECK(sps_parse(out, out_len, &after) == 0);
    CHECK(after.has_vui && after.has_restriction);
    CHECK(after.max_num_reorder_frames == 0);
    CHECK(after.max_dec_frame_buffering == (expect_refs > 1 ? expect_refs : 1));
    CHECK(after.profile_idc == before.profile_idc && after.level_idc == before.level_idc);
    CHECK(after.width_mbs == before.width_mbs && after.height_map_units == before.height_map_units);
    CHECK(after.pic_order_cnt_type == before.pic_order_cnt_type);

    // Idempotent: the rewritten SPS is left alone.
    uint8_t again[SPS_OUT_BYTES];
    size_t again_len = 0;
    CHECK(sps_rewrite(out, out_len, again, sizeof(again), &again_len, NULL) == SPS_ALREADY_OK);
}

// Minimal MSB-first bit writer for building a synthetic SPS.
typedef struct { uint8_t b[64]; size_t n; } W;
static void put(W* w, uint32_t v, int bits)
{
    for (int i = bits - 1; i >= 0; i--, w->n++)
        if ((v >> i) & 1) w->b[w->n >> 3] |= (uint8_t)(0x80 >> (w->n & 7));
}
static void put_ue(W* w, uint32_t v)
{
    int len = 0;
    for (uint32_t t = v + 1; t; t >>= 1) len++;
    put(w, 0, len - 1);
    put(w, v + 1, len);
}

static int unit_tests(void)
{
    // (a) VideoToolbox SPS captured before S3DSKit's rewrite (S3DSKit SPSRewriterTests.swift):
    // Constrained Baseline 3.0, 240x400, POC type 0, 2 refs, VUI with colour + NAL HRD,
    // no bitstream_restriction.
    static const uint8_t vt[] = { 0x27, 0x42, 0xE0, 0x1E, 0xA9, 0x18, 0x78, 0x67, 0x60, 0x2D,
                                  0x40, 0x80, 0x80, 0x8C, 0x2B, 0x5E, 0xF7, 0xC0, 0x40 };
    test_rewrite("VideoToolbox SPS", vt, sizeof(vt), SPS_REWRITTEN, 2);

    // (b) x264 baseline 240x400 SPS (has restriction with reorder 0; contains 00 00 03).
    static const uint8_t x264[] = { 0x67, 0x42, 0xc0, 0x1e, 0xda, 0x0f, 0x0c, 0xec, 0x04, 0x40, 0x00,
                                    0x00, 0x03, 0x00, 0x40, 0x00, 0x00, 0x1e, 0x23, 0xc5, 0x8b, 0xa8 };
    test_rewrite("x264 SPS", x264, sizeof(x264), SPS_ALREADY_OK, 1);

    // (c) Synthetic Baseline SPS without VUI, 3 refs, POC type 0.
    W w;
    memset(&w, 0, sizeof(w));
    put(&w, 0x67, 8);
    put(&w, 66, 8);
    put(&w, 0xC0, 8);
    put(&w, 30, 8);
    put_ue(&w, 0);   // sps id
    put_ue(&w, 0);   // log2_max_frame_num_minus4
    put_ue(&w, 0);   // poc type
    put_ue(&w, 2);   // log2_max_poc_lsb_minus4
    put_ue(&w, 3);   // max_num_ref_frames
    put(&w, 0, 1);   // gaps
    put_ue(&w, 14);  // width 240
    put_ue(&w, 24);  // height 400
    put(&w, 1, 1);   // frame_mbs_only
    put(&w, 1, 1);   // direct_8x8
    put(&w, 0, 1);   // cropping
    put(&w, 0, 1);   // vui
    put(&w, 1, 1);   // stop bit
    test_rewrite("synthetic no-VUI SPS", w.b, (w.n + 7) / 8, SPS_REWRITTEN, 3);

    // (d) Garbage must be refused, not misparsed.
    static const uint8_t junk[] = { 0x67, 0xff, 0xff, 0xff, 0xff };
    test_rewrite("garbage", junk, sizeof(junk), SPS_PARSE_FAILED, 0);
    static const uint8_t pps[] = { 0x68, 0xce, 0x3c, 0x80 };
    test_rewrite("PPS (not an SPS)", pps, sizeof(pps), SPS_PARSE_FAILED, 0);

    printf(failures ? "\nFAILED: %d check(s)\n" : "\nall unit tests passed\n", failures);
    return failures ? 1 : 0;
}

// Rewrites every SPS NAL of an Annex-B file. Output uses 4-byte start codes.
static int rewrite_file(const char* in_path, const char* out_path)
{
    FILE* f = fopen(in_path, "rb");
    if (!f) { perror(in_path); return 1; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t* d = malloc((size_t)size);
    if (!d || fread(d, 1, (size_t)size, f) != (size_t)size) { fclose(f); return 1; }
    fclose(f);
    FILE* o = fopen(out_path, "wb");
    if (!o) { perror(out_path); return 1; }

    size_t n = (size_t)size, i = 0, sps_total = 0, rewritten = 0, ok = 0, failed = 0;
    while (i + 3 <= n && !(d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 1)) i++;
    while (i + 3 <= n) {
        size_t start = i + 3, next = start;
        while (next + 3 <= n && !(d[next] == 0 && d[next + 1] == 0 && d[next + 2] == 1)) next++;
        if (next + 3 > n) next = n;
        size_t end = next;
        while (end > start && d[end - 1] == 0) end--;
        static const uint8_t sc[] = { 0, 0, 0, 1 };
        fwrite(sc, 1, 4, o);
        if (end > start && (d[start] & 0x1F) == 7) {
            uint8_t out[SPS_OUT_BYTES];
            size_t out_len = 0;
            sps_total++;
            SpsResult r = sps_rewrite(d + start, end - start, out, sizeof(out), &out_len, NULL);
            if (r == SPS_REWRITTEN) { fwrite(out, 1, out_len, o); rewritten++; }
            else { fwrite(d + start, 1, end - start, o); r == SPS_ALREADY_OK ? ok++ : failed++; }
        } else {
            fwrite(d + start, 1, end - start, o);
        }
        i = next;
    }
    fclose(o);
    free(d);
    printf("%s: %zu SPS, %zu rewritten, %zu already ok, %zu failed\n", in_path, sps_total, rewritten, ok, failed);
    return failed ? 1 : 0;
}

int main(int argc, char** argv)
{
    if (argc == 3) return rewrite_file(argv[1], argv[2]);
    if (argc != 1) {
        fprintf(stderr, "usage: %s [IN.h264 OUT.h264]\n", argv[0]);
        return 2;
    }
    return unit_tests();
}
