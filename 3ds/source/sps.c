#include "sps.h"

#include <string.h>

// ---------------------------------------------------------------------------
// Bit reader / writer over RBSP bytes (emulation prevention removed)

typedef struct {
    const uint8_t* b;
    size_t nbits, pos;
    int err;
} BitReader;

static uint32_t rd(BitReader* r, int n)
{
    uint32_t v = 0;
    if (n > 32 || r->pos + (size_t)n > r->nbits) {
        r->err = 1;
        r->pos = r->nbits;
        return 0;
    }
    for (int i = 0; i < n; i++, r->pos++)
        v = (v << 1) | ((r->b[r->pos >> 3] >> (7 - (r->pos & 7))) & 1);
    return v;
}

static uint32_t rd_ue(BitReader* r)
{
    int zeros = 0;
    while (!r->err && rd(r, 1) == 0) {
        if (++zeros > 31) {
            r->err = 1;
            return 0;
        }
    }
    if (r->err || zeros == 0) return 0;
    return (uint32_t)(((uint64_t)1 << zeros) - 1 + rd(r, zeros));
}

static int32_t rd_se(BitReader* r)
{
    uint32_t k = rd_ue(r);
    return (k & 1) ? (int32_t)((k + 1) / 2) : -(int32_t)(k / 2);
}

typedef struct {
    uint8_t b[SPS_OUT_BYTES];
    size_t nbits;
    int err;
} BitWriter;

static void wr(BitWriter* w, uint32_t v, int n)
{
    for (int i = n - 1; i >= 0; i--, w->nbits++) {
        if ((w->nbits >> 3) >= sizeof(w->b)) {
            w->err = 1;
            return;
        }
        if ((w->nbits & 7) == 0) w->b[w->nbits >> 3] = 0;
        if ((v >> i) & 1) w->b[w->nbits >> 3] |= (uint8_t)(0x80 >> (w->nbits & 7));
    }
}

static void wr_ue(BitWriter* w, uint32_t v)
{
    uint64_t x = (uint64_t)v + 1;
    int len = 0;
    for (uint64_t t = x; t; t >>= 1) len++;
    wr(w, 0, len - 1);
    for (int i = len - 1; i >= 0; i--) wr(w, (uint32_t)((x >> i) & 1), 1);
}

static void copy_bits(BitWriter* w, const uint8_t* src, size_t n)
{
    for (size_t p = 0; p < n; p++) wr(w, (src[p >> 3] >> (7 - (p & 7))) & 1, 1);
}

// ---------------------------------------------------------------------------
// Emulation prevention

static size_t unescape(const uint8_t* in, size_t len, uint8_t* out)
{
    size_t n = 0;
    int zeros = 0;
    for (size_t i = 0; i < len; i++) {
        if (zeros >= 2 && in[i] == 3) {
            zeros = 0;
            continue;
        }
        out[n++] = in[i];
        zeros = in[i] == 0 ? zeros + 1 : 0;
    }
    return n;
}

// Returns the escaped length, or 0 if it does not fit.
static size_t escape(const uint8_t* in, size_t len, uint8_t* out, size_t cap)
{
    size_t n = 0;
    int zeros = 0;
    for (size_t i = 0; i < len; i++) {
        if (zeros >= 2 && in[i] <= 3) {
            if (n >= cap) return 0;
            out[n++] = 3;
            zeros = 0;
        }
        if (n >= cap) return 0;
        out[n++] = in[i];
        zeros = in[i] == 0 ? zeros + 1 : 0;
    }
    return n;
}

// ---------------------------------------------------------------------------
// Parsing

typedef struct {
    SpsInfo info;
    size_t vui_flag_bit;          // bit offset of vui_parameters_present_flag
    size_t restriction_flag_bit;  // bit offset of bitstream_restriction_flag (with VUI)
} Parsed;

static int is_high_profile(uint32_t p)
{
    static const uint32_t high[] = { 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135 };
    for (size_t i = 0; i < sizeof(high) / sizeof(high[0]); i++)
        if (high[i] == p) return 1;
    return 0;
}

static void skip_scaling_list(BitReader* r, int size)
{
    int32_t last = 8, next = 8;
    for (int i = 0; i < size && !r->err; i++) {
        if (next != 0) next = (last + rd_se(r) + 256) % 256;
        last = next == 0 ? last : next;
    }
}

static void skip_hrd(BitReader* r)
{
    uint32_t cpb_cnt = rd_ue(r) + 1;
    if (cpb_cnt > 32) {
        r->err = 1;
        return;
    }
    rd(r, 4);   // bit_rate_scale
    rd(r, 4);   // cpb_size_scale
    for (uint32_t i = 0; i < cpb_cnt && !r->err; i++) {
        rd_ue(r);   // bit_rate_value_minus1
        rd_ue(r);   // cpb_size_value_minus1
        rd(r, 1);   // cbr_flag
    }
    rd(r, 5);   // initial_cpb_removal_delay_length_minus1
    rd(r, 5);   // cpb_removal_delay_length_minus1
    rd(r, 5);   // dpb_output_delay_length_minus1
    rd(r, 5);   // time_offset_length
}

// Parses RBSP bytes (header byte included). Returns 0 on success.
static int parse_rbsp(const uint8_t* rbsp, size_t len, Parsed* p)
{
    BitReader rr = { rbsp, len * 8, 0, 0 };
    BitReader* r = &rr;
    SpsInfo* s = &p->info;
    memset(p, 0, sizeof(*p));

    uint32_t header = rd(r, 8);
    if ((header & 0x80) || (header & 0x1F) != 7) return -1;
    s->profile_idc = rd(r, 8);
    rd(r, 8);   // constraint flags
    s->level_idc = rd(r, 8);
    if (rd_ue(r) > 31) return -1;   // seq_parameter_set_id

    if (is_high_profile(s->profile_idc)) {
        uint32_t chroma = rd_ue(r);
        if (chroma > 3) return -1;
        if (chroma == 3) rd(r, 1);   // separate_colour_plane_flag
        rd_ue(r);                    // bit_depth_luma_minus8
        rd_ue(r);                    // bit_depth_chroma_minus8
        rd(r, 1);                    // qpprime_y_zero_transform_bypass_flag
        if (rd(r, 1)) {              // seq_scaling_matrix_present_flag
            int lists = chroma == 3 ? 12 : 8;
            for (int i = 0; i < lists; i++)
                if (rd(r, 1)) skip_scaling_list(r, i < 6 ? 16 : 64);
        }
    }

    if (rd_ue(r) > 12) return -1;   // log2_max_frame_num_minus4
    s->pic_order_cnt_type = rd_ue(r);
    if (s->pic_order_cnt_type == 0) {
        if (rd_ue(r) > 12) return -1;   // log2_max_pic_order_cnt_lsb_minus4
    } else if (s->pic_order_cnt_type == 1) {
        rd(r, 1);    // delta_pic_order_always_zero_flag
        rd_se(r);    // offset_for_non_ref_pic
        rd_se(r);    // offset_for_top_to_bottom_field
        uint32_t n = rd_ue(r);
        if (n > 255) return -1;
        for (uint32_t i = 0; i < n && !r->err; i++) rd_se(r);
    } else if (s->pic_order_cnt_type != 2) {
        return -1;
    }
    s->max_num_ref_frames = rd_ue(r);
    if (s->max_num_ref_frames > 16) return -1;
    rd(r, 1);   // gaps_in_frame_num_value_allowed_flag
    s->width_mbs = rd_ue(r) + 1;
    s->height_map_units = rd_ue(r) + 1;
    if (!rd(r, 1)) rd(r, 1);   // frame_mbs_only_flag, mb_adaptive_frame_field_flag
    rd(r, 1);   // direct_8x8_inference_flag
    if (rd(r, 1)) {            // frame_cropping_flag
        for (int i = 0; i < 4; i++) rd_ue(r);
    }

    p->vui_flag_bit = r->pos;
    if (rd(r, 1)) {
        s->has_vui = 1;
        if (rd(r, 1)) {                        // aspect_ratio_info_present_flag
            if (rd(r, 8) == 255) {             // Extended_SAR
                rd(r, 16);
                rd(r, 16);
            }
        }
        if (rd(r, 1)) rd(r, 1);                // overscan info / appropriate
        if (rd(r, 1)) {                        // video_signal_type_present_flag
            rd(r, 3);                          // video_format
            rd(r, 1);                          // video_full_range_flag
            if (rd(r, 1)) {                    // colour_description_present_flag
                rd(r, 8);
                rd(r, 8);
                rd(r, 8);
            }
        }
        if (rd(r, 1)) {                        // chroma_loc_info_present_flag
            rd_ue(r);
            rd_ue(r);
        }
        if (rd(r, 1)) {                        // timing_info_present_flag
            rd(r, 32);
            rd(r, 32);
            rd(r, 1);
        }
        int nal_hrd = (int)rd(r, 1);
        if (nal_hrd) skip_hrd(r);
        int vcl_hrd = (int)rd(r, 1);
        if (vcl_hrd) skip_hrd(r);
        if (nal_hrd || vcl_hrd) rd(r, 1);      // low_delay_hrd_flag
        rd(r, 1);                              // pic_struct_present_flag
        p->restriction_flag_bit = r->pos;
        if (rd(r, 1)) {
            s->has_restriction = 1;
            rd(r, 1);                          // motion_vectors_over_pic_boundaries_flag
            rd_ue(r);                          // max_bytes_per_pic_denom
            rd_ue(r);                          // max_bits_per_mb_denom
            rd_ue(r);                          // log2_max_mv_length_horizontal
            rd_ue(r);                          // log2_max_mv_length_vertical
            s->max_num_reorder_frames = rd_ue(r);
            s->max_dec_frame_buffering = rd_ue(r);
        }
    }

    // rbsp_trailing_bits: a one, then zeros to the end. Anything else (unknown extensions,
    // a misparse) means we do not understand this SPS.
    if (r->err || rd(r, 1) != 1) return -1;
    while (r->pos < r->nbits)
        if (rd(r, 1) != 0) return -1;
    return r->err ? -1 : 0;
}

int sps_parse(const uint8_t* nal, size_t len, SpsInfo* info)
{
    uint8_t rbsp[SPS_MAX_BYTES];
    Parsed p;
    if (len == 0 || len > SPS_MAX_BYTES) return -1;
    if (parse_rbsp(rbsp, unescape(nal, len, rbsp), &p) != 0) return -1;
    if (info) *info = p.info;
    return 0;
}

// ---------------------------------------------------------------------------
// Rewrite

static int bits_equal(const uint8_t* a, const uint8_t* b, size_t nbits)
{
    for (size_t i = 0; i < nbits; i++)
        if (((a[i >> 3] >> (7 - (i & 7))) & 1) != ((b[i >> 3] >> (7 - (i & 7))) & 1)) return 0;
    return 1;
}

SpsResult sps_rewrite(const uint8_t* in, size_t in_len, uint8_t* out, size_t out_cap,
                      size_t* out_len, SpsInfo* info)
{
    uint8_t rbsp[SPS_MAX_BYTES];
    Parsed p;
    if (in_len == 0 || in_len > SPS_MAX_BYTES) return SPS_PARSE_FAILED;
    size_t rlen = unescape(in, in_len, rbsp);
    if (parse_rbsp(rbsp, rlen, &p) != 0) return SPS_PARSE_FAILED;
    if (info) *info = p.info;
    if (p.info.has_restriction && p.info.max_num_reorder_frames == 0) return SPS_ALREADY_OK;

    uint32_t dec_buf = p.info.max_num_ref_frames > 1 ? p.info.max_num_ref_frames : 1;
    BitWriter w;
    memset(&w, 0, sizeof(w));
    size_t prefix;
    if (p.info.has_vui) {
        prefix = p.restriction_flag_bit;
        copy_bits(&w, rbsp, prefix);
    } else {
        prefix = p.vui_flag_bit;
        copy_bits(&w, rbsp, prefix);
        wr(&w, 1, 1);   // vui_parameters_present_flag
        wr(&w, 0, 8);   // aspect, overscan, video signal, chroma loc, timing, NAL/VCL HRD, pic_struct
    }
    wr(&w, 1, 1);       // bitstream_restriction_flag
    wr(&w, 1, 1);       // motion_vectors_over_pic_boundaries_flag
    wr_ue(&w, 2);       // max_bytes_per_pic_denom
    wr_ue(&w, 1);       // max_bits_per_mb_denom
    wr_ue(&w, 16);      // log2_max_mv_length_horizontal
    wr_ue(&w, 16);      // log2_max_mv_length_vertical
    wr_ue(&w, 0);       // max_num_reorder_frames
    wr_ue(&w, dec_buf); // max_dec_frame_buffering
    wr(&w, 1, 1);       // rbsp_stop_one_bit
    while (w.nbits & 7) wr(&w, 0, 1);
    if (w.err) return SPS_PARSE_FAILED;

    size_t n = escape(w.b, w.nbits >> 3, out, out_cap);
    if (n == 0) return SPS_PARSE_FAILED;

    // Paranoia: the result must parse back with the new restriction, the same leading bits
    // and the same picture parameters.
    uint8_t check[SPS_OUT_BYTES];
    Parsed q;
    if (n > SPS_MAX_BYTES) return SPS_PARSE_FAILED;
    size_t clen = unescape(out, n, check);
    if (parse_rbsp(check, clen, &q) != 0 || !q.info.has_restriction || q.info.max_num_reorder_frames != 0 ||
        q.info.max_dec_frame_buffering != dec_buf || q.info.max_num_ref_frames != p.info.max_num_ref_frames ||
        q.info.width_mbs != p.info.width_mbs || q.info.height_map_units != p.info.height_map_units ||
        !bits_equal(rbsp, check, prefix))
        return SPS_PARSE_FAILED;

    *out_len = n;
    return SPS_REWRITTEN;
}
