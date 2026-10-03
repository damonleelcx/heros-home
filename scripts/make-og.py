"""Render site/img/og-N.jpg, the link-preview card: the five agents, full length.

Reads each agent's full figure from its own repository, checked out beside this one
(../heros-agent, ../Opportunity-Bridge-Agent, ...). Windows fonts (Georgia, Segoe UI,
Microsoft YaHei for 阿桥).

    python scripts/make-og.py site/img/og-3.jpg

JPEG, not webp: several link-preview crawlers still drop webp. A new filename on every
change, because crawlers cache the image by URL far longer than the site's own headers say.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS = os.path.normpath(os.path.join(HERE, "..", ".."))
FONTS = r"C:\Windows\Fonts"

# name, figure (relative to the agents directory), card colour behind the figure
CAST = [
    ("Heros", "heros-agent/web/static/heros-figure.jpg", (189, 185, 184)),
    ("阿桥", "Opportunity-Bridge-Agent/web/static/mascot-full.png", (254, 254, 253)),
    ("FORGE", "J.A.R.V.I.S.-agent/internal/httpapi/assets/portrait/figure.png", (243, 238, 225)),
    ("Vera", "Law-Med-agent/web/public/vera/vera-full.webp", (226, 236, 246)),
    ("Aoi", "play-with-agents/web/public/play/aoi/aoi-full.webp", (4, 11, 34)),
]

W, H = 1200, 630
INK, PAPER, LIME, SUN = (11, 11, 10), (243, 241, 234), (221, 232, 106), (239, 227, 95)
M, GAP, TOP = 36, 12, 118  # outer margin, gap between cards, card top


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def rounded(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def card(path, bg, w, h):
    fig = Image.open(path).convert("RGBA")
    opaque = fig.getpixel((2, 2))[3] == 255
    # Opaque figures fill the card's height so their own background reaches the edges;
    # cut-outs get a little headroom.
    fh = h if opaque else int(h * 0.94)
    fig = fig.resize((round(fig.width * fh / fig.height), fh), Image.LANCZOS)
    c = Image.new("RGBA", (w, h), bg + (255,))
    c.alpha_composite(fig, ((w - fig.width) // 2, h - fh))
    return c


def main(out):
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)

    # the asterisk mark, as on the page
    cx, cy, r = M + 16, 56, 15
    for dx, dy in [(0, 1), (1, 0), (0.7071, 0.7071), (0.7071, -0.7071)]:
        d.line((cx - r * dx, cy - r * dy, cx + r * dx, cy + r * dy), fill=PAPER, width=4)
    d.text((M + 46, 56), "Heros Agents", font=font("georgia.ttf", 44), fill=PAPER, anchor="lm")
    d.text((W - M, 44), "Five agents. One honest bench.", font=font("georgiai.ttf", 26), fill=LIME, anchor="rm")
    d.text((W - M, 76), "heros-agent.space", font=font("segoeui.ttf", 19), fill=(169, 167, 157), anchor="rm")

    cw = (W - 2 * M - GAP * (len(CAST) - 1)) // len(CAST)
    ch = H - TOP - M
    label = font("seguisb.ttf", 20)
    label_zh = ImageFont.truetype(os.path.join(FONTS, "msyhbd.ttc"), 19)
    for i, (name, rel, bg) in enumerate(CAST):
        x = M + i * (cw + GAP)
        c = card(os.path.join(AGENTS, rel), bg, cw, ch)
        img.paste(c.convert("RGB"), (x, TOP), rounded((cw, ch), 16))
        f = label_zh if any(ord(ch_) > 0x2E80 for ch_ in name) else label
        tw = d.textlength(name, font=f)
        d.rounded_rectangle((x + 10, TOP + ch - 44, x + 10 + tw + 26, TOP + ch - 12), 16, fill=SUN)
        d.text((x + 23, TOP + ch - 28), name, font=f, fill=INK, anchor="lm")

    img.save(out, quality=86, optimize=True, progressive=True)
    print(out, img.size, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "site", "img", "og-2.jpg"))
