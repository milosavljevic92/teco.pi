"""Teco.Pi Wi-Fi za goste na ekranu (sloj preko plejera mpv): veliki QR kod + ime mreže i lozinka.
Podešavanje iz JSON fajla (web stranica).

python3 guest.py <data/tool-guest.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-guest.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def fit_font(d, s, path, size, wmax):
    f = ImageFont.truetype(path, size)
    while d.textlength(s, font=f) > wmax and size > 14:
        size -= 2
        f = ImageFont.truetype(path, size)
    return f


def render(cfg, w, h):
    import wifiqr
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    mx = w * 0.04
    d.text((mx, h * 0.04), 'WI-FI ZA GOSTE', font=ImageFont.truetype(FONTB, h // 22), fill=(45, 212, 191))
    if not cfg.get('ssid'):
        f = ImageFont.truetype(FONTB, h // 16)
        s = 'Upiši mrežu u Alati → Wi-Fi za goste'
        d.text(((w - d.textlength(s, font=f)) / 2, h * 0.45), s, font=f, fill=(148, 163, 184))
        return img
    q = int(min(h * 0.78, w * 0.48))
    qr = wifiqr.image(cfg, q)
    qx, qy = int(mx), int(h * 0.15)
    d.rounded_rectangle([qx - 10, qy - 10, qx + q + 10, qy + q + 10], radius=18, fill=(255, 255, 255))
    img.paste(qr, (qx, qy))
    tx = qx + q + w * 0.05
    tw = w - tx - mx
    f_l = ImageFont.truetype(FONT, h // 26)
    y = h * 0.2
    d.text((tx, y), 'Mreža', font=f_l, fill=(148, 163, 184))
    s = cfg['ssid']
    f = fit_font(d, s, FONTB, h // 11, tw)
    d.text((tx, y + h * 0.05), s, font=f, fill=(250, 250, 250))
    y += h * 0.22
    if cfg.get('sec') != 'nopass':
        d.text((tx, y), 'Lozinka', font=f_l, fill=(148, 163, 184))
        s = cfg.get('pw') or ''
        f = fit_font(d, s, FONTM, h // 12, tw)
        d.text((tx, y + h * 0.05), s, font=f, fill=(250, 204, 21))
    else:
        d.text((tx, y), 'Otvorena mreža (bez lozinke)', font=f_l, fill=(148, 163, 184))
    y += h * 0.24
    f_s = ImageFont.truetype(FONT, h // 30)
    for i, line in enumerate(('Otvori kameru telefona', 'i usmeri je na kod —', 'telefon se sam poveže.')):
        d.text((tx, y + i * h * 0.05), line, font=f_s, fill=(100, 116, 139))
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.dirname(here))   # wifiqr.py je pored server.py
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('guest')
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
            if os.environ.get('GUEST_SNAP'):
                img.save(os.environ['GUEST_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.2)
    scr.close()

if __name__ == '__main__':
    main()
