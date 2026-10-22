"""Teco.Pi Logički analizator (2 kanala) na ekranu — sloj preko plejera (mpv), sve ostalo radi ispod.

python3 logic.py <data/tool-logic.json>

Kanali iz simulatora (takt, PWM, UART, šablon bitova); UART se dekodira i ispisuje iznad signala.
Kasnije: pravi ulazi (GPIO / kartica) — zameni funkciju level().
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen import Screen   # noqa: E402

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-logic.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
DIVX = 10
COL = {'bg': (5, 8, 11), 'grid': (30, 41, 52), 'txt': (148, 163, 184), 'ch0': (74, 222, 128), 'ch1': (244, 114, 182),
       'trig': (249, 115, 22), 'run': (34, 197, 94), 'stop': (239, 68, 68), 'dec': (56, 189, 248), 'decbg': (12, 44, 64)}
DEFAULT = {'run': True, 'tdiv': 0.0002, 'trig': {'src': 'ch0', 'edge': 'rise'},
           'ch0': {'on': True, 'src': 'clock', 'freq': 10000.0, 'duty': 50.0, 'text': 'Teco', 'baud': 9600, 'bits': '10110010', 'rate': 10000.0},
           'ch1': {'on': True, 'src': 'uart', 'freq': 1000.0, 'duty': 50.0, 'text': 'Teco.Pi', 'baud': 9600, 'bits': '1100', 'rate': 5000.0}}

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


def uart_frames(text):
    """UART 8N1: start(0) + 8 bita (LSB prvi) + stop(1); posle poruke pauza od 2 znaka (linija na 1)."""
    bits, chars = [], []
    for ch in (text or ' ').encode('latin-1', 'replace')[:64]:
        chars.append((len(bits), ch))
        bits += [0] + [(ch >> i) & 1 for i in range(8)] + [1]
    return np.array(bits + [1] * 20, dtype=np.uint8), chars


def level(c, t, tnow):
    """Nivo 0/1 kanala u trenucima t (s). Simulator; za prave ulaze ovde čitati uzorke."""
    s = c['src']
    if s == 'clock':
        return (((t * float(c['freq'])) % 1.0) < float(c['duty']) / 100).astype(np.uint8)
    if s == 'pwm':   # duty se polako menja (kao regulacija)
        d = 0.5 + 0.4 * np.sin(2 * np.pi * 0.25 * tnow)
        return (((t * float(c['freq'])) % 1.0) < d).astype(np.uint8)
    if s == 'pattern':
        b = np.array([1 if x == '1' else 0 for x in (c.get('bits') or '10')], dtype=np.uint8)
        return b[(t * float(c['rate'])).astype(np.int64) % len(b)]
    bits, _ = uart_frames(c.get('text'))
    return bits[(t * float(c['baud'])).astype(np.int64) % len(bits)]


def fmt_t(s):
    for div, u in ((1, 's'), (1e-3, 'ms'), (1e-6, 'µs')):
        if s >= div:
            return ('%g' % round(s / div, 3)) + ' ' + u
    return '%g ns' % round(s * 1e9, 3)


def fmt_f(f):
    return ('%.4g MHz' % (f / 1e6)) if f >= 1e6 else ('%.4g kHz' % (f / 1e3)) if f >= 1e3 else ('%.4g Hz' % f)


def measure(v, dt):
    """Frekvencija, perioda i duty iz uzoraka (rastuće ivice)."""
    r = np.nonzero((v[:-1] == 0) & (v[1:] == 1))[0]
    if len(r) < 2:
        return None
    per = float(np.mean(np.diff(r))) * dt
    duty = float(np.mean(v[r[0]:r[-1]])) * 100
    return 1 / per, per, duty


def main():
    scr = Screen('logic')
    w, h = scr.size()
    top, bot, side = int(h * 0.09), int(h * 0.19), int(w * 0.12)   # 7" ekran: krupnija slova
    px0, py0, px1, py1 = side, top, w - int(w * 0.02), h - bot
    gw, gh = px1 - px0, py1 - py0
    f_s = ImageFont.truetype(FONT, max(14, h // 29))
    f_b = ImageFont.truetype(FONTB, max(15, h // 23))
    f_d = ImageFont.truetype(FONTB, max(12, h // 36))
    pal = list(COL.values()) + [(4, 20, 10), (255, 255, 255)] + [tuple(int(COL['bg'][i] + (COL[k][i] - COL['bg'][i]) * f) for i in range(3))
                                                                  for k in ('ch0', 'ch1', 'txt', 'trig', 'grid', 'dec') for f in (0.25, 0.5, 0.75)]
    pal += [tuple(int(COL['decbg'][i] + (COL['dec'][i] - COL['decbg'][i]) * f) for i in range(3)) for f in (0.25, 0.5, 0.75)]
    palimg = Image.new('P', (1, 1))
    palimg.putpalette([v for c in pal for v in c] + [0] * (768 - 3 * len(pal)))
    lut = np.array([(255 << 24) | (r << 16) | (g << 8) | b for r, g, b in pal] + [0xFF000000] * (256 - len(pal)), dtype='<u4')
    q = lambda im: im.quantize(palette=palimg, dither=Image.Dither.NONE)
    lane = gh / 2   # dve trake: CH0 gore, CH1 dole
    cfg, mt, bg, tnow, last = None, None, None, 0.0, None
    npts = 1200
    while running:
        t0 = time.monotonic()
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt or cfg is None:
            mt, cfg = m, load()
            bgr = Image.new('RGB', (w, h), COL['bg'])
            d = ImageDraw.Draw(bgr)
            for i in range(DIVX + 1):
                x = px0 + gw * i / DIVX
                d.line([(x, py0), (x, py1)], fill=COL['grid'])
            for j in range(3):
                d.line([(px0, py0 + lane * j), (px1, py0 + lane * j)], fill=COL['grid'])
            for k, ch in enumerate(('ch0', 'ch1')):
                c = cfg[ch]
                d.text((w * 0.012, py0 + lane * k + lane * 0.38), ch.upper(), font=f_b, fill=COL[ch] if c['on'] else COL['grid'])
                lab = {'clock': 'takt', 'pwm': 'PWM', 'uart': 'UART %s' % c['baud'], 'pattern': 'šablon'}[c['src']]
                d.text((w * 0.012, py0 + lane * k + lane * 0.38 + h * 0.055), lab, font=f_s, fill=COL['txt'])
            tr = cfg['trig']
            hdr = 'LOGIČKI ANALIZATOR   %s/div   Trig %s %s' % (fmt_t(cfg['tdiv']), tr['src'].upper(), '↑' if tr['edge'] == 'rise' else '↓')
            d.text((px0, top * 0.25), hdr, font=f_b, fill=COL['txt'])
            st = 'RUN' if cfg['run'] else 'STOP'
            tw_ = d.textlength(st, font=f_b)
            d.rounded_rectangle([w - w * 0.02 - tw_ - 16, top * 0.18, w - w * 0.02, top * 0.82], radius=6, fill=COL['run'] if cfg['run'] else COL['stop'])
            d.text((w - w * 0.02 - tw_ - 8, top * 0.25), st, font=f_b, fill=(4, 20, 10) if cfg['run'] else (255, 255, 255))
            d.text((w - w * 0.02 - d.textlength('SIM', font=f_s), h - bot * 0.3), 'SIM', font=f_s, fill=COL['grid'])
            bg = q(bgr)
            last = None
        img = last if (not cfg['run'] and last is not None) else None
        if img is None:
            span = float(cfg['tdiv']) * DIVX
            tnow += 0.1 + span * 3
            dt = span / npts
            t = tnow + np.arange(3 * npts) * dt
            tr = cfg['trig']
            src = cfg.get(tr['src']) or cfg['ch0']
            vs = level(src, t, tnow)
            e = np.nonzero((vs[:-1] == 0) & (vs[1:] == 1) if tr['edge'] == 'rise' else (vs[:-1] == 1) & (vs[1:] == 0))[0] + 1
            pre = npts // 10
            e = e[(e >= pre) & (e < 2 * npts)]
            start = int(e[0]) - pre if len(e) else 0
            img = bg.copy()
            d = ImageDraw.Draw(img)
            meas = []
            for k, ch in enumerate(('ch0', 'ch1')):
                c = cfg[ch]
                if not c['on']:
                    continue
                v = vs[start:start + npts] if c is src else level(c, t[start:start + npts], tnow)
                yh, yl = py0 + lane * k + lane * 0.3, py0 + lane * k + lane * 0.8
                xs = px0 + np.arange(npts) * gw / (npts - 1)
                ch_ = np.nonzero(np.diff(v.astype(np.int8)))[0] + 1   # crtanje po ivicama (brzo i oštro)
                seg = [0] + ch_.tolist() + [npts - 1]
                pts = []
                for a, b in zip(seg[:-1], seg[1:]):
                    y = yh if v[a] else yl
                    pts += [(xs[a], y), (xs[b], y)]
                d.line(pts, fill=pal.index(COL[ch]), width=3)
                if c['src'] == 'uart':   # dekoder: okvir i znak iznad svakog bajta
                    bits, chars = uart_frames(c.get('text'))
                    nb, baud = len(bits), float(c['baud'])
                    t_s = t[start]
                    b0 = int(t_s * baud)
                    for idx, chv in chars:
                        base = (b0 // nb) * nb + idx
                        for rep in (0, 1, 2):
                            fs = (base + rep * nb) / baud
                            x0 = px0 + (fs - t_s) / dt * gw / (npts - 1)
                            x1 = x0 + 10 / baud / dt * gw / (npts - 1)
                            if x1 < px0 or x0 > px1:
                                continue
                            yb = py0 + lane * k + lane * 0.06
                            d.rounded_rectangle([max(px0, x0 + 2), yb, min(px1, x1 - 2), yb + lane * 0.17], radius=4, fill=pal.index(COL['decbg']), outline=pal.index(COL['dec']))
                            lab = (chr(chv) if 32 <= chv < 127 else '.') + ' 0x%02X' % chv
                            if x1 - x0 > d.textlength(lab, font=f_d) + 6:
                                d.text((x0 + 5, yb + 2), lab, font=f_d, fill=pal.index(COL['dec']))
                r = measure(v, dt)
                if c['src'] == 'uart':   # za UART: dekodirana poruka umesto frekvencije
                    meas.append((ch, '%s  UART %s 8N1  poruka: "%s"' % (ch.upper(), c['baud'], (c.get('text') or '')[:40])))
                    continue
                meas.append((ch, '%s  f=%s  T=%s  duty=%.1f%%' % (ch.upper(), fmt_f(r[0]), fmt_t(r[1]), r[2]) if r else '%s  nema ponavljanja u prozoru' % ch.upper()))
            tx = px0 + pre * gw / (npts - 1)
            d.polygon([(tx, py0), (tx - 6, py0 - 9), (tx + 6, py0 - 9)], fill=pal.index(COL['trig']))
            strip = Image.new('RGB', (w - 4, h - py1 - 2), COL['bg'])
            sd = ImageDraw.Draw(strip)
            for i, (ch, txt) in enumerate(meas):
                sd.text((px0, bot * (0.12 + 0.3 * i)), txt, font=f_s, fill=COL[ch])
            sd.text((w - w * 0.02 - sd.textlength('SIM', font=f_s), bot * 0.68), 'SIM', font=f_s, fill=COL['grid'])
            img.paste(q(strip), (0, py1 + 2))
            last = img
        if os.environ.get('LOGIC_SNAP'):
            img.convert('RGB').save(os.environ['LOGIC_SNAP'])
            return
        scr.show(lut.take(np.asarray(img)).tobytes(), w, h)
        time.sleep(max(0.0, 1 / 8 - (time.monotonic() - t0)))   # ~8 slika u sekundi
    scr.close()


if __name__ == '__main__':
    main()
