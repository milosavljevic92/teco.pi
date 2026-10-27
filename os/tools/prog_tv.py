"""Teco.Pi programatori na ekranu (sloj preko plejera mpv): proizvođač, radnja, velika traka napretka, USB i poslednji redovi ispisa.
Stanje (prog_view) piše server u JSON fajl dok posao traje.

python3 prog_tv.py <data/tool-prog.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-prog.json')
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
BG, PANEL, TEXT, MUTED, OK, ERR = (6, 9, 13), (13, 17, 23), (235, 238, 242), (100, 116, 139), (34, 197, 94), (239, 68, 68)
BRAND = {'esp': ('ESPRESSIF', 'ESP32 · ESP8266', (231, 53, 44)), 'avr': ('AVR', 'AVR · ATmega / ATtiny', (10, 110, 189)),
         'pic': ('MICROCHIP', 'PIC · PICkit 2', (238, 34, 35))}
LOGOS = Path(__file__).resolve().parent.parent / 'static' / 'logos'   # isti logotipi kao na stranici (providna pozadina)
LOGO_FILE = {'esp': 'espressif.png', 'avr': 'atmel.png', 'pic': 'microchip.png'}
_logo = {}


def logo(mode):
    if mode not in _logo:
        try:
            _logo[mode] = Image.open(LOGOS / LOGO_FILE[mode]).convert('RGBA')
        except (OSError, KeyError):
            _logo[mode] = None
    return _logo[mode]

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(p, s):
    return ImageFont.truetype(p, max(8, int(s)))


def render(p, w, h):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    mx = w * 0.04
    mode = p.get('mode') or p.get('tool') or 'esp'
    name, sub, col = BRAND.get(mode, BRAND['esp'])
    # logo proizvođača na beloj pločici (bez fajla: natpis u boji proizvođača)
    bw, bh = w * 0.24, h * 0.15
    x0, y0b = w - mx - bw, h * 0.035
    lg = logo(mode)
    f2 = F(FONTB, bh * 0.15)
    if lg:
        d.rounded_rectangle([x0, y0b, w - mx, y0b + bh], radius=int(h * 0.02), fill=(255, 255, 255))
        mh, mw = bh * 0.56, bw * 0.84
        sc = min(mh / lg.height, mw / lg.width)
        im = lg.resize((max(1, int(lg.width * sc)), max(1, int(lg.height * sc))), Image.LANCZOS)
        img.paste(im, (int(x0 + bw / 2 - im.width / 2), int(y0b + bh * 0.1 + (mh - im.height) / 2)), im)
        d.text((x0 + bw / 2 - d.textlength(sub, font=f2) / 2, y0b + bh * 0.72), sub, font=f2, fill=(71, 85, 105))
    else:
        d.rounded_rectangle([x0, y0b, w - mx, y0b + bh], radius=int(h * 0.02), fill=col)
        f1 = F(FONTB, bh * 0.32)
        d.text((x0 + bw / 2 - d.textlength(name, font=f1) / 2, y0b + bh * 0.14), name, font=f1, fill=(255, 255, 255))
        d.text((x0 + bw / 2 - d.textlength(sub, font=f2) / 2, y0b + bh * 0.62), sub, font=f2, fill=(255, 235, 235))
    d.text((mx, h * 0.04), 'PROGRAMATOR', font=F(FONTB, h / 26), fill=col)
    mine = p.get('tool') == mode
    busy, ok = p.get('busy') and mine, p.get('ok') if mine else None
    what = p.get('what') if mine else ''
    title = (what + '…') if busy else (what + (' — uspešno' if ok else ' — greška')) if ok is not None and what else 'Spreman'
    d.text((mx, h * 0.1), title, font=F(FONTB, h / 15), fill=OK if ok else ERR if ok is False else TEXT)
    # USB
    usb = p.get('usb') or {}
    need = {'pic': 'pickit2'}.get(mode)
    if need:
        con = need in (usb.get('progs') or [])
        txt = 'PICkit 2 priključen' if con else 'PICkit 2 nije priključen'
    else:
        con = bool(usb.get('serial')) or bool(usb.get('progs'))
        txt = ('USB: ' + ', '.join([x.replace('/dev/', '') for x in usb.get('serial') or []] + (usb.get('progs') or []))) if con else 'Nema USB programatora / adaptera'
    r = h * 0.014
    d.ellipse([mx, h * 0.205, mx + 2 * r, h * 0.205 + 2 * r], fill=OK if con else ERR)
    d.text((mx + 2 * r + 12, h * 0.198), txt, font=F(FONTB, h / 30), fill=TEXT if con else MUTED)
    # traka napretka
    pct = p.get('pct') if mine else None
    y0 = h * 0.27
    d.rounded_rectangle([mx, y0, w - mx, y0 + h * 0.09], radius=int(h * 0.045), fill=(30, 41, 59))
    if pct is not None:
        x1 = mx + (w - 2 * mx) * max(0, min(100, pct)) / 100
        if x1 - mx > h * 0.09:
            d.rounded_rectangle([mx, y0, x1, y0 + h * 0.09], radius=int(h * 0.045), fill=OK if ok is not False else ERR)
        s = '%d %%' % pct
        f = F(FONTB, h / 18)
        d.text((w / 2 - d.textlength(s, font=f) / 2, y0 + h * 0.012), s, font=f, fill=(255, 255, 255))
    # ispis
    top = y0 + h * 0.13
    d.rounded_rectangle([mx, top, w - mx, h * 0.96], radius=int(h * 0.02), fill=PANEL)
    lf = F(FONTM, h / 34)
    lines = (p.get('log') or [])[-int((h * 0.96 - top - 20) / (lf.size * 1.35)):] if mine or not p.get('tool') else []
    maxc = int((w - 2 * mx - 32) / d.textlength('M', font=lf))
    y = top + 14
    for ln in lines:
        c = OK if ln.startswith('✔') else ERR if ln.startswith('✖') or 'error' in ln.lower() else (148, 163, 184)
        d.text((mx + 16, y), ln[:maxc], font=lf, fill=c)
        y += lf.size * 1.35
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('prog')
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
                p = json.loads(CFG.read_text())
            except (OSError, ValueError):
                p = {}
            img = render(p, w, h)
            if os.environ.get('PROG_SNAP'):
                img.save(os.environ['PROG_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.15)
    scr.close()


if __name__ == '__main__':
    main()
