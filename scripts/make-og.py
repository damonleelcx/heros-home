"""Render site/img/og-N.jpg, the link-preview card: the seven agents, full length.

Heros, 阿桥, FORGE and Vera come straight from og-base.jpg, trimmed at the sides to make room.
Aoi's figure is scripts/aoi-full.jpg, 鸭鸭's is scripts/yaya-full.jpg (cut from her character board). Suki's is read from her own repository, checked out beside this one. Windows fonts (Georgia,
Segoe UI, Microsoft YaHei for 阿桥).

    python scripts/make-og.py site/img/og-10.jpg

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
BASE_GAP = 12  # gap between cards on og-base.jpg
# name, figure (relative to the agents directory), card colour behind the figure
AOI = ("Aoi", os.path.join(HERE, "aoi-full.jpg"), (22, 36, 62))
YAYA = ("鸭鸭", os.path.join(HERE, "yaya-full.jpg"), (255, 233, 168))
SUKI = ("Suki", "ai-social-network/docs/assets/suki-full.png", (178, 175, 208))

W, H = 1200, 630
INK, PAPER, LIME, SUN = (11, 11, 10), (243, 241, 234), (221, 232, 106), (239, 227, 95)
M, GAP, TOP = 36, 8, 118  # outer margin, gap between cards, card top
YAYA_W = 186  # her arm is raised, so her card is wider: the same scale as the others, hand in frame


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
    d.text((W - M, 44), "Seven agents. One honest bench.", font=font("georgiai.ttf", 26), fill=LIME, anchor="rm")
    d.text((W - M, 76), "heros-agent.space", font=font("segoeui.ttf", 19), fill=(169, 167, 157), anchor="rm")

    names = BASE + [SUKI[0], YAYA[0]]
    cw = (W - 2 * M - GAP * (len(names) - 1) - YAYA_W) // (len(names) - 1)
    ch = H - TOP - M
    base = Image.open(BASE_IMAGE).convert("RGB")
    label = font("seguisb.ttf", 20)
    label_zh = ImageFont.truetype(os.path.join(FONTS, "msyhbd.ttc"), 19)
    trim = (BASE_CARD - cw) // 2
    for i, name in enumerate(names):
        x = M + i * (cw + GAP)  # 鸭鸭 is last, so the wider card moves no one
        if name == AOI[0]:
            c = figure(AOI[1], AOI[2], cw, ch)
        elif name == YAYA[0]:
            c = figure(YAYA[1], YAYA[2], YAYA_W, ch)
        elif name in BASE:
            bx = M + i * (BASE_CARD + BASE_GAP) + trim
            c = base.crop((bx, TOP, bx + cw, TOP + ch))
            # The trim cuts into the old label: cover what is left of it with the card's
            # own edge from just above. The new label below covers the rest.
            c.paste(c.crop((0, ch - 84, 30, ch - 46)), (0, ch - 46))
        else:
            c = figure(os.path.join(AGENTS, SUKI[1]), SUKI[2], cw, ch)
        img.paste(c, (x, TOP), rounded((c.width, ch), 16))
        f = label_zh if any(ord(ch_) > 0x2E80 for ch_ in name) else label
        tw = d.textlength(name, font=f)
        d.rounded_rectangle((x + 10, TOP + ch - 44, x + 10 + tw + 26, TOP + ch - 12), 16, fill=SUN)
        d.text((x + 23, TOP + ch - 28), name, font=f, fill=INK, anchor="lm")

    img.save(out, quality=86, optimize=True, progressive=True)
    print(out, img.size, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "site", "img", "og-10.jpg"))
