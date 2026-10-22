"""Teco.Pi Mrežni tester na ekranu (sloj preko plejera mpv; sve ostalo radi ispod).
Server radi provere i upisuje rezultat u JSON (data/tool-net.json); ovde se samo crta.

python3 net.py <data/tool-net.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-net.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
ST = {'ok': ((34, 197, 94), 'OK'), 'warn': ((245, 158, 11), 'PAŽNJA'), 'fail': ((239, 68, 68), 'GREŠKA'),
      'info': ((56, 189, 248), 'INFO'), 'run': ((168, 85, 247), '…'), 'wait': ((71, 85, 105), '')}

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


def render(nt, w, h, tick):
    img = Image.new('RGB', (w, h), (6, 9, 13))
    d = ImageDraw.Draw(img)
    f_t = ImageFont.truetype(FONTB, h // 22)
    f_n = ImageFont.truetype(FONTB, h // 30)
    f_v = ImageFont.truetype(FONTB, h // 24)
    f_d = ImageFont.truetype(FONT, h // 38)
    f_b = ImageFont.truetype(FONTB, h // 44)
    mx = w * 0.035
    d.text((mx, h * 0.035), 'MREŽNI TESTER', font=f_t, fill=(45, 212, 191))
    items = nt.get('items') or []
    if nt.get('running'):
        step = next((it['n'] for it in items if it['k'] == nt.get('step')), '')
        sub = 'Test u toku %s  %s' % ('◐◓◑◒'[tick % 4], step)
    elif nt.get('done'):
        bad = sum(it['s'] == 'fail' for it in items)
        warn = sum(it['s'] == 'warn' for it in items)
        sub = 'Završeno %s  ·  %s' % (time.strftime('%H:%M', time.localtime(nt['done'])),
                                      'sve u redu' if not bad and not warn else '%d grešaka, %d upozorenja' % (bad, warn))
    else:
        sub = 'Pokreni test sa telefona'
    sw = d.textlength(sub, font=f_n)
    d.text((w - mx - sw, h * 0.045), sub, font=f_n, fill=(148, 163, 184))
    cols, rows = 2, (len(items) + 1) // 2 or 1
    top, gap = h * 0.1, h * 0.008
    cw = (w - 2 * mx - gap) / cols
    ch = (h * 0.96 - top - gap * (rows - 1)) / rows
    for i, it in enumerate(items):
        x, y = mx + (i // rows) * (cw + gap), top + (i % rows) * (ch + gap)
        col, lab = ST.get(it['s'], ST['wait'])
        d.rounded_rectangle([x, y, x + cw, y + ch], radius=int(ch * 0.18), fill=(15, 23, 32), outline=(30, 41, 52))
        d.rounded_rectangle([x, y, x + ch * 0.09, y + ch], radius=int(ch * 0.05), fill=col)
        tx = x + ch * 0.25
        d.text((tx, y + ch * 0.05), it['n'], font=f_n, fill=(203, 213, 225))
        if lab:
            bw = d.textlength(lab, font=f_b) + ch * 0.2
            d.rounded_rectangle([x + cw - bw - ch * 0.15, y + ch * 0.06, x + cw - ch * 0.15, y + ch * 0.06 + f_b.size * 1.5], radius=8, fill=col)
            d.text((x + cw - bw - ch * 0.05, y + ch * 0.06 + f_b.size * 0.2), lab, font=f_b, fill=(4, 10, 16))
        d.text((tx, y + ch * 0.34), fit(d, it.get('v') or ('čeka…' if it['s'] == 'wait' else ''), f_v, cw - ch * 0.4), font=f_v,
               fill=(250, 250, 250) if it['s'] != 'wait' else (71, 85, 105))
        d.text((tx, y + ch * 0.72), fit(d, it.get('d') or '', f_d, cw - ch * 0.4), font=f_d, fill=(100, 116, 139))
    return img


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('net')
    w, h = scr.size()
    mt, data, last, tick, nt = object(), None, 0, 0, {}
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                nt = json.loads(CFG.read_text())
            except (OSError, ValueError):
                pass
            data = None
        if data is None or (nt.get('running') and time.time() - last > 0.5):   # dok test radi: osvežavanje (pokazivač)
            tick += 1
            img = render(nt, w, h, tick)
            if os.environ.get('NET_SNAP'):
                img.save(os.environ['NET_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > (0.5 if nt.get('running') else 2):
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.15)
    scr.close()

if __name__ == '__main__':
    main()
