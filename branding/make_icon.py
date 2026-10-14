#!/usr/bin/env python3
"""Generates branding/3DStreaming.icon, an Icon Composer document (Xcode 26+) of the
"3D Streaming" logo, plus a 1024 px preview rendered with Icon Composer's ictool.

Depth (Liquid Glass parallax), back to front:
  document fill  dark navy background
  group "Streaming"  "STREAMING" (Oswald SemiBold), white
  group "3D"         "3D" (Archivo Black): white face over a red copy shifted left and a cyan
                     copy shifted right (the anaglyph look of the HOME banner)
Layers are SVGs (full 1024x1024 canvas each, so they stay aligned) built from the font
outlines with fontTools. Icon Composer lists groups front-first in icon.json.

Usage: python3 branding/make_icon.py [--no-render]
Needs fontTools; if missing, it re-runs itself with 3ds/.tools/venv (see
3ds/scripts/make_banner_model.py, which creates that venv).
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, "..", "3ds", ".tools", "venv", "bin", "python")
ICON = os.path.join(HERE, "3DStreaming.icon")
PREVIEW = os.path.join(HERE, "3DStreaming_1024.png")
ICTOOL = ("/Applications/Xcode.app/Contents/Applications/Icon Composer.app"
          "/Contents/Executables/ictool")

FONTS = os.path.join(HERE, "..", "3ds", "resource", "fonts")  # vendored, SIL OFL 1.1
FONT_BIG = os.path.join(FONTS, "ArchivoBlack-Regular.ttf")
FONT_SMALL = os.path.join(FONTS, "Oswald-SemiBold.ttf")  # Oswald[wght] instanced at 600

S = 1024
BG = "extended-srgb:0.08627,0.09412,0.14902,1.00000"   # #161826, as the banner
WHITE, RED, CYAN, SUB = "#F5F5FA", "#F2243A", "#29DBEB", "#EBEDF7"

# layout (canvas px): content kept inside the central ~70% so the rounded-square and
# circular masks never touch it
LOGO_W, LOGO_CY = 640, 440      # "3D" width and vertical centre
ANA_DX = 16                     # red/cyan offset
SUB_W, SUB_CY = 470, 700        # "STREAMING"
SUB_TRACKING = 0.08             # extra letter spacing, in cap heights

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.boundsPen import BoundsPen
except ImportError:
    venv = os.path.realpath(os.path.join(os.path.dirname(VENV_PY), ".."))
    if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != venv:
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("fontTools missing: pip install fonttools (or run 3ds/scripts/make_banner_model.py once)")


def text_path(font_path, text, width, cy, dx=0.0, tracking=0.0):
    """SVG path data for `text` scaled to `width` px, centred at (S/2 + dx, cy)."""
    font = TTFont(font_path)
    gs, cmap = font.getGlyphSet(), font.getBestCmap()
    bp = BoundsPen(gs)
    gs[cmap[ord("H")]].draw(bp)
    cap = bp.bounds[3] - bp.bounds[1]
    # measure in font units
    x, xmin, xmax, ymin, ymax = 0.0, None, None, None, None
    placements = []
    for ch in text:
        g = gs[cmap[ord(ch)]]
        b = BoundsPen(gs)
        g.draw(b)
        if b.bounds:
            x0, y0, x1, y1 = b.bounds
            xmin = x + x0 if xmin is None else min(xmin, x + x0)
            xmax = x + x1 if xmax is None else max(xmax, x + x1)
            ymin = y0 if ymin is None else min(ymin, y0)
            ymax = y1 if ymax is None else max(ymax, y1)
        placements.append((g, x))
        x += g.width + tracking * cap
    s = width / (xmax - xmin)
    ox = S / 2 + dx - (xmin + xmax) / 2 * s
    oy = cy + (ymin + ymax) / 2 * s          # y flips (SVG is y-down)
    pen = SVGPathPen(gs)
    for g, gx in placements:
        g.draw(TransformPen(pen, (s, 0, 0, -s, ox + gx * s, oy)))
    return pen.getCommands()


def svg(path_d, color):
    return (f'<svg width="{S}" height="{S}" viewBox="0 0 {S} {S}" fill="none" '
            f'xmlns="http://www.w3.org/2000/svg">\n'
            f'<path d="{path_d}" fill="{color}" fill-rule="nonzero"/>\n</svg>\n')


def build():
    if os.path.exists(ICON):
        shutil.rmtree(ICON)
    assets = os.path.join(ICON, "Assets")
    os.makedirs(assets)
    files = {
        "3D-face.svg": svg(text_path(FONT_BIG, "3D", LOGO_W, LOGO_CY), WHITE),
        "3D-red.svg": svg(text_path(FONT_BIG, "3D", LOGO_W, LOGO_CY, -ANA_DX), RED),
        "3D-cyan.svg": svg(text_path(FONT_BIG, "3D", LOGO_W, LOGO_CY, ANA_DX), CYAN),
        "Streaming.svg": svg(text_path(FONT_SMALL, "STREAMING", SUB_W, SUB_CY,
                                       tracking=SUB_TRACKING), SUB),
    }
    for name, data in files.items():
        with open(os.path.join(assets, name), "w") as f:
            f.write(data)

    def layer(img, **extra):
        return {"image-name": img, "name": os.path.splitext(img)[0], **extra}

    shadow = {"kind": "neutral", "opacity": 0.5}
    doc = {
        "fill": {"solid": BG},
        "groups": [  # front first
            {"name": "3D",
             "layers": [layer("3D-face.svg"), layer("3D-red.svg"), layer("3D-cyan.svg")],
             "shadow": shadow,
             "translucency": {"enabled": False, "value": 0.5}},
            {"name": "Streaming",
             "layers": [layer("Streaming.svg")],
             "shadow": shadow,
             "translucency": {"enabled": False, "value": 0.5}},
        ],
        "supported-platforms": {"circles": ["watchOS"], "squares": "shared"},
    }
    with open(os.path.join(ICON, "icon.json"), "w") as f:
        json.dump(doc, f, indent=2)
    print(f"wrote {ICON}")


def render(out, platform="iOS", rendition="Default", size=1024, extra=()):
    subprocess.run([ICTOOL, ICON, "--export-image", "--output-file", out,
                    "--platform", platform, "--rendition", rendition,
                    "--width", str(size), "--height", str(size), "--scale", "1", *extra],
                   check=True, capture_output=True, text=True)


def main():
    build()
    if "--no-render" in sys.argv:
        return
    if not os.path.exists(ICTOOL):
        print("ictool not found (Xcode 26+ with Icon Composer); skipping preview")
        return
    render(PREVIEW)
    print(f"wrote {PREVIEW}")


if __name__ == "__main__":
    main()
