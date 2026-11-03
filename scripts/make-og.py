"""Render site/img/og-N.jpg, the link-preview card: the six agents, full length.

The first five come straight from og-base.jpg, trimmed at the sides to make room. Suki's figure
is read from her own repository, checked out beside this one. Windows fonts (Georgia,
Segoe UI, Microsoft YaHei for 阿桥).

    python scripts/make-og.py site/img/og-7.jpg

JPEG, not webp: several link-preview crawlers still drop webp. A new filename on every
change, because crawlers cache the image by URL far longer than the site's own headers say.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS = os.path.normpath(os.path.join(HERE, "..", ".."))
FONTS = r"C:\Windows\Fonts"

# The cards already on og-base.jpg, left to right.
BASE = ["Heros", "阿桥", "FORGE", "Vera", "Aoi"]
BASE_IMAGE = os.path.join(HERE, "og-base.jpg")
BASE_CARD = 216  # card width on og-base.jpg
# name, figure (relative to the agents directory), card colour behind the figure
SUKI = ("Suki", "ai-social-network/docs/assets/suki-full.png", (178, 175, 208))

W, H = 1200, 630
INK, PAPER, LIME, SUN = (11, 11, 10), (243, 241, 234), (221, 232, 106), (239, 227, 95)
M, GAP, TOP = 36, 12, 118  # outer margin, gap between cards, card top


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def rounded(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def figure(path, bg, w, h):
    fig = Image.open(path).convert("RGB")
    fig = fig.resize((round(fig.width * h / fig.height), h), Image.LANCZOS)
    c = Image.new("RGB", (w, h), bg)
    c.paste(fig, ((w - fig.width) // 2, 0))
    return c


def main(out):
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)

    # the asterisk mark, as on the page
    cx, cy, r = M + 16, 56, 15
    for dx, dy in [(0, 1), (1, 0), (0.7071, 0.7071), (0.7071, -0.7071)]:
        d.line((cx - r * dx, cy - r * dy, cx + r * dx, cy + r * dy), fill=PAPER, width=4)
    d.text((M + 46, 56), "Heros Agents", font=font("georgia.ttf", 44), fill=PAPER, anchor="lm")
    d.text((W - M, 44), "Six agents. One honest bench.", font=font("georgiai.ttf", 26), fill=LIME, anchor="rm")
    d.text((W - M, 76), "heros-agent.space", font=font("segoeui.ttf", 19), fill=(169, 167, 157), anchor="rm")

    names = BASE + [SUKI[0]]
    cw = (W - 2 * M - GAP * (len(names) - 1)) // len(names)
    ch = H - TOP - M
    base = Image.open(BASE_IMAGE).convert("RGB")
    label = font("seguisb.ttf", 20)
    label_zh = ImageFont.truetype(os.path.join(FONTS, "msyhbd.ttc"), 19)
    trim = (BASE_CARD - cw) // 2
    for i, name in enumerate(names):
        x = M + i * (cw + GAP)
        if name in BASE:
            bx = M + i * (BASE_CARD + GAP) + trim
            c = base.crop((bx, TOP, bx + cw, TOP + ch))
            # The trim cuts into the old label: cover what is left of it with the card's
            # own edge from just above. The new label below covers the rest.
            c.paste(c.crop((0, ch - 84, 30, ch - 46)), (0, ch - 46))
        else:
            c = figure(os.path.join(AGENTS, SUKI[1]), SUKI[2], cw, ch)
        img.paste(c, (x, TOP), rounded((cw, ch), 16))
        f = label_zh if any(ord(ch_) > 0x2E80 for ch_ in name) else label
        tw = d.textlength(name, font=f)
        d.rounded_rectangle((x + 10, TOP + ch - 44, x + 10 + tw + 26, TOP + ch - 12), 16, fill=SUN)
        d.text((x + 23, TOP + ch - 28), name, font=f, fill=INK, anchor="lm")

    img.save(out, quality=86, optimize=True, progressive=True)
    print(out, img.size, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "site", "img", "og-7.jpg"))
