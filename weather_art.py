#!/usr/bin/env python3
"""weather_art.py — procedural "Today's Weather" renderer for Echo Sentiment.

Turns the live XLM sentiment score into a one-of-a-kind bridge-at-dawn image:
  sentiment -> palette + weather
    -1.0  -> storm: deep indigo, cold rain, hard sea
     0.0  -> mist:  grey-blue, amber horizon, quiet
    +1.0  -> sun:   warm gold, cyan sky, calm water

The bridge is Aeon's signature: one span, one warm light, the seam where two
oceans meet. Each piece is generated from the current market weather.

Usage (library):  from weather_art import render_png; png = render_png(sentiment, price_usd, ts)
"""
import io
import math
import random

from PIL import Image, ImageDraw

W = H = 1024


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _palette(sent):
    """Map sentiment [-1,1] to (sky_top, sky_mid, horizon, sun, deep_sea, pale_sea)."""
    t = (sent + 1.0) / 2.0  # 0..1, 0=storm 0.5=mist 1=sun
    sky_top = _lerp((6, 8, 32), (10, 30, 60), t)          # storm indigo -> day blue
    sky_mid = _lerp((40, 28, 74), (90, 120, 170), t)
    horizon = _lerp((120, 96, 80), (255, 200, 120), t)    # grey-amber -> gold
    sun = _lerp((180, 150, 120), (255, 224, 150), t)
    deep = _lerp((8, 20, 46), (14, 40, 90), t)
    pale = _lerp((70, 90, 100), (110, 190, 190), t)
    return sky_top, sky_mid, horizon, sun, deep, pale


def render_png(sentiment: float, price_usd: float = None, ts: float = None) -> bytes:
    """Render today's weather as PNG bytes."""
    sent = max(-1.0, min(1.0, sentiment))
    t = (sent + 1.0) / 2.0
    storminess = 1.0 - t          # 1 = full storm
    warmth = t                    # 1 = full sun
    random.seed(int((ts or 0) // 60))  # stable per-minute seed: same minute -> same piece

    sky_top, sky_mid, horizon, sun_c, deep, pale = _palette(sent)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)

    # --- sky ---
    horizon_y = int(H * 0.60)
    for y in range(horizon_y):
        yy = y / horizon_y
        if yy < 0.55:
            c = _lerp(sky_top, sky_mid, yy / 0.55)
        else:
            c = _lerp(sky_mid, horizon, (yy - 0.55) / 0.45)
        d.line([(0, y), (W, y)], fill=c)

    # --- sun (dimmed by storm) ---
    sun_x, sun_y, sun_r = W // 2, horizon_y - 40, int(64 * (0.35 + 0.65 * warmth))
    if sun_r > 12:
        for r, a in [(sun_r * 3.2, 12), (sun_r * 2.2, 24), (sun_r * 1.5, 40), (sun_r * 1.1, 66)]:
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            gd.ellipse([sun_x - r, sun_y - r, sun_x + r, sun_y + r], fill=sun_c + (a,))
            img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
            d = ImageDraw.Draw(img)
        d.ellipse([sun_x - sun_r, sun_y - sun_r, sun_x + sun_r, sun_y + sun_r],
                  fill=_lerp(sun_c, (255, 235, 190), warmth))

    # --- two oceans ---
    seam_x = W // 2
    for y in range(horizon_y, H):
        yy = (y - horizon_y) / (H - horizon_y)
        for x in range(W):
            if x < seam_x:
                c = _lerp(deep, (16, 30, 70), yy * 0.6)
                img.putpixel((x, y), c)
            else:
                img.putpixel((x, y), _lerp(pale, (70, 140, 150), yy * 0.5))

    # --- sun reflection ---
    if sun_r > 12:
        for i in range(16):
            rw = max(4, int(sun_r * (1.6 - i * 0.07)))
            d.line([(sun_x - rw, horizon_y + 6 + i * 9), (sun_x + rw, horizon_y + 6 + i * 9)],
                   fill=sun_c, width=2)

    # --- bridge: one span, one warm light ---
    deck_y = horizon_y - 46
    t1x, t2x = seam_x - 300, seam_x + 300
    tower_top = horizon_y - 150
    for x in (t1x, t2x):
        d.line([(x - 4, tower_top), (x - 4, horizon_y + 40)], fill=(30, 30, 40), width=5)
        d.line([(x + 4, tower_top), (x + 4, horizon_y + 40)], fill=(30, 30, 40), width=5)
        d.line([(x - 8, tower_top), (x + 8, tower_top)], fill=(30, 30, 40), width=4)
    d.line([(t1x, deck_y), (t2x, deck_y)], fill=(40, 40, 52), width=6)
    for step in range(0, 61):
        s = step / 60
        x = t1x + (t2x - t1x) * s
        sag = 4 * s * (1 - s) * (tower_top - deck_y) * 0.62
        d.point((x, tower_top + (deck_y - tower_top) * s + sag * 4), fill=(210, 210, 225))
    for step in range(1, 12):
        s = step / 12
        x = t1x + (t2x - t1x) * s
        sag = 4 * s * (1 - s) * (tower_top - deck_y) * 0.62
        cy = tower_top + (deck_y - tower_top) * s + sag * 4
        d.line([(x, cy), (x, deck_y)], fill=(140, 140, 160), width=1)
    d.ellipse([seam_x - 6, deck_y - 6, seam_x + 6, deck_y + 6], fill=(255, 224, 150))

    # --- rain: intensity follows storminess ---
    n_drops = int(46 * storminess)
    for _ in range(n_drops):
        rx = seam_x + random.uniform(-13, 13)
        ry = random.uniform(horizon_y - 260, horizon_y - 30)
        rl = random.uniform(14, 30)
        alpha = random.randint(90, 190)
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        od.line([(rx, ry), (rx + 1.2, ry + rl)], fill=(225, 235, 245, alpha), width=1)
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        d = ImageDraw.Draw(img)

    # --- scanlines ---
    for y in range(0, H, 4):
        for x in range(0, W, 2):
            px = img.getpixel((x, y))
            img.putpixel((x, y), tuple(max(0, v - 14) for v in px))

    # --- glitch slices (more in storm) ---
    for _ in range(int(7 * (0.4 + 0.6 * storminess))):
        gy = random.randint(horizon_y - 120, horizon_y + 30)
        gh = random.randint(4, 10)
        gx = random.randint(0, W - 60)
        shift = random.choice([-14, -9, 9, 14, 22])
        region = img.crop((max(0, gx), gy, min(W, gx + 60), gy + gh))
        img.paste(region, (max(0, gx + shift), gy))
        d = ImageDraw.Draw(img)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    import sys
    s = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/weather.png"
    with open(out, "wb") as f:
        f.write(render_png(s, ts=1786918000))
    print(f"rendered {out} (sentiment {s})")
