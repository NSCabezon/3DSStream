#!/usr/bin/env python3
"""Builds the stereoscopic HOME Menu banner: resource/banner.cgfx.

Scene (units of the HOME banner camera, which sits at (0, 1, 44.786) looking down -z with
a 30 deg vertical FOV, so the z=0 plane spans 40x24 units = the 400x240 top screen and
1 unit = 10 px at screen depth). One static node, "Badge", centred on the vertical axis
through the origin, because HOME Menu spins banner models 360 deg around it:
  - a dark rounded-rectangle card (no full-bleed background: HOME masks the banner area
    with an oval), z = -0.2..+0.2, with a thin red (left) / cyan (right) rim on each face;
  - on each face, readable from its own side (the back is the front rotated 180 deg):
    "3D" (Archivo Black) extruded 1.4 out of the card, white face, red left-facing sides,
    cyan right-facing sides, slate top/bottom; "STREAMING" (Oswald SemiBold) 0.25 thick.
The whole badge is scaled so its projection stays inside the oval at every spin angle.

Pipeline: glyph outlines (fontTools) -> flattened contours -> earcut caps + extruded sides
-> .glb (written here by hand, no glTF writer needed) -> pycgfx (skyfloogle/pycgfx, pinned
commit, cloned into .tools/pycgfx because it ships without a license so we don't vendor it)
-> resource/banner.cgfx. bannertool LZ11-compresses the raw .cgfx itself (make cia passes it
with -ci); never pre-compress it.

Usage (from 3ds/):
  .tools/venv/bin/python scripts/make_banner_model.py [--preview out.png] [--previews [DIR]]
First run creates .tools/venv and installs the deps if you run it with any python3.
"""
import argparse
import json
import math
import os
import struct
import subprocess
import sys
import zlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS = os.path.join(ROOT, ".tools")
VENV_PY = os.path.join(TOOLS, "venv", "bin", "python")
PYCGFX_DIR = os.path.join(TOOLS, "pycgfx")
PYCGFX_URL = "https://github.com/skyfloogle/pycgfx.git"
PYCGFX_REV = "1f78850086f3a77c41e07162e842f97a5bf3c18a"
DEPS = ["gltflib", "pillow", "fonttools", "mapbox-earcut", "numpy", "usd-core"]

OUT_GLB = os.path.join(TOOLS, "banner.glb")
OUT_CGFX = os.path.join(ROOT, "resource", "banner.cgfx")
CGFX_MAX = 0x80000

FONTS = os.path.join(ROOT, "resource", "fonts")  # vendored, SIL Open Font License 1.1
FONT_BIG = os.path.join(FONTS, "ArchivoBlack-Regular.ttf")
FONT_SMALL = os.path.join(FONTS, "Oswald-SemiBold.ttf")  # Oswald[wght] instanced at 600

# HOME Menu banner camera (pycgfx banner-camera.gltf)
CAM_POS = (0.0, 1.0, 44.786)
CAM_YFOV = 0.523599
CAM_ASPECT = 400 / 240
CAM_ZNEAR = 26.5

# Layout (badge-local units, before the fit-to-oval scale)
LOGO_CAP = 7.0          # "3D" cap height (Archivo Black runs ~3% wider than the old face)
LOGO_DEPTH = 1.4        # "3D" extrusion out of each card face
SUB_CAP = 2.12          # "STREAMING" cap height
SUB_TRACKING = 0.08     # extra letter spacing, in cap heights
SUB_DEPTH = 0.25
LINE_GAP = 1.45         # between the bottom of "3D" and the top of "STREAMING"
CARD_THICK = 0.4
CARD_PAD = 0.08         # padding around logo + subtitle, fraction of the content size
CARD_RADIUS = 1.6       # corner radius
CARD_RIM = 0.2          # rim width
BADGE_Y = CAM_POS[1]    # badge centre height (on the camera axis: symmetric perspective)
# Footprint limit: max over all spin angles of the projected badge, as a radius of the
# ellipse inscribed in the nominal 40x24 view (1.0 = touching it). The hardware oval is
# about that ellipse or a little smaller; the previous card (approved size) was ~0.75.
OVAL_LIMIT = 0.80
TURNTABLE_PERIOD = 12.0  # USDZ preview only: seconds per 360 deg

# Colours (sRGB 0..1). Colour lives in a solid 8x8 texture per material; baseColorFactor
# stays white, so the HOME Menu's lighting multiplies a known-good texture path.
COL = {
    "logo_front": (0.96, 0.96, 0.98),
    "logo_back": (0.20, 0.21, 0.28),
    "logo_red": (0.95, 0.14, 0.22),
    "logo_cyan": (0.16, 0.86, 0.92),
    "logo_slate": (0.34, 0.37, 0.50),
    "sub_front": (0.92, 0.93, 0.97),
    "sub_side": (0.42, 0.45, 0.58),
    "card": (0.086, 0.094, 0.149),
    "card_side": (0.20, 0.22, 0.32),
    # USDZ-only HOME Menu mock-up (never in the CGFX)
    "home_theme": (0.93, 0.88, 0.62),
    "home_window": (0.30, 0.31, 0.34),
}


# --------------------------------------------------------------------------- tooling
def ensure_tooling():
    """Re-exec inside .tools/venv (creating it) and make sure pycgfx is cloned."""
    if os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(TOOLS, "venv")):
        if not os.path.exists(VENV_PY):
            subprocess.check_call([sys.executable, "-m", "venv", os.path.join(TOOLS, "venv")])
            subprocess.check_call([VENV_PY, "-m", "pip", "install", "-q", *DEPS])
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    if not os.path.isdir(os.path.join(PYCGFX_DIR, "cgfx")):
        subprocess.check_call(["git", "clone", "-q", PYCGFX_URL, PYCGFX_DIR])
    rev = subprocess.run(["git", "-C", PYCGFX_DIR, "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if rev != PYCGFX_REV:
        subprocess.check_call(["git", "-C", PYCGFX_DIR, "fetch", "-q", "origin", PYCGFX_REV])
        subprocess.check_call(["git", "-C", PYCGFX_DIR, "checkout", "-q", PYCGFX_REV])


# --------------------------------------------------------------------------- outlines
def glyph_contours(font_path, text, cap_height, tracking=0.0, step=0.3):
    """Flattened contours of `text`, scaled to `cap_height`, centred on the origin.
    Returns a list of polygons (lists of (x, y))."""
    from fontTools.ttLib import TTFont
    from fontTools.pens.basePen import BasePen

    font = TTFont(font_path)
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    cap_units = gs[cmap[ord("H")]]
    # measure H height via bounds
    from fontTools.pens.boundsPen import BoundsPen
    bp = BoundsPen(gs)
    cap_units.draw(bp)
    s = cap_height / (bp.bounds[3] - bp.bounds[1])

    class FlatPen(BasePen):
        def __init__(self, ox):
            super().__init__(gs)
            self.ox, self.contours, self.cur = ox, [], []

        def P(self, p):
            return (self.ox + p[0] * s, p[1] * s)

        def _moveTo(self, p):
            self.cur = [self.P(p)]

        def _lineTo(self, p):
            self.cur.append(self.P(p))

        def _bez(self, pts):
            p0 = self.cur[-1]
            ctrl = [self.P(p) for p in pts]
            allp = [p0] + ctrl
            length = sum(math.dist(a, b) for a, b in zip(allp, allp[1:]))
            n = max(1, min(8, math.ceil(length / step)))
            for i in range(1, n + 1):
                t = i / n
                q = allp
                while len(q) > 1:  # de Casteljau
                    q = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                         for a, b in zip(q, q[1:])]
                self.cur.append(q[0])

        def _qCurveToOne(self, p1, p2):
            self._bez([p1, p2])

        def _curveToOne(self, p1, p2, p3):
            self._bez([p1, p2, p3])

        def _closePath(self):
            if len(self.cur) > 2:
                self.contours.append(self.cur)
            self.cur = []

        _endPath = _closePath

    contours, x = [], 0.0
    for ch in text:
        g = gs[cmap[ord(ch)]]
        pen = FlatPen(x)
        g.draw(pen)
        contours += pen.contours
        x += g.width * s + tracking * cap_height
    x -= tracking * cap_height
    contours = [simplify(c) for c in contours]
    xs = [p[0] for c in contours for p in c]
    ys = [p[1] for c in contours for p in c]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return [[(px - cx, py - cy) for px, py in c] for c in contours]


def simplify(c, min_len=0.02, min_angle=math.radians(3)):
    pts = [p for i, p in enumerate(c) if math.dist(p, c[i - 1]) > min_len] or c
    changed = True
    while changed and len(pts) > 3:
        changed = False
        out = []
        for i, p in enumerate(pts):
            a, b = pts[i - 1], pts[(i + 1) % len(pts)]
            v1 = (p[0] - a[0], p[1] - a[1])
            v2 = (b[0] - p[0], b[1] - p[1])
            ang = abs(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))
            if ang < min_angle and not changed:
                changed = True
                continue
            out.append(p)
        pts = out
    return pts


def signed_area(c):
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(c, c[1:] + c[:1]))


def inside(pt, poly):
    x, y, res = pt[0], pt[1], False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            res = not res
    return res


def group_shapes(contours):
    """[(outer, [holes])] with outers CCW and holes CW."""
    depth = [sum(inside(c[0], o) for o in contours if o is not c) for c in contours]
    shapes = []
    for c, d in zip(contours, depth):
        if d % 2 == 0:
            shapes.append((c if signed_area(c) > 0 else c[::-1], []))
    for c, d in zip(contours, depth):
        if d % 2 == 1:
            hole = c if signed_area(c) < 0 else c[::-1]
            for outer, holes in shapes:
                if inside(hole[0], outer):
                    holes.append(hole)
                    break
    return shapes


# --------------------------------------------------------------------------- meshes
class Mesh:
    """Per-material triangle soup with flat-ish normals and UVs."""

    def __init__(self):
        self.parts = {}  # material -> (positions, normals, indices)

    def part(self, mat):
        return self.parts.setdefault(mat, ([], [], []))

    def tri(self, mat, verts, normals, facing):
        """Add a triangle; flips winding so its geometric normal agrees with `facing`."""
        pos, nrm, idx = self.part(mat)
        a, b, c = verts
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        order = (0, 1, 2) if sum(n[i] * facing[i] for i in range(3)) >= 0 else (0, 2, 1)
        base = len(pos)
        for k in order:
            pos.append(tuple(verts[k]))
            nrm.append(tuple(normals[k]))
        idx += [base, base + 1, base + 2]

    def compact(self):
        """Merge identical (position, normal) vertices."""
        for mat, (pos, nrm, idx) in list(self.parts.items()):
            seen, npos, nnrm, nidx = {}, [], [], []
            for i in idx:
                key = (tuple(round(x, 5) for x in pos[i]), tuple(round(x, 4) for x in nrm[i]))
                if key not in seen:
                    seen[key] = len(npos)
                    npos.append(pos[i])
                    nnrm.append(nrm[i])
                nidx.append(seen[key])
            self.parts[mat] = (npos, nnrm, nidx)


def norm(v):
    l = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / l for x in v)


def extrude(mesh, contours, depth, mats):
    """Solid extrusion centred on z=0. mats: front, back, side_fn(nx, ny) -> material.
    Side faces get the front's +z normal on purpose: the HOME Menu light is not exactly
    along -z, and real (or tilted) side normals left the red left-facing sides dark on
    hardware (only cyan showed). With +z every face renders its texture colour as lit as
    the white front; the red/cyan/slate materials alone carry the shape."""
    import mapbox_earcut as earcut
    import numpy as np

    zf, zb = depth / 2, -depth / 2
    for outer, holes in group_shapes(contours):
        rings = [outer] + holes
        flat = np.array([p for r in rings for p in r], dtype=np.float64)
        ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
        tris = earcut.triangulate_float64(flat, ends).reshape(-1, 3)
        for t in tris:
            p = [flat[i] for i in t]
            mesh.tri(mats["front"], [(q[0], q[1], zf) for q in p], [(0, 0, 1)] * 3, (0, 0, 1))
            mesh.tri(mats["back"], [(q[0], q[1], zb) for q in p], [(0, 0, -1)] * 3, (0, 0, -1))
        up = (0, 0, 1)
        for ring in rings:
            n = len(ring)
            en = []
            for i in range(n):  # outward edge normals (outer CCW, holes CW)
                a, b = ring[i], ring[(i + 1) % n]
                en.append(norm((b[1] - a[1], -(b[0] - a[0]))))
            for i in range(n):
                a, b = ring[i], ring[(i + 1) % n]
                e = en[i]
                mat = mats["side"](e[0], e[1])
                facing = (e[0], e[1], 0)
                A, B = (a[0], a[1], zf), (b[0], b[1], zf)
                C, D = (b[0], b[1], zb), (a[0], a[1], zb)
                mesh.tri(mat, [A, B, C], [up] * 3, facing)
                mesh.tri(mat, [A, C, D], [up] * 3, facing)


def logo_side(nx, ny):
    if nx < -0.25:
        return "logo_red"
    if nx > 0.25:
        return "logo_cyan"
    return "logo_slate"


def rounded_rect(hw, hh, r, seg=8):
    """CCW rounded rectangle centred on the origin."""
    pts = []
    for cx, cy, a0 in ((hw - r, hh - r, 0), (-hw + r, hh - r, 90),
                       (-hw + r, -hh + r, 180), (hw - r, -hh + r, 270)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def card_mesh(hw, hh):
    """Dark card centred on z=0 with a thin rim on its front face (+z): red on
    left-facing stretches, cyan on right-facing ones, slate along the top and bottom.
    The back face's rim is added by the caller (a rotated copy)."""
    m = Mesh()
    outer = rounded_rect(hw, hh, CARD_RADIUS)
    extrude(m, [outer], CARD_THICK, {"front": "card", "back": "card",
                                     "side": lambda nx, ny: "card_side"})
    return m


def rim_mesh(hw, hh, z):
    m = Mesh()
    outer = rounded_rect(hw, hh, CARD_RADIUS)
    inner = rounded_rect(hw - CARD_RIM, hh - CARD_RIM, CARD_RADIUS - CARD_RIM)
    up = (0, 0, 1)
    n = len(outer)
    for i in range(n):
        a, b = outer[i], outer[(i + 1) % n]
        c, d = inner[(i + 1) % n], inner[i]
        e = norm((b[1] - a[1], -(b[0] - a[0])))
        A, B, C, D = ((p[0], p[1], z) for p in (a, b, c, d))
        m.tri(logo_side(e[0], e[1]), [A, B, C], [up] * 3, up)
        m.tri(logo_side(e[0], e[1]), [A, C, D], [up] * 3, up)
    return m


def merge(dst, src, offset=(0.0, 0.0, 0.0), back=False, scale=1.0):
    """Append src into dst, translated by offset; back=True rotates it 180 deg about the
    vertical axis (x, z -> -x, -z) so it faces -z and reads correctly from behind."""
    ox, oy, oz = offset
    for mat, (pos, nrm, idx) in src.parts.items():
        dpos, dnrm, didx = dst.part(mat)
        base = len(dpos)
        for (x, y, z), (nx, ny, nz) in zip(pos, nrm):
            x, y, z = x + ox, y + oy, z + oz
            if back:
                x, z, nx, nz = -x, -z, -nx, -nz
            dpos.append((x * scale, y * scale, z * scale))
            dnrm.append((nx, ny, nz))
        didx += [base + i for i in idx]


def footprint(mesh, step=5):
    """Max normalised radius (see OVAL_LIMIT) of the badge's projection over a full spin."""
    D, (vw, vh) = CAM_POS[2], (CAM_POS[2] * math.tan(CAM_YFOV / 2) * CAM_ASPECT,
                               CAM_POS[2] * math.tan(CAM_YFOV / 2))
    pts = [p for pos, _, _ in mesh.parts.values() for p in pos]
    worst = 0.0
    for deg in range(0, 360, step):
        c, s_ = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        for x, y, z in pts:
            x, z = x * c + z * s_, -x * s_ + z * c
            k = D / (D - z)
            worst = max(worst, math.hypot(x * k / vw, (y + BADGE_Y - CAM_POS[1]) * k / vh))
    return worst


def build_scene():
    logo = Mesh()
    extrude(logo, glyph_contours(FONT_BIG, "3D", LOGO_CAP, step=0.25), LOGO_DEPTH,
            {"front": "logo_front", "back": "logo_back", "side": logo_side})
    sub = Mesh()
    sub_contours = glyph_contours(FONT_SMALL, "STREAMING", SUB_CAP, SUB_TRACKING, step=0.15)
    extrude(sub, sub_contours, SUB_DEPTH, {"front": "sub_front", "back": "sub_side",
                                           "side": lambda nx, ny: "sub_side"})
    total_h = LOGO_CAP + LINE_GAP + SUB_CAP
    logo_y, sub_y = total_h / 2 - LOGO_CAP / 2, -total_h / 2 + SUB_CAP / 2
    logo_w = max(p[0] for pos, _, _ in logo.parts.values() for p in pos) * 2
    sub_w = max(p[0] for c in sub_contours for p in c) * 2
    pad = CARD_PAD * max(logo_w, sub_w, total_h)
    hw, hh = max(logo_w, sub_w) / 2 + pad, total_h / 2 + pad

    face = Mesh()  # everything on the +z side of the card
    merge(face, rim_mesh(hw, hh, CARD_THICK / 2 + 0.02))
    merge(face, sub, (0.0, sub_y, CARD_THICK / 2 + SUB_DEPTH / 2))
    merge(face, logo, (0.0, logo_y, CARD_THICK / 2 + LOGO_DEPTH / 2))

    def assemble(scale):
        badge = Mesh()
        merge(badge, card_mesh(hw, hh), scale=scale)
        merge(badge, face, scale=scale)
        merge(badge, face, back=True, scale=scale)
        return badge

    scale = 1.0
    for _ in range(4):  # perspective makes it slightly non-linear: iterate
        r = footprint(assemble(scale))
        if r <= OVAL_LIMIT:
            break
        scale *= OVAL_LIMIT / r * 0.995
    badge = assemble(scale)
    badge.compact()
    print(f"badge: scale {scale:.3f}, card {2 * hw * scale:.1f} x {2 * hh * scale:.1f}, "
          f"footprint over a spin {footprint(badge):.2f} (limit {OVAL_LIMIT})")
    return [("Badge", badge, (0.0, BADGE_Y, 0.0))]


# --------------------------------------------------------------------------- glTF
def solid_png(rgb, size=8):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    px = bytes(round(c * 255) for c in rgb)
    raw = b"".join(b"\x00" + px * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_glb(scene, path):
    blob = bytearray()
    views, accessors, materials, images, textures, meshes, nodes = [], [], [], [], [], [], []
    mat_index = {}

    def add_view(data, target=None):
        while len(blob) % 4:
            blob.append(0)
        v = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            v["target"] = target
        blob.extend(data)
        views.append(v)
        return len(views) - 1

    def add_acc(data, ctype, count, typ, target=None, **extra):
        accessors.append({"bufferView": add_view(data, target), "componentType": ctype,
                          "count": count, "type": typ, **extra})
        return len(accessors) - 1

    def material(name):
        if name not in mat_index:
            images.append({"bufferView": add_view(solid_png(COL[name])), "mimeType": "image/png",
                           "name": f"tex_{name}"})
            textures.append({"source": len(images) - 1})
            materials.append({"name": name, "doubleSided": True,
                              "pbrMetallicRoughness": {"baseColorTexture": {"index": len(textures) - 1},
                                                       "metallicFactor": 0.0, "roughnessFactor": 1.0}})
            mat_index[name] = len(materials) - 1
        return mat_index[name]

    tri_total = 0
    for node_name, mesh, translation in scene:
        prims = []
        for mat, (pos, nrm, idx) in mesh.parts.items():
            assert len(pos) < 65535
            mn = [min(p[i] for p in pos) for i in range(3)]
            mx = [max(p[i] for p in pos) for i in range(3)]
            a_pos = add_acc(struct.pack(f"<{len(pos) * 3}f", *[x for p in pos for x in p]),
                            5126, len(pos), "VEC3", 34962, min=mn, max=mx)
            a_nrm = add_acc(struct.pack(f"<{len(nrm) * 3}f", *[x for p in nrm for x in p]),
                            5126, len(nrm), "VEC3", 34962)
            uv = [(0.5, 0.5)] * len(pos)
            a_uv = add_acc(struct.pack(f"<{len(uv) * 2}f", *[x for p in uv for x in p]),
                           5126, len(uv), "VEC2", 34962)
            a_idx = add_acc(struct.pack(f"<{len(idx)}H", *idx), 5123, len(idx), "SCALAR", 34963)
            prims.append({"attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv},
                          "indices": a_idx, "material": material(mat), "mode": 4})
            tri_total += len(idx) // 3
        meshes.append({"name": node_name, "primitives": prims})
        nodes.append({"name": node_name, "mesh": len(meshes) - 1, "translation": list(translation)})

    gltf = {"asset": {"version": "2.0", "generator": "make_banner_model.py"},
            "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
            "nodes": nodes, "meshes": meshes, "materials": materials,
            "images": images, "textures": textures, "samplers": [], "accessors": accessors,
            "bufferViews": views}
    del gltf["samplers"]
    while len(blob) % 4:
        blob.append(0)
    gltf["buffers"] = [{"byteLength": len(blob)}]
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out = (struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(blob))
           + struct.pack("<II", len(js), 0x4E4F534A) + js
           + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)
    return tri_total


# --------------------------------------------------------------------------- preview
EYE_SEP_PREVIEW = 4.0  # stereo baseline for previews; an estimate of the 3DS at full slider
                       # ("3D" front ~1.25% of the width in front of the screen)


PREVIEW_THEME = (226, 214, 160)  # stands in for the HOME theme behind the banner


def render_eye(scene, eye_x, yaw_deg=0.0, fov_scale=1.0, W=400, H=240, SS=2):
    """Flat-shaded painter's-algorithm render through the banner camera; off-axis stereo
    (eye shifted by eye_x, converging at z=0, the 3DS screen plane)."""
    from PIL import Image, ImageDraw

    f = (H * SS / 2) / math.tan(CAM_YFOV * fov_scale / 2)
    img = Image.new("RGB", (W * SS, H * SS), PREVIEW_THEME)
    dr = ImageDraw.Draw(img)
    polys = []
    yaw = math.radians(yaw_deg)
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    for name, mesh, (tx, ty, tz) in scene:
        for mat, (pos, nrm, idx) in mesh.parts.items():
            col = COL[mat]
            for k in range(0, len(idx), 3):
                pts, ns = [], []
                for i in idx[k:k + 3]:
                    x, y, z = pos[i]
                    nx, ny, nz = nrm[i]
                    if name == "Badge":
                        x, z = x * cy_ + z * sy_, -x * sy_ + z * cy_
                        nx, nz = nx * cy_ + nz * sy_, -nx * sy_ + nz * cy_
                    pts.append((x + tx, y + ty, z + tz))
                    ns.append((nx, ny, nz))
                lit = max(0.0, sum(n[2] for n in ns) / 3)
                cam = []
                for x, y, z in pts:
                    dz = CAM_POS[2] - z
                    sx = (x - eye_x) * f / dz + eye_x * f / CAM_POS[2]
                    sy = (y - CAM_POS[1]) * f / dz
                    cam.append((W * SS / 2 + sx, H * SS / 2 - sy))
                # back-face cull in screen space (mesh is double sided; keep facing ones)
                (ax, ay), (bx, by), (cx, cy) = cam
                if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 0:
                    continue
                depth = sum(CAM_POS[2] - p[2] for p in pts) / 3
                polys.append((depth, cam, tuple(round(255 * c * lit) for c in col)))
    polys.sort(key=lambda p: -p[0])
    for _, cam, c in polys:
        dr.polygon(cam, fill=c)
    return img.resize((W, H), Image.LANCZOS)


def anaglyph(L, R):
    from PIL import Image
    return Image.merge("RGB", (L.split()[0], R.split()[1], R.split()[2]))


def render_preview(scene, path, eye_sep=0.8,
                   rows_spec=((0.0, 1.0), (30.0, 1.0), (90.0, 1.0), (180.0, 1.0))):
    """Contact sheet. Rows: (yaw, FOV scale). Columns: left, right, anaglyph. The grey
    ellipse inscribed in each frame approximates HOME Menu's oval banner mask."""
    from PIL import Image, ImageDraw

    W, H, gap = 400, 240, 8
    rows = []
    for yaw, fs in rows_spec:
        L = render_eye(scene, -eye_sep / 2, yaw, fs)
        R = render_eye(scene, eye_sep / 2, yaw, fs)
        row = [L, R, anaglyph(L, R)]
        for im in row:
            ImageDraw.Draw(im).ellipse((0, 0, W - 1, H - 1), outline=(120, 120, 120))
        rows.append(row)
    out = Image.new("RGB", (3 * W + 2 * gap, len(rows) * H + (len(rows) - 1) * gap), (255, 255, 255))
    for r, row in enumerate(rows):
        for c, im in enumerate(row):
            out.paste(im, (c * (W + gap), r * (H + gap)))
    out.save(path)


def write_previews(scene, outdir, eye_sep=EYE_SEP_PREVIEW):
    """outdir/banner_anaglyph.png (red/cyan), banner_sbs_parallel.png (left|right),
    banner_sbs_crosseyed.png (right|left), banner.usdz."""
    from PIL import Image, ImageDraw

    os.makedirs(outdir, exist_ok=True)
    W, H = 1600, 960
    L = render_eye(scene, -eye_sep / 2, W=W, H=H, SS=2)
    R = render_eye(scene, eye_sep / 2, W=W, H=H, SS=2)
    anaglyph(L, R).save(os.path.join(outdir, "banner_anaglyph.png"))
    for name, a, b, label in (("banner_sbs_parallel.png", L, R, "PARALLEL (left | right)"),
                              ("banner_sbs_crosseyed.png", R, L, "CROSS-EYED (right | left)")):
        sbs = Image.new("RGB", (2 * W, H + 40), (0, 0, 0))
        sbs.paste(a, (0, 40))
        sbs.paste(b, (W, 40))
        ImageDraw.Draw(sbs).text((12, 12), label, fill=(200, 200, 200))
        sbs.save(os.path.join(outdir, name))
    write_usdz(scene, os.path.join(outdir, "banner.usdz"))


HOME_MOCK_Z = -3.0


def home_mockup(seg=64):
    """USDZ-only stand-in for the HOME Menu around the banner: a pale theme-coloured
    rectangle covering the nominal banner view at z=HOME_MOCK_Z, with an elliptical hole
    (the oval HOME masks the banner to, estimated as inscribed in the view) filled by a
    dark disc slightly behind it. If the card sits inside the dark oval, it fits."""
    import mapbox_earcut as earcut
    import numpy as np

    m = Mesh()
    hh = (CAM_POS[2] - HOME_MOCK_Z) * math.tan(CAM_YFOV / 2)
    hw = hh * CAM_ASPECT
    rect = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    oval = [(hw * math.cos(2 * math.pi * i / seg), hh * math.sin(2 * math.pi * i / seg))
            for i in range(seg)][::-1]  # hole: clockwise
    flat = np.array(rect + oval, dtype=np.float64)
    tris = earcut.triangulate_float64(flat, np.array([4, 4 + seg], dtype=np.uint32)).reshape(-1, 3)
    up = (0, 0, 1)
    for t in tris:
        m.tri("home_theme", [(flat[i][0], flat[i][1], 0.0) for i in t], [up] * 3, up)
    for i in range(seg):
        a, b = oval[i], oval[(i + 1) % seg]
        m.tri("home_window", [(0, 0, -0.05), (a[0], a[1], -0.05), (b[0], b[1], -0.05)], [up] * 3, up)
    return m


def write_usdz(scene, path):
    """Same scene as USD (Y-up, 1 unit = 1 cm) for Quick Look, plus /Banner/HomeMockup
    (see home_mockup) and a slow 360 deg turntable on /Banner/Badge imitating HOME Menu's
    spin (preview only: the CGFX itself is static)."""
    from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, UsdUtils

    usda = os.path.splitext(path)[0] + ".usdc"
    stage = Usd.Stage.CreateNew(usda)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    root = UsdGeom.Xform.Define(stage, "/Banner")
    stage.SetDefaultPrim(root.GetPrim())
    fps = 30
    stage.SetTimeCodesPerSecond(fps)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(TURNTABLE_PERIOD * fps)
    mats = {}
    for name, c in COL.items():
        m = UsdShade.Material.Define(stage, f"/Banner/Materials/{name}")
        sh = UsdShade.Shader.Define(stage, f"/Banner/Materials/{name}/Surface")
        sh.CreateIdAttr("UsdPreviewSurface")
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*c))
        sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.7)
        sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        m.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
        mats[name] = m
    scene = list(scene) + [("HomeMockup", home_mockup(), (0.0, CAM_POS[1], HOME_MOCK_Z))]
    for node_name, mesh, (tx, ty, tz) in scene:
        x = UsdGeom.Xform.Define(stage, f"/Banner/{node_name}")
        x.AddTranslateOp().Set(Gf.Vec3d(tx, ty, tz))
        if node_name == "Badge":
            rot = x.AddRotateYOp()
            steps = 24
            for k in range(steps + 1):
                rot.Set(360.0 * k / steps, TURNTABLE_PERIOD * fps * k / steps)
        for mat, (pos, nrm, idx) in mesh.parts.items():
            g = UsdGeom.Mesh.Define(stage, f"/Banner/{node_name}/{mat}")
            g.CreatePointsAttr([Gf.Vec3f(*p) for p in pos])
            g.CreateFaceVertexCountsAttr([3] * (len(idx) // 3))
            g.CreateFaceVertexIndicesAttr(list(idx))
            g.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
            g.CreateDoubleSidedAttr(True)
            g.CreateDisplayColorAttr([Gf.Vec3f(*COL[mat])])
            # Quick Look lights from its own environment, so use true face normals
            # (the CGFX's +z side normals are a HOME Menu lighting workaround).
            g.CreateExtentAttr(UsdGeom.PointBased.ComputeExtent([Gf.Vec3f(*p) for p in pos]))
            UsdShade.MaterialBindingAPI.Apply(g.GetPrim()).Bind(mats[mat])
    stage.GetRootLayer().Save()
    if os.path.exists(path):
        os.remove(path)
    UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(usda), path)
    os.remove(usda)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--preview", metavar="PNG", help="also render a stereo preview")
    ap.add_argument("--previews", metavar="DIR", nargs="?", const=os.path.join(ROOT, "preview"),
                    help="write anaglyph / side-by-side PNGs and banner.usdz (default 3ds/preview/)")
    ap.add_argument("--out", default=OUT_CGFX)
    args = ap.parse_args()
    ensure_tooling()

    scene = build_scene()
    tris = write_glb(scene, OUT_GLB)
    print(f"glb: {OUT_GLB} ({os.path.getsize(OUT_GLB)} bytes, {tris} triangles)")

    sys.path.insert(0, PYCGFX_DIR)
    import gltflib
    import main as pycgfx  # pycgfx/main.py
    gltf = gltflib.GLTF.load(OUT_GLB, load_file_resources=True)
    data = pycgfx.write(pycgfx.convert_gltf(gltf))
    if len(data) >= CGFX_MAX:
        sys.exit(f"CGFX too big: {len(data)} bytes (max {CGFX_MAX})")
    with open(args.out, "wb") as fh:
        fh.write(data)
    print(f"cgfx: {args.out} ({len(data)} bytes)")

    if args.preview:
        render_preview(scene, args.preview)
        print(f"preview: {args.preview}")
    if args.previews:
        write_previews(scene, args.previews)
        print(f"previews: {args.previews}")


if __name__ == "__main__":
    main()
