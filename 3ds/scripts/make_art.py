"""Generates resource/banner.png (256x128) and resource/icon.png (48x48):
a big anaglyph-style "3D" with "STREAMING" underneath. Needs Pillow.
Fonts are vendored in resource/fonts (SIL Open Font License 1.1): Archivo Black for "3D",
Oswald (variable font instanced at SemiBold, 600) for "STREAMING"."""
import os
from PIL import Image, ImageDraw, ImageFont

BG = (22, 24, 38)
RED, CYAN, WHITE = (230, 40, 60), (40, 220, 235), (245, 245, 250)
OUT = os.path.join(os.path.dirname(__file__), "..", "resource")
FONT_BIG = os.path.join(OUT, "fonts", "ArchivoBlack-Regular.ttf")
FONT_SMALL = os.path.join(OUT, "fonts", "Oswald-SemiBold.ttf")


def centered(draw, text, font, cx, cy, fill, dx=0):
    """Draw `text` with its ink box centred on (cx + dx, cy)."""
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    draw.text((cx + dx - (x0 + x1) / 2, cy - (y0 + y1) / 2), text, font=font, fill=fill)


def make(size, big_px, small_px, big_cy, small_cy, shift, scale=4):
    w, h = size[0] * scale, size[1] * scale   # draw large, downsample for clean edges
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT_BIG, round(big_px * scale))
    small = ImageFont.truetype(FONT_SMALL, round(small_px * scale))
    s = shift * scale
    centered(d, "3D", big, w / 2, big_cy * scale, RED, -s)
    centered(d, "3D", big, w / 2, big_cy * scale, CYAN, s)
    centered(d, "3D", big, w / 2, big_cy * scale, WHITE)
    centered(d, "STREAMING", small, w / 2, small_cy * scale, WHITE)
    return img.resize(size, Image.LANCZOS)


# sizes in px; vertical positions are ink-box centres
make((256, 128), 64, 20.5, 49.5, 99.5, 2).save(os.path.join(OUT, "banner.png"))
make((48, 48), 22, 9, 18.5, 37, 1).save(os.path.join(OUT, "icon.png"))
print("wrote banner.png and icon.png")
