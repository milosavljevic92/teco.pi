"""Teco.Pi Osciloskop na ekranu (sloj preko plejera mpv: YouTube, radio… rade ispod).

python3 scope.py <data/tool-scope.json>

Podešavanje čita iz JSON fajla (menja ga web stranica preko servera) i prati izmene.
Uzorci: simulator signala; kasnije ADC kartica (zameni samo funkciju samples()).
"""
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-scope.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
DIVX, DIVY = 10, 8
COL = {'bg': (5, 8, 11), 'grid': (30, 41, 52), 'axis': (56, 72, 88), 'txt': (148, 163, 184),
       'ch1': (250, 204, 21), 'ch2': (34, 211, 238), 'trig': (249, 115, 22), 'run': (34, 197, 94), 'stop': (239, 68, 68)}
DEFAULT = {'run': True, 'tdiv': 0.005, 'trig': {'src': 'ch1', 'level': 0.0, 'edge': 'rise'},
           'ch1': {'on': True, 'wave': 'sine', 'freq': 50.0, 'amp': 2.0, 'offset': 0.0, 'vdiv': 1.0, 'noise': 0.03},
           'ch2': {'on': True, 'wave': 'square', 'freq': 100.0, 'amp': 1.0, 'offset': 0.0, 'vdiv': 1.0, 'noise': 0.02}}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def load():
    try:
        c = json.loads(CFG.read_text())
    except (OSError, ValueError):
        c = {}
    out = json.loads(json.dumps(DEFAULT))
    for k, v in c.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def wave(kind, t, f, amp):
    ph = 2 * math.pi * f * t
    if kind == 'square':
        return amp * np.sign(np.sin(ph))
    if kind == 'triangle':
        return amp * (2 / math.pi) * np.arcsin(np.sin(ph))
    if kind == 'saw':
        return amp * (2 * ((f * t) % 1.0) - 1)
    if kind == 'dc':
        return np.full_like(t, amp)
    if kind == 'noise':
        return amp * np.random.normal(0, 0.5, t.shape)
    return amp * np.sin(ph)


def samples(ch, t):
    """Simulator: talas + šum + pomeraj. (ADC kartica: ovde čitati uzorke.)"""
    v = wave(ch['wave'], t, max(0.01, float(ch['freq'])), float(ch['amp'])) + float(ch['offset'])
    n = float(ch.get('noise') or 0) * max(0.05, abs(float(ch['amp'])))
    return v + (np.random.normal(0, n, t.shape) if n else 0)


def fmt_t(s):
    for div, u in ((1, 's'), (1e-3, 'ms'), (1e-6, 'µs')):
        if s >= div:
            return ('%g' % round(s / div, 3)) + ' ' + u
    return '%g ns' % round(s * 1e9, 3)


def fmt_f(f):
    return ('%.3g kHz' % (f / 1000)) if f >= 1000 else ('%.3g Hz' % f)


def main():
    scr = Screen('scope')
    w, h = scr.size()
    top, bot, side = int(h * 0.085), int(h * 0.15), int(w * 0.02)   # 7" ekran: krupnija slova
    px0, py0, px1, py1 = side, top, w - side, h - bot
    gw, gh = px1 - px0, py1 - py0
    f_s = ImageFont.truetype(FONT, max(14, h // 30))
    f_b = ImageFont.truetype(FONTB, max(15, h // 26))
    # paleta: crtanje u 'P' slici + tabela u RGB565 je ~4x brže od pretvaranja pune RGB slike (Pi 3)
    pal = [COL['bg']] + [c for k, c in COL.items() if k != 'bg'] + [tuple(int(x * 0.35) for x in COL[k]) for k in ('ch1', 'ch2')] + [(4, 20, 10), (255, 255, 255)]
    for k in ('ch1', 'ch2', 'txt', 'trig', 'grid'):   # međutonovi za glatka slova
        for f in (0.25, 0.5, 0.75):
            pal.append(tuple(int(COL['bg'][i] + (COL[k][i] - COL['bg'][i]) * f) for i in range(3)))
    for f in (0.25, 0.5, 0.75):
        pal.append(tuple(int(COL['run'][i] * (1 - f) + (4, 20, 10)[i] * f) for i in range(3)))
        pal.append(tuple(int(COL['stop'][i] * (1 - f) + 255 * f) for i in range(3)))
    palimg = Image.new('P', (1, 1))
    palimg.putpalette([v for c in pal for v in c] + [0] * (768 - 3 * len(pal)))
    lut = np.array([(255 << 24) | (r << 16) | (g << 8) | b for r, g, b in pal] + [0xFF000000] * (256 - len(pal)), dtype='<u4')   # -> B,G,R,A
    I = lambda c: pal.index(c)
    q = lambda im: im.quantize(palette=palimg, dither=Image.Dither.NONE)
    cfg, mt, bg, tnow, last = None, None, None, 0.0, None
    npts = min(gw, 500)
    while running:
        t0 = time.monotonic()
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt or cfg is None:
            mt, cfg = m, load()
            bg = Image.new('RGB', (w, h), COL['bg'])
            d = ImageDraw.Draw(bg)
            for i in range(DIVX + 1):   # mreža 10 x 8 + sitne crtice na osama
                x = px0 + gw * i / DIVX
                d.line([(x, py0), (x, py1)], fill=COL['axis'] if i == DIVX // 2 else COL['grid'])
            for j in range(DIVY + 1):
                y = py0 + gh * j / DIVY
                d.line([(px0, y), (px1, y)], fill=COL['axis'] if j == DIVY // 2 else COL['grid'])
            for k in range(DIVX * 5):
                x = px0 + gw * k / (DIVX * 5)
                d.line([(x, py0 + gh / 2 - 3), (x, py0 + gh / 2 + 3)], fill=COL['axis'])
            for k in range(DIVY * 5):
                y = py0 + gh * k / (DIVY * 5)
                d.line([(px0 + gw / 2 - 3, y), (px0 + gw / 2 + 3, y)], fill=COL['axis'])
            x = px0
            for ch in ('ch1', 'ch2'):   # gornja traka: kanali, vreme, trigger
                c = cfg[ch]
                txt = '%s %s V/div' % (ch.upper(), '%g' % c['vdiv']) if c['on'] else '%s isključen' % ch.upper()
                d.text((x, top * 0.25), txt, font=f_b, fill=COL[ch] if c['on'] else COL['grid'])
                x += d.textlength(txt, font=f_b) + w * 0.03
            txt = '%s/div' % fmt_t(cfg['tdiv'])
            d.text((x, top * 0.25), txt, font=f_b, fill=COL['txt'])
            x += d.textlength(txt, font=f_b) + w * 0.03
            tr = cfg['trig']
            d.text((x, top * 0.25), 'Trig %s %s %.2f V' % (tr['src'].upper(), '↑' if tr['edge'] == 'rise' else '↓', tr['level']),
                   font=f_b, fill=COL['trig'])
            st = 'RUN' if cfg['run'] else 'STOP'   # RUN/STOP oznaka je deo pozadine (menja se samo sa podešavanjem)
            tw_ = d.textlength(st, font=f_b)
            d.rounded_rectangle([w - side - tw_ - 16, top * 0.18, w - side, top * 0.82], radius=6, fill=COL['run'] if cfg['run'] else COL['stop'])
            d.text((w - side - tw_ - 8, top * 0.25), st, font=f_b, fill=(4, 20, 10) if cfg['run'] else (255, 255, 255))
            bg = q(bg)
            last = None
        img = last if (not cfg['run'] and last is not None) else None
        if img is None:
            tdiv = float(cfg['tdiv'])
            span = tdiv * DIVX
            tnow += max(0.02, time.monotonic() - t0 + 0.05)
            t = tnow + np.linspace(0, 2 * span, 2 * npts)
            tr = cfg['trig']
            src = cfg.get(tr['src']) or cfg['ch1']
            vs = samples(src, t)
            lvl = float(tr['level'])
            a, b = (vs[:-1] < lvl) & (vs[1:] >= lvl), (vs[:-1] > lvl) & (vs[1:] <= lvl)
            hit = np.nonzero(a if tr['edge'] == 'rise' else b)[0]
            pre = npts // 10   # trigger na 10% od leve ivice
            hit = hit[(hit >= pre) & (hit < npts)]
            start = int(hit[0]) - pre if len(hit) else 0
            tw = t[start:start + npts]
            img = bg.copy()
            d = ImageDraw.Draw(img)
            xs = px0 + np.arange(npts) * gw / (npts - 1)
            meas = []
            for ch in ('ch2', 'ch1'):   # CH1 crta se poslednji (preko CH2)
                c = cfg[ch]
                if not c['on']:
                    continue
                v = vs[start:start + npts] if c is src else samples(c, tw)
                ys = np.clip(py0 + gh / 2 - v / float(c['vdiv']) * gh / DIVY, py0, py1)
                pts = list(zip(xs.tolist(), ys.tolist()))
                d.line(pts, fill=I(tuple(int(x * 0.35) for x in COL[ch])), width=4)   # sjaj kao na pravom ekranu
                d.line(pts, fill=I(COL[ch]), width=2)
                vpp, vrms, vavg = float(v.max() - v.min()), float(np.sqrt(np.mean(v ** 2))), float(v.mean())
                meas.append((ch, 'f=%s  Vpp=%.2f V  Vrms=%.2f V  Vavg=%.2f V' % (fmt_f(float(c['freq'])), vpp, vrms, vavg)))
            # oznaka nivoa triggera i trenutka okidanja
            ty = py0 + gh / 2 - lvl / float(src['vdiv']) * gh / DIVY
            d.polygon([(px1, ty), (px1 + side * 0.8, ty - 6), (px1 + side * 0.8, ty + 6)], fill=I(COL['trig']))
            tx = px0 + pre * gw / (npts - 1)
            d.polygon([(tx, py0), (tx - 6, py0 - 9), (tx + 6, py0 - 9)], fill=I(COL['trig']))
            strip = Image.new('RGB', (w, h - py1 - 2), COL['bg'])   # merenja: glatka slova u punoj boji, pa u paletu
            sd = ImageDraw.Draw(strip)
            for i, (ch, txt) in enumerate(reversed(meas)):
                sd.text((px0, bot * (0.12 + 0.42 * i) - 2), txt, font=f_s, fill=COL[ch])
            sd.text((w - side - sd.textlength('SIM', font=f_s), bot * 0.58 - 2), 'SIM', font=f_s, fill=COL['grid'])
            img.paste(q(strip), (0, py1 + 2))
            last = img
        frame = img
        if os.environ.get('SCOPE_SNAP'):   # provera: jedan kadar u PNG pa kraj
            frame.convert('RGB').save(os.environ['SCOPE_SNAP'])
            return
        scr.show(lut.take(np.asarray(frame)).tobytes(), w, h)   # paleta -> BGRA sloj preko plejera
        time.sleep(max(0.0, 1 / 10 - (time.monotonic() - t0)))   # 10 slika u sekundi (sloj preko plejera: i mpv radi posao)
    scr.close()


if __name__ == '__main__':
    main()
