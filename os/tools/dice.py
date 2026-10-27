"""Teco.Pi kockice na ekranu (sloj preko plejera mpv): poslednje bacanje — kockice sa tačkama (d6) ili brojem, i zbir.
Bacanje se dešava na stranici (crypto.getRandomValues), ovde se samo prikazuje.

python3 dice.py <data/tool-dice.json>
"""
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-dice.json')
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BG, MUTED, TEXT, ACC = (6, 9, 13), (148, 163, 184), (240, 244, 248), (234, 88, 12)
PIPS = {1: [(1, 1)], 2: [(0, 0), (2, 2)], 3: [(0, 0), (1, 1), (2, 2)], 4: [(0, 0), (2, 0), (0, 2), (2, 2)],
        5: [(0, 0), (2, 0), (1, 1), (0, 2), (2, 2)], 6: [(0, 0), (2, 0), (0, 1), (2, 1), (0, 2), (2, 2)]}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(path, s):
    return ImageFont.truetype(path, max(8, int(s)))


def poly(cx, cy, r, n, rot=-90):
    return [(cx + r * math.cos(math.radians(rot + i * 360 / n)), cy + r * math.sin(math.radians(rot + i * 360 / n))) for i in range(n)]


def die(d, x, y, s, v, sides):
    """Jedna kockica u kvadratu (x, y, s): d6 sa tačkama, ostale kao oblik sa brojem."""
    cx, cy = x + s / 2, y + s / 2
    face, edge = (250, 250, 250), (203, 213, 225)
    if sides == 6:
        d.rounded_rectangle([x + 4, y + 8, x + s + 4, y + s + 8], radius=int(s * 0.18), fill=(0, 0, 0))
        d.rounded_rectangle([x, y, x + s, y + s], radius=int(s * 0.18), fill=face, outline=edge, width=3)
        r = s * 0.085
        for gx, gy in PIPS.get(v, []):
            px, py = x + s * (0.25 + gx * 0.25), y + s * (0.25 + gy * 0.25)
            d.ellipse([px - r, py - r, px + r, py + r], fill=(220, 38, 38) if v == 1 else (17, 24, 39))
        return
    n = {4: 3, 8: 4, 10: 5, 12: 5, 20: 6}.get(sides, 0)
    col = {4: (37, 99, 235), 8: (22, 163, 74), 10: (147, 51, 234), 12: (219, 39, 119), 20: (234, 88, 12), 100: (8, 145, 178)}.get(sides, (100, 116, 139))
    if n:
        pts = poly(cx, cy + (s * 0.06 if n == 3 else 0), s * (0.58 if n == 3 else 0.52), n, -90 if n != 4 else -90)
        d.polygon([(px + 4, py + 8) for px, py in pts], fill=(0, 0, 0))
        d.polygon(pts, fill=col, outline=(255, 255, 255))
    else:
        d.ellipse([x + 4, y + 8, x + s + 4, y + s + 8], fill=(0, 0, 0))
        d.ellipse([x, y, x + s, y + s], fill=col, outline=(255, 255, 255), width=3)
    f = F(FONTB, s * (0.34 if v >= 10 else 0.42))
    t = str(v)
    d.text((cx - d.textlength(t, font=f) / 2, cy - f.size * 0.62 + (s * 0.08 if n == 3 else 0)), t, font=f, fill=(255, 255, 255))


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    mx = w * 0.04
    sides, vals = int(cfg.get('sides') or 6), [int(v) for v in cfg.get('vals') or []]
    d.text((mx, h * 0.04), 'KOCKICE', font=F(FONTB, h / 24), fill=ACC)
    n0 = int(cfg.get('n') or 1)
    d.text((mx, h * 0.1), '%d %s' % (n0, 'kockica' if n0 == 1 or n0 >= 5 else 'kockice'), font=F(FONTB, h / 14), fill=TEXT)
    if not vals:
        f = F(FONTB, h / 14)
        s = 'Baci kockice na stranici'
        d.text(((w - d.textlength(s, font=f)) / 2, h * 0.47), s, font=f, fill=MUTED)
        return img
    n = len(vals)
    cols = n if n <= 5 else math.ceil(n / 2)
    rows = 1 if n <= 5 else 2
    s = min((w - 2 * mx) / (cols * 1.3), h * (0.42 if rows == 1 else 0.27))
    gap = s * 0.3
    top = h * 0.24 + (h * 0.5 - rows * s - (rows - 1) * gap) / 2
    for i, v in enumerate(vals):
        r, c = divmod(i, cols)
        cnt = min(cols, n - r * cols)
        x0 = (w - (cnt * s + (cnt - 1) * gap)) / 2
        die(d, x0 + c * (s + gap), top + r * (s + gap), s, v, sides)
    if n > 1:
        f = F(FONTB, h / 9)
        t = 'Zbir: %d' % sum(vals)
        d.text(((w - d.textlength(t, font=f)) / 2, h * 0.8), t, font=f, fill=TEXT)
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('dice')
    w, h = scr.size()
    mt, data, last = object(), None, 0
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt or data is None:
            mt = m
            try:
                cfg = json.loads(CFG.read_text())
            except (OSError, ValueError):
                cfg = {}
            img = render(cfg, w, h)
            if os.environ.get('DICE_SNAP'):
                img.save(os.environ['DICE_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.1)
    scr.close()


if __name__ == '__main__':
    main()
