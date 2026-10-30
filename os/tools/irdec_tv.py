"""Teco.Pi IR dekoder na ekranu (sloj preko plejera mpv): poslednji taster krupno (protokol, kod, adresa, komanda)
i spisak prethodnih. Kodove piše server u /dev/shm/teco-irdec.json čim stignu.

python3 irdec_tv.py [ignorisano]
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SRC = Path('/dev/shm/teco-irdec.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-irdec.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
PC = {'NEC': (45, 212, 191), 'RC-5': (250, 204, 21), 'RC-6': (248, 113, 113), 'Sony': (167, 139, 250), 'JVC': (96, 165, 250), 'Samsung': (251, 146, 60)}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def col(proto):
    return next((c for k, c in PC.items() if proto.startswith(k)), (148, 163, 184))


def render(v, w, h, flash):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    F = lambda p, s: ImageFont.truetype(p, max(8, int(s)))
    d.text((w * 0.04, h * 0.04), 'IR DEKODER', font=F(FONTB, h / 22), fill=(45, 212, 191))
    hist = v.get('hist') or []
    if not hist:
        msg = 'Usmeri daljinski ka Teco.Pi-ju i pritisni taster'
        f = F(FONTB, h / 18)
        d.text(((w - d.textlength(msg, font=f)) / 2, h * 0.45), msg, font=f, fill=(148, 163, 184))
        return img
    e = hist[0]
    c = col(e['proto'])
    # protokol (bedž), kod krupno, adresa / komanda
    fb = F(FONTB, h / 16)
    bw = d.textlength(e['proto'], font=fb) + h * 0.06
    x0 = w * 0.06
    d.rounded_rectangle([x0, h * 0.14, x0 + bw, h * 0.14 + fb.size * 1.5], radius=int(fb.size * 0.75), fill=c)
    d.text((x0 + h * 0.03, h * 0.14 + fb.size * 0.22), e['proto'], font=fb, fill=(6, 9, 13))
    if e.get('n', 1) > 1:
        d.text((x0 + bw + h * 0.03, h * 0.14 + fb.size * 0.25), '× %d (držan)' % e['n'], font=F(FONT, h / 20), fill=(148, 163, 184))
    fh = F(FONTM, h / 5.2)
    d.text((x0, h * 0.27), e['hex'], font=fh, fill=(255, 255, 255) if flash else (226, 232, 240))
    y = h * 0.27 + fh.size * 1.2
    fs = F(FONT, h / 17)
    line = 'adresa %d (0x%X)   ·   komanda %d (0x%X)' % (e['addr'], e['addr'], e['cmd'], e['cmd'])
    d.text((x0, y), line, font=fs, fill=(203, 213, 225))
    if e.get('raw'):
        d.text((x0, y + fs.size * 1.5), 'Arduino IRremote: ' + e['raw'], font=F(FONT, h / 22), fill=(100, 116, 139))
    # prethodni tasteri
    fl = F(FONTM, h / 26)
    yy = h * 0.68
    d.line([(x0, yy - h * 0.02), (w * 0.94, yy - h * 0.02)], fill=(30, 41, 59), width=2)
    for p in hist[1:7]:
        d.ellipse([x0, yy + fl.size * 0.25, x0 + fl.size * 0.5, yy + fl.size * 0.75], fill=col(p['proto']))
        d.text((x0 + fl.size, yy), '%-11s %-12s adr %-4d kom %d' % (p['proto'], p['hex'], p['addr'], p['cmd']), font=fl, fill=(148, 163, 184))
        yy += fl.size * 1.45
        if yy > h * 0.96:
            break
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen
    scr = Screen('irdec')
    w, h = scr.size()
    mt, data, last, key, flash_t = object(), None, 0, None, 0
    while running:
        try:
            m = SRC.stat().st_mtime_ns
        except OSError:
            m = None
        if m != mt or data is None or (flash_t and time.time() - flash_t > 0.3):
            mt = m
            try:
                v = json.loads(SRC.read_text())
            except (OSError, ValueError):
                v = {}
            k = json.dumps((v.get('hist') or [])[:1])
            if k != key:   # nov taster: kratko istakni kod
                key, flash_t = k, time.time()
            fl = bool(flash_t and time.time() - flash_t < 0.3)
            if not fl:
                flash_t = 0
            img = render(v, w, h, fl)
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
