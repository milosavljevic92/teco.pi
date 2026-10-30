"""Teco.Pi Tranzistori na ekranu (sloj preko plejera mpv): oznaka ili SMD kod -> vrsta, kućište sa označenim nožicama,
granice i komplementarni par. Dekoder je zajednički sa stranicom (components.py).

python3 tran_tv.py <data/tool-tran.json>
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

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-tran.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
PIN_C = {'E': (56, 189, 248), 'B': (250, 204, 21), 'C': (248, 113, 113), 'G': (250, 204, 21), 'D': (248, 113, 113), 'S': (56, 189, 248)}
KIND_C = {'NPN': (45, 212, 191), 'PNP': (167, 139, 250), 'NPN-D': (45, 212, 191), 'PNP-D': (167, 139, 250), 'N-MOS': (251, 146, 60), 'P-MOS': (244, 114, 182)}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def package(d, r, cx, cy, s, fonts):
    """Kućište i nožice (s = osnovna mera u px). Vraća [(x, y nožice dole, slovo)]."""
    body, lead, lw = (40, 44, 52), (175, 180, 188), max(3, int(s * 0.05))
    pkg, pins = r['pkg'], [p for p in r['pins'] if p[0]]
    f_code = fonts['code']
    if pkg == 'TO-92':   # polukrug, ravna strana ka gledaocu
        w, h = s * 1.6, s * 1.25
        d.chord([cx - w / 2, cy - h, cx + w / 2, cy + h * 0.6], 180, 360, fill=body)
        d.rectangle([cx - w / 2, cy - h * 0.2, cx + w / 2, cy + h * 0.6], fill=body)
        xs = [cx - w * 0.3, cx, cx + w * 0.3]
        top, bot = cy + h * 0.6, cy + h * 1.9
        label_y = cy - h * 0.15
    elif pkg in ('TO-220', 'TO-126'):
        w, h = (s * 1.7, s * 1.4) if pkg == 'TO-220' else (s * 1.3, s * 1.2)
        if pkg == 'TO-220':   # metalni jezičak sa rupom
            d.rounded_rectangle([cx - w / 2, cy - h * 1.15, cx + w / 2, cy - h * 0.35], radius=int(s * 0.06), fill=(190, 196, 206))
            d.ellipse([cx - s * 0.2, cy - h * 0.95, cx + s * 0.2, cy - h * 0.95 + s * 0.4], fill=(6, 9, 13))
        else:
            d.ellipse([cx - s * 0.18, cy - h * 0.55, cx + s * 0.18, cy - h * 0.55 + s * 0.36], fill=(6, 9, 13))
        d.rectangle([cx - w / 2, cy - h * 0.4, cx + w / 2, cy + h * 0.5], fill=body)
        xs = [cx - w * 0.32, cx, cx + w * 0.32]
        top, bot = cy + h * 0.5, cy + h * 1.6
        label_y = cy + h * 0.05
    elif pkg == 'SOT-23':   # odozgo: 1 levo dole, 2 desno dole, 3 gore u sredini
        w, h = s * 1.6, s * 0.9
        d.rectangle([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], fill=body)
        pts = [(cx - w * 0.3, cy + h / 2, cy + h * 1.05), (cx + w * 0.3, cy + h / 2, cy + h * 1.05), (cx, cy - h / 2, cy - h * 1.05)]
        out = []
        for (x, y0, y1), p in zip(pts, pins):
            d.rectangle([x - s * 0.1, min(y0, y1), x + s * 0.1, max(y0, y1)], fill=lead)
            out.append((x, y1, p))
        if r.get('smd'):
            tw = d.textlength(r['smd'], font=f_code)
            d.text((cx - tw / 2, cy - f_code.size * 0.6), r['smd'], font=f_code, fill=(235, 235, 235))
        return out, 'odozgo'
    else:   # TO-3: metalni romb sa dve nožice (B, E); kućište = kolektor
        w, h = s * 2.4, s * 1.4
        d.polygon([(cx - w / 2, cy), (cx - w * 0.2, cy - h / 2), (cx + w * 0.2, cy - h / 2), (cx + w / 2, cy), (cx + w * 0.2, cy + h / 2), (cx - w * 0.2, cy + h / 2)], fill=(190, 196, 206))
        d.ellipse([cx - s * 0.55, cy - s * 0.55, cx + s * 0.55, cy + s * 0.55], fill=(160, 166, 176))
        out = []
        for x, p in zip((cx - s * 0.25, cx + s * 0.25), pins):
            d.ellipse([x - s * 0.08, cy + s * 0.15 - s * 0.08, x + s * 0.08, cy + s * 0.15 + s * 0.08], fill=PIN_C[p[1]])
            out.append((x, cy + s * 0.15, p))
        return out, 'odozdo (nožice); kolektor = kućište'
    out = []
    for x, p in zip(xs, pins):
        d.rectangle([x - s * 0.06, top, x + s * 0.06, bot], fill=lead)
        out.append((x, bot, p))
    name = r['name'].replace('-SMD', '')
    f = f_code
    while d.textlength(name, font=f) > w * 0.9 and f.size > 12:
        f = ImageFont.truetype(FONTM, f.size - 2)
    tw = d.textlength(name, font=f)
    d.text((cx - tw / 2, label_y - f.size / 2), name, font=f, fill=(225, 228, 232))
    return out, 'ravna strana / natpis ka tebi, nožice nadole'


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t, f_n, f_s, f_p = (ImageFont.truetype(FONTB, h // 22), ImageFont.truetype(FONTB, h // 9), ImageFont.truetype(FONT, h // 26),
                          ImageFont.truetype(FONTB, h // 16))
    fonts = {'code': ImageFont.truetype(FONTM, h // 20)}
    d.text((w * 0.04, h * 0.04), 'TRANZISTORI', font=f_t, fill=(45, 212, 191))
    r = components.tran(cfg.get('q', ''))
    if not r.get('name'):
        msg = 'Upiši oznaku ili SMD kod' if not r['q'] else 'Nepoznato: ' + r['q']
        tw = d.textlength(msg, font=f_p)
        d.text(((w - tw) / 2, h * 0.42), msg, font=f_p, fill=(148, 163, 184))
        if r.get('cands'):
            line = 'Možda: ' + ', '.join(r['cands'])
            sw = d.textlength(line, font=f_s)
            d.text(((w - sw) / 2, h * 0.55), line, font=f_s, fill=(100, 116, 139))
        return img
    col = KIND_C.get(r['kind'], (148, 163, 184))
    # levo: kućište i nožice
    s = h * 0.145
    cx, cy = w * 0.27, h * 0.38
    pins, how = package(d, r, cx, cy, s, fonts)
    for x, y, (n, letter, nm) in pins:
        c = PIN_C[letter]
        d.ellipse([x - s * 0.22, y + s * 0.08, x + s * 0.22, y + s * 0.52], fill=c)
        lt = d.textlength(letter, font=f_p)
        d.text((x - lt / 2, y + s * 0.3 - f_p.size * 0.62), letter, font=f_p, fill=(10, 10, 10))
        nn = '%d' % n
        d.text((x - d.textlength(nn, font=f_s) / 2, y + s * 0.6), nn, font=f_s, fill=(148, 163, 184))
    hw = d.textlength(r['pkg'] + ' · ' + how, font=f_s)
    d.text((cx - min(hw, w * 0.46) / 2, h * 0.92), (r['pkg'] + ' · ' + how)[:60], font=f_s, fill=(100, 116, 139))
    # desno: naziv, vrsta, granice, nožice rečima
    x0 = w * 0.52
    name = r['name'].replace('-SMD', ' (SMD)')
    f = f_n
    while d.textlength(name, font=f) > w * 0.45 and f.size > 24:
        f = ImageFont.truetype(FONTB, f.size - 4)
    d.text((x0, h * 0.14), name, font=f, fill=(250, 250, 250))
    y = h * 0.14 + f.size * 1.25
    d.rounded_rectangle([x0, y, x0 + d.textlength(r['kind_n'], font=f_s) + 28, y + f_s.size * 1.6], radius=int(f_s.size * 0.8), fill=col)
    d.text((x0 + 14, y + f_s.size * 0.25), r['kind_n'], font=f_s, fill=(6, 9, 13))
    y += f_s.size * 2.4
    for txt, c in ((r['spec'], (226, 232, 240)), ('Nožice: ' + ' · '.join('%d %s (%s)' % (n, l, nm) for n, l, nm in r['pins'] if n), (148, 163, 184)),
                   ('Par: ' + r['comp'].replace('-SMD', '') if r.get('comp') else '', (148, 163, 184)), (r.get('note') or '', (100, 116, 139)),
                   ('SMD kod ' + r['smd'] + (' · može biti i: ' + ', '.join(r['cands']) if r.get('cands') else '') if r.get('smd') else '', (250, 204, 21))):
        if not txt:
            continue
        ff = f_s
        while d.textlength(txt, font=ff) > w * 0.45 and ff.size > 12:
            ff = ImageFont.truetype(FONT, ff.size - 1)
        d.text((x0, y), txt, font=ff, fill=c)
        y += ff.size * 1.6
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen
    scr = Screen('tran')
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
