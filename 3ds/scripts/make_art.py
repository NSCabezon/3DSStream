"""Generates resource/banner.png (256x128) and resource/icon.png (48x48):
a big anaglyph-style "3D" with "STREAMING" underneath. Needs Pillow."""
import os
from PIL import Image, ImageDraw, ImageFont

BG = (22, 24, 38)
RED, CYAN, WHITE = (230, 40, 60), (40, 220, 235), (245, 245, 250)
FONT_BIG = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
FONT_SMALL = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
OUT = os.path.join(os.path.dirname(__file__), "..", "resource")


def centered(draw, text, font, cx, y, fill, dx=0):
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2 + dx, y), text, font=font, fill=fill)


def make(size, big_px, small_px, big_y, small_y, shift, scale=4):
    w, h = size[0] * scale, size[1] * scale   # draw large, downsample for clean edges
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT_BIG, big_px * scale)
    small = ImageFont.truetype(FONT_SMALL, small_px * scale)
    s = shift * scale
    centered(d, "3D", big, w / 2, big_y * scale, RED, -s)
    centered(d, "3D", big, w / 2, big_y * scale, CYAN, s)
    centered(d, "3D", big, w / 2, big_y * scale, WHITE)
    centered(d, "STREAMING", small, w / 2, small_y * scale, WHITE)
    return img.resize(size, Image.LANCZOS)


make((256, 128), 64, 26, 2, 90, 2).save(os.path.join(OUT, "banner.png"))
make((48, 48), 22, 11, 2, 33, 1).save(os.path.join(OUT, "icon.png"))
print("wrote banner.png and icon.png")
