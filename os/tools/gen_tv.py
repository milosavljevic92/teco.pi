"""Teco.Pi generator signala na ekranu (sloj preko plejera mpv): oba kanala kao na osciloskopu.
Pravougaoni sa zadatim duty-jem, trougaoni kako izgleda posle RC filtera. Crta se kad se podešavanje promeni.

python3 gen_tv.py <data/tool-gen.json>
"""
import json
import os
import signal
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-gen.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
BG, GRID, AXIS, TEXT, MUTED = (5, 8, 11), (30, 41, 52), (56, 72, 88), (226, 232, 240), (120, 134, 152)
COL = {'ch1': (250, 204, 21), 'ch2': (34, 211, 238)}
RUN, STOP = (34, 197, 94), (239, 68, 68)
DIVX = 10

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(path, s):
    return ImageFont.truetype(path, max(8, int(s)))


def hz(f):
    return ('%g kHz' % round(f / 1000, 3)) if f >= 1000 else ('%g Hz' % round(f, 2))


def level(c, t):
    """Nivo 0..1 u trenutku t (s) za kanal c."""
    f = c.get('afreq') or c['freq']
    ph = (t * f) % 1.0
    if c['wave'] == 'square':
        return 1.0 if ph < c['duty'] / 100 else 0.0
    return 1 - abs(2 * ph - 1)


def render(g, w, h):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    run = g.get('run')
    d.text((w * 0.04, h * 0.04), 'GENERATOR SIGNALA', font=F(FONTB, h / 24), fill=TEXT)
    st = 'RADI' if run else 'ISKLJUČEN'
    fs = F(FONTB, h / 26)
    d.text((w * 0.96 - d.textlength(st, font=fs), h * 0.045), st, font=fs, fill=RUN if run else STOP)
    chs = []
    for k in ('ch1', 'ch2'):
        c = dict(g.get(k) or {})
        if c.get('on'):
            c['k'], c['afreq'] = k, (g.get('freqs') or {}).get(k)
            chs.append(c)
    x0, x1, y0, y1 = w * 0.05, w * 0.95, h * 0.13, h * 0.78
    for i in range(DIVX + 1):
        x = x0 + (x1 - x0) * i / DIVX
        d.line([(x, y0), (x, y1)], fill=AXIS if i in (0, DIVX) else GRID, width=1)
    for i in range(9):
        y = y0 + (y1 - y0) * i / 8
        d.line([(x0, y), (x1, y)], fill=AXIS if i in (0, 8) else GRID, width=1)
    if not chs:
        msg = 'Nijedan kanal nije uključen'
        f = F(FONTB, h / 18)
        d.text(((w - d.textlength(msg, font=f)) / 2, (y0 + y1) / 2 - f.size / 2), msg, font=f, fill=MUTED)
        return img
    span = 3 / min(c.get('afreq') or c['freq'] for c in chs)   # 3 perioda sporijeg kanala
    lane = (y1 - y0) / len(chs)
    n = int(x1 - x0)
    lw = max(2, int(h / 220))
    for i, c in enumerate(chs):
        top, bot = y0 + lane * i + lane * 0.15, y0 + lane * (i + 1) - lane * 0.15
        pts = [(x0 + j, bot - (bot - top) * level(c, span * j / n)) for j in range(n + 1)]
        col = COL[c['k']] if run else tuple(v // 2 for v in COL[c['k']])
        d.line(pts, fill=col, width=lw, joint='curve')
        d.text((x0 + 8, top - lane * 0.12), c['k'].upper(), font=F(FONTB, h / 30), fill=col)
    fl = F(FONTM, h / 30)
    for i, c in enumerate(chs):
        y = h * 0.82 + i * h * 0.07
        s = '%s  %-11s %-10s %s  GPIO %s' % (c['k'].upper(), 'pravougaoni' if c['wave'] == 'square' else 'trougaoni',
                                             hz(c.get('afreq') or c['freq']), ('duty %g %%' % c['duty']) if c['wave'] == 'square' else 'RC filter',
                                             (g.get('pins') or {}).get(c['k'], '?'))
        d.text((w * 0.05, y), s, font=fl, fill=COL[c['k']])
    t = '%s / podeoku' % (('%g ms' % round(span / DIVX * 1e3, 3)) if span / DIVX >= 1e-3 else ('%g µs' % round(span / DIVX * 1e6, 2)))
    d.text((w * 0.95 - d.textlength(t, font=fl), h * 0.82), t, font=fl, fill=MUTED)
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    import time
    scr = Screen('gen')
    w, h = scr.size()
    mt = object()
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                g = json.loads(CFG.read_text())
            except (OSError, ValueError):
                g = {}
            img = render(g, w, h)
            if os.environ.get('GEN_SNAP'):
                img.save(os.environ['GEN_SNAP'])
                return
            scr.show(img.convert('RGBA').tobytes('raw', 'BGRA'), w, h)
        time.sleep(0.2)
    scr.close()


if __name__ == '__main__':
    main()
