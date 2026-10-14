#!/usr/bin/env python3
"""Builds the stereoscopic HOME Menu banner: resource/banner.cgfx.

Scene (units of the HOME banner camera, which sits at (0, 1, 44.786) looking down -z with
a 30 deg vertical FOV, so the z=0 plane spans 40x24 units = the 400x240 top screen and
1 unit = 10 px at screen depth):
  - "3D": Arial Black glyphs extruded into a solid, z = +1.5..+5 (in front of the screen),
    white front, red left-facing sides, cyan right-facing sides, slate top/bottom.
    Gently sways +-12 deg around its vertical axis (4 s loop; --static to disable).
  - "STREAMING": DIN Condensed Bold, thin extrusion at z = 0..+0.5 (screen depth).
  - a dark background plane at z = -4 (behind the screen).

Pipeline: glyph outlines (fontTools) -> flattened contours -> earcut caps + extruded sides
-> .glb (written here by hand, no glTF writer needed) -> pycgfx (skyfloogle/pycgfx, pinned
commit, cloned into .tools/pycgfx because it ships without a license so we don't vendor it)
-> resource/banner.cgfx. bannertool LZ11-compresses the raw .cgfx itself (make cia passes it
with -ci); never pre-compress it.

Usage (from 3ds/):
  .tools/venv/bin/python scripts/make_banner_model.py [--static] [--preview out.png]
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
DEPS = ["gltflib", "pillow", "fonttools", "mapbox-earcut", "numpy"]

OUT_GLB = os.path.join(TOOLS, "banner.glb")
OUT_CGFX = os.path.join(ROOT, "resource", "banner.cgfx")
CGFX_MAX = 0x80000

FONT_BIG = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
FONT_SMALL = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"

# HOME Menu banner camera (pycgfx banner-camera.gltf)
CAM_POS = (0.0, 1.0, 44.786)
CAM_YFOV = 0.523599
CAM_ASPECT = 400 / 240
CAM_ZNEAR = 26.5

# Layout
LOGO_CAP = 11.0                 # "3D" cap height
LOGO_CENTER = (0.0, 4.0, 3.25)  # node origin (pivot of the sway)
LOGO_DEPTH = 3.5                # z = +1.5 .. +5.0
SUB_CAP = 3.4                   # "STREAMING" cap height
SUB_TRACKING = 0.12             # extra letter spacing, in cap heights
SUB_CENTER = (0.0, -6.6, 0.25)
SUB_DEPTH = 0.5
BG_Z = -4.0
SWAY_DEG = 12.0
SWAY_PERIOD = 4.0

# Colours (sRGB 0..1). Colour lives in a solid 8x8 texture per material; baseColorFactor
# stays white, so the HOME Menu's lighting multiplies a known-good texture path.
COL = {
    "logo_front": (0.96, 0.96, 0.98),
    "logo_back": (0.20, 0.21, 0.28),
    "logo_red": (0.92, 0.16, 0.24),
    "logo_cyan": (0.16, 0.86, 0.92),
    "logo_slate": (0.34, 0.37, 0.50),
    "sub_front": (0.92, 0.93, 0.97),
    "sub_side": (0.42, 0.45, 0.58),
    "bg": (0.086, 0.094, 0.149),
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


def extrude(mesh, contours, depth, mats, side_lift=0.9, smooth_deg=35):
    """Solid extrusion centred on z=0. mats: front, back, side_fn(nx, ny) -> material.
    Side normals are tilted towards +z by `side_lift`: the HOME banner light shines
    straight down -z, so true side normals (z=0) would render pure black."""
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
        cos_s = math.cos(math.radians(smooth_deg))
        for ring in rings:
            n = len(ring)
            en = []
            for i in range(n):  # outward edge normals (outer CCW, holes CW)
                a, b = ring[i], ring[(i + 1) % n]
                en.append(norm((b[1] - a[1], -(b[0] - a[0]))))
            for i in range(n):
                a, b = ring[i], ring[(i + 1) % n]
                e = en[i]

                def vn(other):
                    if e[0] * other[0] + e[1] * other[1] > cos_s:
                        return norm((e[0] + other[0], e[1] + other[1]))
                    return e
                na, nb = vn(en[i - 1]), vn(en[(i + 1) % n])
                la = norm((na[0], na[1], side_lift))
                lb = norm((nb[0], nb[1], side_lift))
                mat = mats["side"](e[0], e[1])
                facing = (e[0], e[1], 0)
                A, B = (a[0], a[1], zf), (b[0], b[1], zf)
                C, D = (b[0], b[1], zb), (a[0], a[1], zb)
                mesh.tri(mat, [A, B, C], [la, lb, lb], facing)
                mesh.tri(mat, [A, C, D], [la, lb, la], facing)


def logo_side(nx, ny):
    if nx < -0.25:
        return "logo_red"
    if nx > 0.25:
        return "logo_cyan"
    return "logo_slate"


def build_scene():
    logo = Mesh()
    extrude(logo, glyph_contours(FONT_BIG, "3D", LOGO_CAP, step=0.35), LOGO_DEPTH,
            {"front": "logo_front", "back": "logo_back", "side": logo_side})
    sub = Mesh()
    extrude(sub, glyph_contours(FONT_SMALL, "STREAMING", SUB_CAP, SUB_TRACKING, step=0.2),
            SUB_DEPTH, {"front": "sub_front", "back": "sub_side",
                        "side": lambda nx, ny: "sub_side"}, side_lift=1.2)
    bg = Mesh()
    d = CAM_POS[2] - BG_Z
    hh = d * math.tan(CAM_YFOV / 2) * 1.15
    hw = hh * CAM_ASPECT * 1.15
    cy = CAM_POS[1]
    q = [(-hw, cy - hh, 0), (hw, cy - hh, 0), (hw, cy + hh, 0), (-hw, cy + hh, 0)]
    bg.tri("bg", [q[0], q[1], q[2]], [(0, 0, 1)] * 3, (0, 0, 1))
    bg.tri("bg", [q[0], q[2], q[3]], [(0, 0, 1)] * 3, (0, 0, 1))
    for m in (logo, sub, bg):
        m.compact()
    return [("Background", bg, (0.0, 0.0, BG_Z)),
            ("Streaming", sub, SUB_CENTER),
            ("Logo3D", logo, LOGO_CENTER)]


# --------------------------------------------------------------------------- glTF
def solid_png(rgb, size=8):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    px = bytes(round(c * 255) for c in rgb)
    raw = b"".join(b"\x00" + px * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_glb(scene, path, animate):
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
    if animate:
        steps = 32
        times = [SWAY_PERIOD * i / steps for i in range(steps + 1)]
        quats = []
        for t in times:
            a = math.radians(SWAY_DEG) * math.sin(2 * math.pi * t / SWAY_PERIOD)
            quats += [0.0, math.sin(a / 2), 0.0, math.cos(a / 2)]
        a_t = add_acc(struct.pack(f"<{len(times)}f", *times), 5126, len(times), "SCALAR",
                      min=[times[0]], max=[times[-1]])
        a_q = add_acc(struct.pack(f"<{len(quats)}f", *quats), 5126, len(times), "VEC4")
        logo_node = [n["name"] for n in nodes].index("Logo3D")
        gltf["animations"] = [{"name": "Sway",
                               "samplers": [{"input": a_t, "output": a_q, "interpolation": "LINEAR"}],
                               "channels": [{"sampler": 0, "target": {"node": logo_node,
                                                                      "path": "rotation"}}]}]
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
def render_preview(scene, path, eye_sep=0.8, yaws=(0.0, SWAY_DEG)):
    """Flat-shaded painter's-algorithm render through the banner camera. Rows: one per yaw.
    Columns: left eye, right eye, red/cyan anaglyph. Off-axis stereo converging at z=0."""
    from PIL import Image, ImageDraw, ImageChops

    W, H, SS = 400, 240, 2
    f = (H * SS / 2) / math.tan(CAM_YFOV / 2)

    def render(eye_x, yaw_deg):
        img = Image.new("RGB", (W * SS, H * SS), (0, 0, 0))
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
                        if name == "Logo3D":
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

    rows = []
    for yaw in yaws:
        L = render(-eye_sep / 2, yaw)
        R = render(eye_sep / 2, yaw)
        Lr, _, _ = L.convert("RGB").split()
        _, Rg, Rb = R.convert("RGB").split()
        ana = Image.merge("RGB", (Lr, Rg, Rb))
        rows.append([L, R, ana])
    gap = 8
    out = Image.new("RGB", (3 * W + 2 * gap, len(rows) * H + (len(rows) - 1) * gap), (255, 255, 255))
    for r, row in enumerate(rows):
        for c, im in enumerate(row):
            out.paste(im, (c * (W + gap), r * (H + gap)))
    out.save(path)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--static", action="store_true", help="no sway animation")
    ap.add_argument("--preview", metavar="PNG", help="also render a stereo preview")
    ap.add_argument("--out", default=OUT_CGFX)
    args = ap.parse_args()
    ensure_tooling()

    scene = build_scene()
    tris = write_glb(scene, OUT_GLB, not args.static)
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


if __name__ == "__main__":
    main()
