"""Teco.Pi Kondenzatori na ekranu (sloj preko plejera mpv; sve ostalo radi ispod): dekoder oznaka
za kondenzatore sa nožicama (keramički / folija: 104, 473J, 2A104K, 4n7, 0.1) i SMD (EIA-198: S5, tantal 107C).
Podešavanje iz JSON fajla (web stranica).

python3 capacitor.py <data/tool-capacitor.json>
"""
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-capacitor.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

TOL = {'B': '±0,1 pF', 'C': '±0,25 pF', 'D': '±0,5 pF', 'F': '±1%', 'G': '±2%', 'J': '±5%', 'K': '±10%', 'M': '±20%', 'Z': '+80/−20%'}
VOLT = {'0G': 4, '0L': 5.5, '0J': 6.3, '1A': 10, '1C': 16, '1E': 25, '1V': 35, '1H': 50, '1J': 63, '1K': 80, '2A': 100, '2Q': 110,
        '2B': 125, '2C': 160, '2Z': 180, '2D': 200, '2P': 220, '2E': 250, '2F': 315, '2V': 350, '2G': 400, '2W': 450, '2J': 630, '3A': 1000}
TANT_V = {'e': 2.5, 'G': 4, 'J': 6.3, 'A': 10, 'C': 16, 'D': 20, 'E': 25, 'V': 35, 'H': 50}
# EIA-198 (SMD keramički): slovo = vrednost, cifra = množilac (10^n pF; 9 = ×0,1)
EIA198 = {'A': 1.0, 'B': 1.1, 'C': 1.2, 'D': 1.3, 'E': 1.5, 'F': 1.6, 'G': 1.8, 'H': 2.0, 'J': 2.2, 'K': 2.4, 'L': 2.7, 'M': 3.0,
          'N': 3.3, 'P': 3.6, 'Q': 3.9, 'R': 4.3, 'S': 4.7, 'T': 5.1, 'U': 5.6, 'V': 6.2, 'W': 6.8, 'X': 7.5, 'Y': 8.2, 'Z': 9.1,
          'a': 2.5, 'b': 3.5, 'd': 4.0, 'e': 4.5, 'f': 5.0, 'm': 6.0, 'n': 7.0, 't': 8.0, 'y': 9.0}
UNIT = {'p': 1, 'n': 1e3, 'u': 1e6, 'µ': 1e6}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def g(v):
    return ('%g' % round(v, 4)).replace('.', ',')


def fmt_pf(pf):
    """Glavna vrednost (najprirodnija jedinica) + sve tri jedinice."""
    if pf is None:
        return '?', ''
    main = g(pf / 1e6) + ' µF' if pf >= 1e6 else g(pf / 1e3) + ' nF' if pf >= 1e3 else g(pf) + ' pF'
    return main, '%s pF = %s nF = %s µF' % (g(pf), g(pf / 1e3), g(pf / 1e6))


def mdesc(d):
    return '0,01' if d == 8 else '0,1' if d == 9 else '10^%d' % d


def mult(d):
    return 0.01 if d == 8 else 0.1 if d == 9 else 10 ** d


def decode_code(code):
    """Kondenzator sa nožicama. -> dict(pf, tol, volt, desc, kind) ili pf=None."""
    s = (code or '').strip().replace(',', '.')
    r = {'pf': None, 'tol': None, 'volt': None, 'desc': 'nepoznata oznaka', 'kind': 'disc'}
    written = False
    m = re.search(r'(?:^|\s|(?<=[pnuµF]))(\d+(?:\.\d+)?)\s*V$', s, re.I)   # napon napisan (47u 16V, 220 25V)
    if m:
        r['volt'], written = float(m.group(1)), True
        s = s[:m.start()]
    s = s.replace(' ', '')
    m = re.match(r'^([0-3][A-Z])(?=\d)', s)   # EIA napon ispred koda (2A104J = 100 V)
    if m and m.group(1) in VOLT:
        r['volt'] = VOLT[m.group(1)]
        s = s[2:]
    if len(s) > 1 and s[-1] in TOL and s[-2].isdigit():
        r['tol'] = TOL[s[-1]]
        s = s[:-1]
    s = s.replace('F', '').replace('f', '') if re.search(r'[pnuµ]F$', s, re.I) else s   # 100nF, 47µF
    m = re.match(r'^(\d*)([pnuµ])(\d*)$', s, re.I)
    if m and (m.group(1) or m.group(3)):   # RKM: 4n7, p47, 2u2, 100n
        u = m.group(2).lower()
        r['pf'] = float((m.group(1) or '0') + '.' + (m.group(3) or '0')) * UNIT[u]
        r['desc'] = 'slovo je jedinica i zarez (%s)' % {'p': 'pF', 'n': 'nF', 'u': 'µF', 'µ': 'µF'}[u]
        if u in 'uµ' and r['pf'] >= 1e6:
            r['kind'] = 'elko'
    elif re.match(r'^\d+(\.\d+)?$', s) and written:   # elektrolit: 220 25V = 220 µF
        r['pf'] = float(s) * 1e6
        r['desc'] = 'elektrolit: broj je vrednost u µF'
        r['kind'] = 'elko'
    elif re.match(r'^\d{3}$', s):
        d = int(s[2])
        if d == 7:   # 7 se ne koristi kao množilac
            return r
        r['pf'] = int(s[:2]) * mult(d)
        r['desc'] = '%s × %s pF' % (s[:2], mdesc(d))
        if r['pf'] >= 1e5:   # od 100 nF obično folija
            r['kind'] = 'film'
    elif re.match(r'^\d{1,2}$', s):
        r['pf'] = float(s)
        r['desc'] = 'dve cifre = vrednost u pF'
    elif re.match(r'^\d*\.\d+$', s):
        r['pf'] = float(s) * 1e6
        r['desc'] = 'decimalni broj = µF (folija)'
        r['kind'] = 'film'
    if r['volt'] and r['pf'] and r['pf'] >= 1e6 and r['kind'] != 'film':
        r['kind'] = 'elko'
    return r


def decode_smd(code):
    """SMD: EIA-198 (S5, 1 slovo proizvođača ispred dozvoljeno) ili tantal / kod od 3 cifre (+ slovo napona)."""
    s = (code or '').replace(' ', '')
    r = {'pf': None, 'tol': None, 'volt': None, 'desc': 'nepoznata šifra', 'kind': 'chip'}
    m = re.match(r'^[A-Za-z]?([A-Za-z])(\d)$', s)
    if m and m.group(1) in EIA198:
        r['pf'] = EIA198[m.group(1)] * mult(int(m.group(2)))
        r['desc'] = 'EIA-198: %s = %s, %s = ×%s pF' % (m.group(1), g(EIA198[m.group(1)]), m.group(2), mdesc(int(m.group(2))))
        return r
    m = re.match(r'^(\d{3})([A-Za-z])?$', s)
    if m:
        d = int(s[2])
        r['pf'] = int(s[:2]) * mult(d)
        r['kind'] = 'tant'
        r['desc'] = 'tantal: %s × %s pF' % (s[:2], mdesc(d))
        if m.group(2):
            v = TANT_V.get(m.group(2)) or TANT_V.get(m.group(2).upper())
            r['volt'] = v
            r['desc'] += ', %s = %s V' % (m.group(2), g(v)) if v else ', nepoznato slovo napona'
        return r
    r2 = decode_code(s)   # elektrolit na SMD (47 16V, 100u)
    if r2['pf']:
        r2['kind'] = 'elko' if r2['pf'] >= 1e6 else 'chip'
        return r2
    return r


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t = ImageFont.truetype(FONTB, h // 22)
    f_v = ImageFont.truetype(FONTB, h // 7)
    f_s = ImageFont.truetype(FONT, h // 24)
    f_m = ImageFont.truetype(FONTM, h // 11)
    d.text((w * 0.04, h * 0.04), 'KONDENZATORI', font=f_t, fill=(45, 212, 191))
    smd = cfg.get('mode') == 'smd'
    code = (cfg.get('smd') if smd else cfg.get('code')) or ''
    r = decode_smd(code) if smd else decode_code(code)
    cx, cy, k = w / 2, h * 0.3, r['kind']
    lead, lw = (170, 175, 182), max(4, h // 90)
    if k == 'disc':   # keramički disk
        rr = h * 0.13
        d.line([(cx - rr * 0.4, cy + rr * 0.8), (cx - rr * 0.4, cy + rr * 1.6)], fill=lead, width=lw)
        d.line([(cx + rr * 0.4, cy + rr * 0.8), (cx + rr * 0.4, cy + rr * 1.6)], fill=lead, width=lw)
        d.ellipse([cx - rr * 1.25, cy - rr, cx + rr * 1.25, cy + rr], fill=(214, 140, 52))
        col = (40, 24, 8)
    elif k == 'film':   # folija (kvadar)
        bw, bh = h * 0.42, h * 0.22
        d.line([(cx - bw * 0.25, cy + bh / 2), (cx - bw * 0.25, cy + bh * 1.05)], fill=lead, width=lw)
        d.line([(cx + bw * 0.25, cy + bh / 2), (cx + bw * 0.25, cy + bh * 1.05)], fill=lead, width=lw)
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], radius=10, fill=(200, 60, 50))
        col = (250, 240, 235)
    elif k == 'elko':   # elektrolit: valjak sa trakom za minus
        bw, bh = h * 0.24, h * 0.3
        d.line([(cx - bw * 0.2, cy + bh / 2), (cx - bw * 0.2, cy + bh * 0.8)], fill=lead, width=lw)
        d.line([(cx + bw * 0.2, cy + bh / 2), (cx + bw * 0.2, cy + bh * 0.8)], fill=lead, width=lw)
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], radius=12, fill=(30, 64, 175))
        d.rectangle([cx + bw * 0.2, cy - bh / 2 + 4, cx + bw * 0.42, cy + bh / 2 - 4], fill=(203, 213, 225))
        for i in range(3):
            y = cy - bh * 0.3 + i * bh * 0.25
            d.text((cx + bw * 0.25, y), '−', font=f_s, fill=(30, 64, 175))
        col = (240, 244, 255)
        code = ''
    elif k == 'tant':   # tantal: žuto telo, traka = plus
        bw, bh = h * 0.42, h * 0.24
        d.rectangle([cx - bw / 2 - bw * 0.08, cy - bh * 0.3, cx + bw / 2 + bw * 0.08, cy + bh * 0.3], fill=lead)
        d.rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], fill=(234, 179, 8))
        d.rectangle([cx - bw / 2, cy - bh / 2, cx - bw / 2 + bw * 0.12, cy + bh / 2], fill=(120, 72, 6))
        col = (40, 24, 4)
    else:   # SMD keramički čip (obično bez oznake; šifra je na kolutu / pakovanju)
        bw, bh = h * 0.42, h * 0.22
        d.rectangle([cx - bw / 2 - bw * 0.1, cy - bh / 2, cx + bw / 2 + bw * 0.1, cy + bh / 2], fill=lead)
        d.rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], fill=(150, 112, 70))
        col = (250, 245, 235)
    if code:
        tw = d.textlength(code, font=f_m)
        d.text((cx - tw / 2, cy - h * 0.05), code, font=f_m, fill=col)
    main, all3 = fmt_pf(r['pf'])
    extra = '  '.join(x for x in (r['tol'], (g(r['volt']) + ' V') if r['volt'] else None) if x)
    txt = main + ('   ' + extra if extra else '')
    f = f_v
    while d.textlength(txt, font=f) > w * 0.92 and f.size > 20:
        f = ImageFont.truetype(FONTB, f.size - 4)
    tw = d.textlength(txt, font=f)
    d.text(((w - tw) / 2, h * 0.56), txt, font=f, fill=(250, 250, 250))
    for i, line in enumerate((all3, r['desc'])):
        if line:
            sw = d.textlength(line, font=f_s)
            d.text(((w - sw) / 2, h * (0.75 + i * 0.075)), line, font=f_s, fill=(148, 163, 184) if i == 0 else (100, 116, 139))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('capacitor')
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
            if os.environ.get('CAP_SNAP'):
                img.save(os.environ['CAP_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:   # ponovo na 2 s (ako je plejer restartovan, sloj se vrati)
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.2)
    scr.close()

if __name__ == '__main__':
    main()
