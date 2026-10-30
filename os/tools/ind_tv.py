"""Teco.Pi Induktivnosti na ekranu (sloj preko plejera mpv): prstenovi u boji (kalem kao otpornik) ili SMD/RKM kod
(4R7, 101, 2N2) -> vrednost u nH / µH / mH. Dekoder je zajednički sa stranicom (components.py).

python3 ind_tv.py <data/tool-ind.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import components   # noqa: E402

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-ind.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t, f_v, f_s, f_m = (ImageFont.truetype(FONTB, h // 22), ImageFont.truetype(FONTB, h // 7), ImageFont.truetype(FONT, h // 24),
                          ImageFont.truetype(FONTM, h // 10))
    d.text((w * 0.04, h * 0.04), 'INDUKTIVNOSTI', font=f_t, fill=(45, 212, 191))
    r = components.ind(cfg)
    cx, cy = w / 2, h * 0.32
    lead = (175, 180, 188)
    if cfg.get('mode') == 'bands':   # kalem kao otpornik: svetlo zeleno telo, šira sredina, 4 prstena
        bw, bh = w * 0.42, h * 0.2
        d.line([(cx - bw * 0.85, cy), (cx + bw * 0.85, cy)], fill=lead, width=max(4, h // 90))
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], radius=int(bh * 0.45), fill=(134, 197, 160))
        bands = list(cfg.get('bands') or [])[:4]
        for i, b in enumerate(bands):
            c = components.COL_RGB.get(b)
            if not c:
                continue
            x = cx - bw * 0.3 + i * bw * 0.17 + (bw * 0.08 if i == 3 else 0)
            d.rectangle([x - bw * 0.035, cy - bh / 2 + 2, x + bw * 0.035, cy + bh / 2 - 2], fill=c)
    else:   # SMD: sivi čip sa kodom (veliki „power“ kalemovi su crni kvadrati sa natpisom)
        bw, bh = h * 0.5, h * 0.32
        d.rectangle([cx - bw / 2 - bw * 0.08, cy - bh * 0.35, cx + bw / 2 + bw * 0.08, cy + bh * 0.35], fill=lead)
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], radius=int(bh * 0.12), fill=(38, 42, 50))
        code = str(cfg.get('smd') or '')[:8]
        if code:
            tw = d.textlength(code, font=f_m)
            d.text((cx - tw / 2, cy - f_m.size * 0.6), code, font=f_m, fill=(235, 235, 235))
    txt = r['main'] + ('   ' + r['tol'] if r.get('tol') else '')
    f = f_v
    while d.textlength(txt, font=f) > w * 0.92 and f.size > 20:
        f = ImageFont.truetype(FONTB, f.size - 4)
    d.text(((w - d.textlength(txt, font=f)) / 2, h * 0.56), txt, font=f, fill=(250, 250, 250))
    for i, line in enumerate((r['all3'], r['desc'])):
        if line:
            sw = d.textlength(line, font=f_s)
            d.text(((w - sw) / 2, h * (0.75 + i * 0.075)), line, font=f_s, fill=(148, 163, 184) if i == 0 else (100, 116, 139))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen
    scr = Screen('ind')
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
            if os.environ.get('TOOL_SNAP'):
                img.save(os.environ['TOOL_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.2)
    scr.close()


if __name__ == '__main__':
    main()
