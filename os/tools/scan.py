"""Teco.Pi Skeniranje mreže na ekranu (sloj preko plejera mpv; sve ostalo radi ispod).
Server skenira i upisuje spisak uređaja u JSON (data/tool-scan.json); ovde se samo crta.

python3 scan.py <data/tool-scan.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-scan.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
TYPE = {'router': ((45, 212, 191), 'RUTER'), 'pi': ((244, 63, 94), 'TECO.PI'), 'camera': ((239, 68, 68), 'KAMERA'),
        'tv': ((168, 85, 247), 'TV'), 'printer': ((148, 163, 184), 'ŠTAMPAČ'), 'phone': ((59, 130, 246), 'TELEFON'),
        'pc': ((14, 165, 233), 'RAČUNAR'), 'alarm': ((248, 113, 113), 'ALARM'), 'light': ((250, 204, 21), 'SVETLO'), 'web': ((129, 140, 248), 'WEB'), 'server': ((34, 197, 94), 'SERVER'), 'iot': ((245, 158, 11), 'PAMETNI'),
        'unknown': ((100, 116, 139), '?')}

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def fit(d, s, font, wmax):
    if d.textlength(s, font=font) <= wmax:
        return s
    while s and d.textlength(s + '…', font=font) > wmax:
        s = s[:-1]
    return s + '…'


def render(sc, w, h, tick):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t = ImageFont.truetype(FONTB, h // 22)
    f_s = ImageFont.truetype(FONTB, h // 28)
    mx = w * 0.035
    d.text((mx, h * 0.035), 'SKENIRANJE MREŽE', font=f_t, fill=(45, 212, 191))
    devs = sc.get('devices') or []
    on = [x for x in devs if x.get('online')]
    if sc.get('running'):
        sub = '%s  %s' % ('◐◓◑◒'[tick % 4], sc.get('step') or '')
        pw = w * 0.3   # traka napretka
        d.rounded_rectangle([w - mx - pw, h * 0.095, w - mx, h * 0.105], radius=4, fill=(30, 41, 52))
        d.rounded_rectangle([w - mx - pw, h * 0.095, w - mx - pw + pw * sc.get('prog', 0) / 100, h * 0.105], radius=4, fill=(45, 212, 191))
    elif sc.get('last'):
        sub = '%d na mreži · %d sačuvanih · %s' % (len(on), len(devs), time.strftime('%H:%M', time.localtime(sc['last'])))
    else:
        sub = 'Pokreni skeniranje sa telefona'
    sw = d.textlength(sub, font=f_s)
    d.text((w - mx - sw, h * 0.045), sub, font=f_s, fill=(148, 163, 184))
    # 7" ekran: krupni redovi (2 kolone po ~7); prvo uređaji na mreži, ostatak se samo prebroji
    top = h * 0.13
    rh = h * 0.12
    per = max(1, int((h * 0.985 - top) // rh))
    cols = 2 if len(devs) > per else 1
    rows = devs[:per * cols]
    if len(devs) > len(rows):
        rows = rows[:-1]
    cw = (w - 2 * mx - (cols - 1) * mx * 0.5) / cols
    f_n = ImageFont.truetype(FONTB, int(rh * 0.34))
    f_d = ImageFont.truetype(FONT, int(rh * 0.25))
    f_b = ImageFont.truetype(FONTB, int(rh * 0.2))
    for i, x in enumerate(rows):
        cx, y = mx + (i // per) * (cw + mx * 0.5), top + (i % per) * rh
        col, lab = TYPE.get(x.get('type'), TYPE['unknown'])
        alive = x.get('online')
        d.rounded_rectangle([cx, y + 3, cx + cw, y + rh - 3], radius=int(rh * 0.18), fill=(15, 23, 32) if alive else (9, 13, 18))
        d.rounded_rectangle([cx, y + 3, cx + rh * 0.08, y + rh - 3], radius=int(rh * 0.05), fill=col if alive else (51, 65, 85))
        tx = cx + rh * 0.25
        vn = x.get('vendor') if x.get('vendor') != 'privatna MAC' else None
        name = x.get('label') or x.get('name') or vn or x.get('kind') or ''
        if x.get('new'):
            name = '★ ' + name
        bw = d.textlength(lab, font=f_b) + rh * 0.3   # vrsta uređaja (oznaka desno gore)
        d.rounded_rectangle([cx + cw - bw - rh * 0.15, y + rh * 0.14, cx + cw - rh * 0.15, y + rh * 0.14 + f_b.size * 1.45], radius=7, fill=col if alive else (51, 65, 85))
        d.text((cx + cw - bw - rh * 0.15 + rh * 0.15, y + rh * 0.14 + f_b.size * 0.2), lab, font=f_b, fill=(4, 10, 16))
        d.text((tx, y + rh * 0.1), fit(d, name, f_n, cw - bw - rh * 0.6), font=f_n, fill=(241, 245, 249) if alive else (100, 116, 139))
        # 2. red: IP · veza · proizvođač (sivo), portovi žuto desno
        ports = ' '.join(str(p) for p in x.get('ports') or [])
        vend = x.get('vendor') if x.get('vendor') and x.get('vendor') != name.lstrip('★ ') else ''
        info = ' · '.join(s for s in (x['ip'], x.get('link') or '', vend, '' if alive else 'nije na mreži') if s)
        avail = cw - rh * 0.4
        iw = d.textlength(info, font=f_d)
        pw = d.textlength(ports, font=f_d) + rh * 0.3 if ports else 0
        if iw + pw > avail:   # ne staje: portovi do 45% širine, ostatak za IP / vezu / proizvođača
            pw = min(pw, max(avail - iw, avail * 0.45))
        d.text((tx, y + rh * 0.56), fit(d, info, f_d, avail - pw), font=f_d, fill=(148, 163, 184) if alive else (71, 85, 105))
        if ports:
            ps = fit(d, ports, f_d, pw - rh * 0.3 + 1)
            d.text((cx + cw - rh * 0.15 - d.textlength(ps, font=f_d), y + rh * 0.56), ps, font=f_d, fill=(250, 204, 21) if alive else (71, 85, 105))
    if len(devs) > len(rows):
        more = '+ još %d (vidi telefon)' % (len(devs) - len(rows))
        d.text((mx + (cols - 1) * (cw + mx * 0.5) + rh * 0.25, top + (per - 1) * rh + rh * 0.3), more, font=f_n, fill=(100, 116, 139))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('scan')
    w, h = scr.size()
    mt, data, last, tick, sc = object(), None, 0, 0, {}
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                sc = json.loads(CFG.read_text())
            except (OSError, ValueError):
                pass
            data = None
        if data is None or (sc.get('running') and time.time() - last > 0.5):
            tick += 1
            img = render(sc, w, h, tick)
            if os.environ.get('SCAN_SNAP'):
                img.save(os.environ['SCAN_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > (0.5 if sc.get('running') else 2):
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.15)
    scr.close()

if __name__ == '__main__':
    main()
