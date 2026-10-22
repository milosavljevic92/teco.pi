"""Teco.Pi Otpornici na ekranu (sloj preko plejera mpv; sve ostalo radi ispod): kod bojama (3-6 traka)
ili SMD šifra (3 cifre, 4 cifre, R kao zarez, EIA-96). Podešavanje iz JSON fajla (web stranica).

python3 resistor.py <data/tool-resistor.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-resistor.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

# boja: (prikaz RGB, cifra, množilac, tolerancija %, tempko ppm/K, naziv)
COLORS = {
    'black': ((20, 20, 20), 0, 1, None, 250, 'crna'), 'brown': ((122, 72, 38), 1, 10, 1, 100, 'braon'),
    'red': ((214, 40, 40), 2, 100, 2, 50, 'crvena'), 'orange': ((240, 128, 32), 3, 1e3, 0.05, 15, 'narandžasta'),
    'yellow': ((240, 210, 40), 4, 1e4, 0.02, 25, 'žuta'), 'green': ((40, 150, 70), 5, 1e5, 0.5, 20, 'zelena'),
    'blue': ((40, 90, 210), 6, 1e6, 0.25, 10, 'plava'), 'violet': ((140, 70, 190), 7, 1e7, 0.1, 5, 'ljubičasta'),
    'grey': ((128, 128, 128), 8, 1e8, 0.05, 1, 'siva'), 'white': ((240, 240, 240), 9, 1e9, None, None, 'bela'),
    'gold': ((212, 175, 55), None, 0.1, 5, None, 'zlatna'), 'silver': ((192, 192, 200), None, 0.01, 10, None, 'srebrna'),
    'none': (None, None, None, 20, None, 'bez trake'),
}
# EIA-96 (SMD 1%): kod 01-96 -> vrednost
EIA96 = [100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133, 137, 140, 143, 147, 150, 154, 158, 162, 165, 169, 174,
         178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232, 237, 243, 249, 255, 261, 267, 274, 280, 287, 294, 301, 309,
         316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412, 422, 432, 442, 453, 464, 475, 487, 499, 511, 523, 536, 549,
         562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732, 750, 768, 787, 806, 825, 845, 866, 887, 909, 931, 953, 976]
EIA_MUL = {'Z': 0.001, 'Y': 0.01, 'R': 0.01, 'X': 0.1, 'S': 0.1, 'A': 1, 'B': 10, 'H': 10, 'C': 100, 'D': 1000, 'E': 1e4, 'F': 1e5}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def fmt_ohm(v):
    if v is None:
        return '?'
    for div, u in ((1e9, 'GΩ'), (1e6, 'MΩ'), (1e3, 'kΩ')):
        if v >= div:
            return ('%g' % round(v / div, 4)) + ' ' + u
    return ('%g' % round(v, 4)) + ' Ω'


def decode_bands(n, cols):
    """[(boja, uloga)], vrednost, tolerancija, tempko."""
    cols = (list(cols) + ['black'] * 6)[:n]
    ndig = 2 if n <= 4 else 3
    roles, val = [], 0
    for i in range(ndig):
        d = COLORS.get(cols[i], COLORS['black'])[1]
        val = val * 10 + (d or 0)
        roles.append((cols[i], str(d if d is not None else '?')))
    m = COLORS.get(cols[ndig], COLORS['black'])[2] or 1
    val *= m
    roles.append((cols[ndig], '×%s' % fmt_ohm(m).replace(' Ω', '')))
    tol = 20
    if n >= 4:
        tol = COLORS.get(cols[ndig + 1], COLORS['gold'])[3]
        roles.append((cols[ndig + 1], '±%g%%' % tol if tol else '?'))
    tc = None
    if n == 6:
        tc = COLORS.get(cols[5], COLORS['brown'])[4]
        roles.append((cols[5], '%s ppm/K' % tc if tc else '?'))
    return roles, val, tol, tc


def decode_smd(code):
    """(vrednost, opis, tolerancija) ili (None, greška, None)."""
    c = code.upper().strip()
    if len(c) == 3 and c[:2].isdigit() and c[2] in EIA_MUL:
        k = int(c[:2])
        if 1 <= k <= 96:
            return EIA96[k - 1] * EIA_MUL[c[2]], 'EIA-96 (1%%): %d -> %d × %g' % (k, EIA96[k - 1], EIA_MUL[c[2]]), 1
    if 'R' in c and c.replace('R', '').isdigit() and c.count('R') == 1:
        return float(c.replace('R', '.')), 'R = decimalni zarez', None
    if c.isdigit() and len(c) in (3, 4):
        base, exp = int(c[:-1]), int(c[-1])
        return base * 10 ** exp, '%s × 10^%d' % (c[:-1], exp), 5 if len(c) == 3 else 1
    return None, 'nepoznata šifra', None


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t = ImageFont.truetype(FONTB, h // 22)
    f_v = ImageFont.truetype(FONTB, h // 7)
    f_s = ImageFont.truetype(FONT, h // 24)
    f_m = ImageFont.truetype(FONTM, h // 9)
    d.text((w * 0.04, h * 0.04), 'OTPORNICI', font=f_t, fill=(45, 212, 191))
    if cfg.get('mode') == 'smd':
        code = cfg.get('smd') or ''
        val, desc, tol = decode_smd(code)
        cw, ch = w * 0.42, h * 0.26   # SMD čip: crno telo, metalni krajevi, bela šifra
        x0, y0 = (w - cw) / 2, h * 0.18
        d.rounded_rectangle([x0 - cw * 0.09, y0, x0 + cw * 1.09, y0 + ch], radius=10, fill=(170, 175, 182))
        d.rectangle([x0, y0, x0 + cw, y0 + ch], fill=(24, 24, 26))
        tw = d.textlength(code or '—', font=f_m)
        d.text((x0 + (cw - tw) / 2, y0 + ch * 0.2), code or '—', font=f_m, fill=(245, 245, 245))
        sub = 'SMD · ' + desc
    else:
        n = int(cfg.get('bands') or 4)
        roles, val, tol, tc = decode_bands(n, cfg.get('colors') or [])
        bx0, bx1, by, bh = w * 0.18, w * 0.82, h * 0.3, h * 0.2   # telo otpornika sa žicama
        d.line([(w * 0.04, by), (bx0, by)], fill=(170, 175, 182), width=max(4, h // 90))
        d.line([(bx1, by), (w * 0.96, by)], fill=(170, 175, 182), width=max(4, h // 90))
        d.rounded_rectangle([bx0, by - bh / 2, bx1, by + bh / 2], radius=int(bh / 2.2), fill=(222, 196, 150))
        gap = (bx1 - bx0) / (n + 2.2)
        xs = [bx0 + gap * (i + 1) for i in range(n - 1)] + [bx1 - gap * 1.1]   # poslednja traka (tolerancija) odvojena
        for (col, role), x in zip(roles, xs):
            rgb = COLORS.get(col, COLORS['black'])[0]
            if rgb:
                d.rectangle([x - gap * 0.28, by - bh / 2 + 1, x + gap * 0.28, by + bh / 2 - 1], fill=rgb)
            lab = role
            lw = d.textlength(lab, font=f_s)
            d.text((x - lw / 2, by + bh / 2 + h * 0.03), lab, font=f_s, fill=(203, 213, 225))
            nm = COLORS.get(col, COLORS['black'])[5]
            nw = d.textlength(nm, font=f_s)
            d.text((x - nw / 2, by - bh / 2 - h * 0.075), nm, font=f_s, fill=(100, 116, 139))
        sub = '%d trake%s' % (n, (' · %s ppm/K' % tc) if tc else '')
    txt = fmt_ohm(val) + (('  ±%g%%' % tol) if tol else '')
    tw = d.textlength(txt, font=f_v)
    d.text(((w - tw) / 2, h * 0.58), txt, font=f_v, fill=(250, 250, 250))
    sw = d.textlength(sub, font=f_s)
    d.text(((w - sw) / 2, h * 0.8), sub, font=f_s, fill=(148, 163, 184))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('resistor')
    w, h = scr.size()
    mt, data, last = object(), None, 0
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt or data is None:   # slika se pravi samo kad se podešavanje promeni
            mt = m
            try:
                cfg = json.loads(CFG.read_text())
            except (OSError, ValueError):
                cfg = {}
            img = render(cfg, w, h)
            if os.environ.get('RES_SNAP'):
                img.save(os.environ['RES_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:   # ponovo na 2 s (ako je plejer restartovan, sloj se vrati)
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.2)
    scr.close()

if __name__ == '__main__':
    main()
