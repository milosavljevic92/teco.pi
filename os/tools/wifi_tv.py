"""Teco.Pi Wi-Fi analizator na ekranu (sloj preko plejera mpv): grafikon kanala 2,4 GHz, preporuka i najjače mreže.
Podatke (poslednje skeniranje) piše server u JSON fajl; ovde se samo crta kad se promene.

python3 wifi_tv.py <data/tool-wifi.json>
"""
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-wifi.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
BG, PANEL, TEXT, MUTED, ACC, ME, OK = (6, 9, 13), (11, 16, 22), (235, 238, 242), (100, 116, 139), (168, 85, 247), (168, 85, 247), (34, 197, 94)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(p, s):
    return ImageFont.truetype(p, max(8, int(s)))


def hue_col(s):
    h = 17
    for ch in s or '?':
        h = (h * 31 + ord(ch)) % 360
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360, 0.58, 0.75)
    return int(r * 255), int(g * 255), int(b * 255)


def render(d, w, h):
    img = Image.new('RGB', (w, h), BG)
    dr = ImageDraw.Draw(img, 'RGBA')
    mx = w * 0.03
    dr.text((mx, h * 0.03), 'WI-FI ANALIZATOR', font=F(FONTB, h / 26), fill=ACC)
    nets = [n for n in d.get('nets') or [] if n.get('band') == '2.4']
    cur = d.get('cur')
    if not d.get('nets'):
        f = F(FONTB, h / 16)
        s = 'Skeniram okolne mreže…'
        dr.text(((w - dr.textlength(s, font=f)) / 2, h * 0.45), s, font=f, fill=MUTED)
        return img
    sub = ('Povezan: %s · kanal %s · %s dBm' % (cur.get('ssid') or '?', cur.get('ch'), cur.get('dbm'))) if cur else 'Teco.Pi nije na Wi-Fi mreži'
    dr.text((mx, h * 0.085), sub, font=F(FONTB, h / 22), fill=TEXT)
    # grafikon
    L, T, R, B = w * 0.06, h * 0.18, w * 0.70, h * 0.92
    dr.rounded_rectangle([mx, T - h * 0.02, R + w * 0.01, B + h * 0.05], radius=int(h * 0.02), fill=PANEL)
    X = lambda c: L + (R - L) * (c + 1.2) / 16.4
    Y = lambda db: T + (B - T) * (1 - (max(-100, min(-25, db)) + 100) / 75)
    fs = F(FONTM, h / 40)
    for db in range(-90, -20, 10):
        dr.line([L, Y(db), R, Y(db)], fill=(28, 37, 49), width=1)
        dr.text((L - 8 - dr.textlength(str(db), font=fs), Y(db) - fs.size / 2), str(db), font=fs, fill=MUTED)
    rec = (d.get('rec') or {}).get('2.4')
    if rec:
        b = rec['best']
        dr.rounded_rectangle([X(b - 2), T, X(b + 2), B], radius=8, fill=(34, 197, 94, 28), outline=(34, 197, 94, 110), width=2)
    fc = F(FONTM, h / 30)
    for c in range(1, 14):
        col = TEXT if c in (1, 6, 11) else MUTED
        if cur and cur.get('band') == '2.4' and cur.get('ch') == c:
            col = ME
        s = str(c)
        dr.text((X(c) - dr.textlength(s, font=fc) / 2, B + h * 0.008), s, font=fc, fill=col)
    for n in sorted(nets, key=lambda n: n['sig']):
        col = ME if n.get('on') else hue_col(n.get('ssid') or n.get('bssid'))
        x0, x1, xm, y, yb = X(n['ch'] - 2), X(n['ch'] + 2), X(n['ch']), Y(n['dbm']), Y(-100)
        pts = [(x0 + (x1 - x0) * i / 40, yb - (yb - y) * math.sin(math.pi * i / 40) ** 0.7) for i in range(41)]
        dr.polygon(pts, fill=col + ((70,) if n.get('on') else (26,)))
        dr.line(pts, fill=col + (255,), width=4 if n.get('on') else 2)
    lf = F(FONTB, h / 34)
    placed = []
    for n in sorted(nets, key=lambda n: (not n.get('on'), -n['sig']))[:10]:
        if n['dbm'] < -85 and not n.get('on'):
            continue
        s = (n.get('ssid') or '(skrivena)')[:16]
        tw, th = dr.textlength(s, font=lf) + 16, lf.size + 10
        x, y = min(max(L, X(n['ch']) - tw / 2), R - tw), Y(n['dbm']) - th - 6
        for _ in range(10):
            if not any(x < px + pw and x + tw > px and y < py + ph and y + th > py for px, py, pw, ph in placed):
                break
            y -= th + 3
        if y < T - h * 0.015:
            continue
        placed.append((x, y, tw, th))
        col = ME if n.get('on') else hue_col(n.get('ssid') or n.get('bssid'))
        dr.rounded_rectangle([x, y, x + tw, y + th], radius=int(th / 2), fill=BG, outline=col, width=2)
        dr.text((x + 8, y + 4), s, font=lf, fill=col)
    # desno: preporuka i spisak
    sx = w * 0.735
    if rec:
        dr.rounded_rectangle([sx, T - h * 0.02, w - mx, T + h * 0.2], radius=int(h * 0.02), fill=(16, 40, 26), outline=OK, width=2)
        dr.text((sx + 16, T), 'PREPORUČEN KANAL', font=F(FONTB, h / 40), fill=MUTED)
        dr.text((sx + 16, T + h * 0.035), str(rec['best']), font=F(FONTM, h / 9), fill=OK)
        cc = cur.get('ch') if cur and cur.get('band') == '2.4' else None
        note = '' if cc is None else ('✔ već si na njemu' if cc == rec['best'] else 'ti si na %s' % cc)
        dr.text((sx + 16 + h * 0.12, T + h * 0.08), note, font=F(FONTB, h / 36), fill=TEXT)
    y = T + h * 0.24
    rf = F(FONT, h / 36)
    for n in sorted(nets, key=lambda n: (not n.get('on'), -n['sig']))[:9]:
        col = ME if n.get('on') else hue_col(n.get('ssid') or n.get('bssid'))
        dr.ellipse([sx, y + 6, sx + 14, y + 20], fill=col)
        s = (n.get('ssid') or '(skrivena)')[:14]
        dr.text((sx + 22, y), s, font=rf, fill=TEXT)
        t2 = 'k%d %d' % (n['ch'], n['dbm'])
        dr.text((w - mx - dr.textlength(t2, font=rf), y), t2, font=rf, fill=MUTED)
        y += h * 0.05
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('wifi')
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
                d = json.loads(CFG.read_text())
            except (OSError, ValueError):
                d = {}
            img = render(d, w, h)
            if os.environ.get('WIFI_SNAP'):
                img.save(os.environ['WIFI_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.2)
    scr.close()


if __name__ == '__main__':
    main()
