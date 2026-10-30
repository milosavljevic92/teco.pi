"""Teco.Pi Multimetar na ekranu (sloj preko plejera mpv): LCD kao na OWON multimetru (vrednost, jedinica, oznake, bar)
i grafik poslednjeg minuta. Prikaz piše server u /dev/shm/teco-mm.json (za sada primer sa stranice, kasnije BLE most).

python3 mm_tv.py [ignorisano]
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SRC = Path('/dev/shm/teco-mm.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-mm.json')
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
LCD, LCD_L, INK = (159, 174, 151), (200, 240, 184), (26, 36, 22)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def render(v, w, h, blink):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    F = lambda p, s: ImageFont.truetype(p, max(8, int(s)))
    d.text((w * 0.04, h * 0.035), 'MULTIMETAR', font=F(FONTB, h / 22), fill=(244, 63, 94))
    con = v.get('conn')
    sub = (v.get('dev') or 'OWON') + (' · Bluetooth · primer' if con else ' · nije povezan')
    d.text((w * 0.96 - d.textlength(sub, font=F(FONT, h / 30)), h * 0.045), sub, font=F(FONT, h / 30), fill=(148, 163, 184))
    # kućište i LCD
    x0, y0, x1, y1 = w * 0.05, h * 0.12, w * 0.95, h * 0.62
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(h * 0.04), fill=(185, 28, 28))
    lx0, ly0, lx1, ly1 = x0 + h * 0.025, y0 + h * 0.025, x1 - h * 0.025, y1 - h * 0.025
    bg = LCD_L if v.get('light') else LCD
    if v.get('beep') and blink:
        bg = (185, 201, 174)
    d.rounded_rectangle([lx0, ly0, lx1, ly1], radius=int(h * 0.02), fill=bg)
    ff = F(FONTB, h / 26)
    fl = v.get('flags') or {}
    x = lx0 + h * 0.03
    for k, n in (('bt', 'BT'), ('auto', 'AUTO'), ('hold', 'HOLD'), ('rel', 'REL'), ('max', 'MAX'), ('min', 'MIN')):
        on = fl.get(k)
        d.text((x, ly0 + h * 0.02), n, font=ff, fill=INK if on else tuple(int(c * 0.92) for c in bg))
        x += d.textlength(n, font=ff) + h * 0.03
    ac = v.get('acdc') or ''
    if ac:
        d.text((lx1 - h * 0.03 - d.textlength(ac, font=ff), ly0 + h * 0.02), ac, font=ff, fill=INK)
    val = v.get('val') or '-.---'
    unit = v.get('unit') or ''
    fv, fu = F(FONTM, h / 4.6), F(FONTB, h / 11)
    uw = d.textlength(unit, font=fu)
    vw = d.textlength(val, font=fv)
    right = lx1 - h * 0.05 - uw - h * 0.02
    d.text((right - vw, ly0 + h * 0.07), val, font=fv, fill=INK if con else tuple(int(c * 0.8) for c in bg))
    d.text((right + h * 0.02, ly0 + h * 0.07 + fv.size - fu.size * 1.05), unit, font=fu, fill=INK)
    # bar graf
    by = ly1 - h * 0.085
    d.rectangle([lx0 + h * 0.03, by, lx1 - h * 0.03, by + h * 0.022], fill=tuple(int(c * 0.88) for c in bg))
    bw = (lx1 - lx0 - h * 0.06) * max(0, min(100, float(v.get('bar') or 0))) / 100
    xx = lx0 + h * 0.03
    while xx < lx0 + h * 0.03 + bw:
        d.rectangle([xx, by, xx + 4, by + h * 0.022], fill=INK)
        xx += 7
    fs = F(FONTM, h / 34)
    d.text((lx0 + h * 0.03, ly1 - h * 0.05), v.get('range') or '', font=fs, fill=INK)
    mm = v.get('minmax') or ''
    d.text((lx1 - h * 0.03 - d.textlength(mm, font=fs), ly1 - h * 0.05), mm, font=fs, fill=INK)
    # grafik poslednjeg minuta
    hist = v.get('hist') or []
    gx0, gy0, gx1, gy1 = w * 0.05, h * 0.68, w * 0.95, h * 0.95
    d.rounded_rectangle([gx0, gy0, gx1, gy1], radius=int(h * 0.02), fill=(15, 23, 42))
    d.text((gx0 + h * 0.02, gy0 + h * 0.015), 'POSLEDNJI MINUT', font=F(FONTB, h / 34), fill=(148, 163, 184))
    if len(hist) > 1:
        lo, hi = min(hist), max(hist)
        if hi - lo < 1e-12:
            hi, lo = hi + 1e-6, lo - 1e-6
        pad = (hi - lo) * 0.15
        lo, hi = lo - pad, hi + pad
        px0, py0, px1, py1 = gx0 + h * 0.02, gy0 + h * 0.06, gx1 - h * 0.02, gy1 - h * 0.02
        step = (px1 - px0) / 239
        pts = [(px1 - (len(hist) - 1 - i) * step, py1 - (val_ - lo) / (hi - lo) * (py1 - py0)) for i, val_ in enumerate(hist)]
        d.line(pts, fill=(45, 212, 191), width=max(2, h // 220))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen
    scr = Screen('mm')
    w, h = scr.size()
    mt, data, last, prev, bl = object(), None, 0, None, 0
    while running:
        try:
            m = SRC.stat().st_mtime_ns
        except OSError:
            m = None
        blink = int(time.time() * 4) % 2
        if m != mt or data is None or bl != blink:
            mt = m
            try:
                v = json.loads(SRC.read_text())
            except (OSError, ValueError):
                v = {}
            key = json.dumps(v, sort_keys=True) + (str(blink) if v.get('beep') else '')
            bl = blink
            if key != prev or data is None:
                prev = key
                img = render(v, w, h, blink)
                if os.environ.get('TOOL_SNAP'):
                    img.save(os.environ['TOOL_SNAP'])
                    return
                data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2 or last == 0:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.05)
    scr.close()


if __name__ == '__main__':
    main()
