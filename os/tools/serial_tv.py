"""Teco.Pi serijski terminal na ekranu (sloj preko plejera mpv): poslednji redovi, port i brzina, TX/RX lampice.
Podatke čita sa istog kanala kao stranica (ws://127.0.0.1/ws/serial), pa vidi sve uživo i kad je port otvoren sa telefona.

python3 serial_tv.py <data/tool-ser.json>
"""
import asyncio
import json
import os
import signal
import struct
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-ser.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTMB = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
PORT = int(os.environ.get('TECO_PORT', '80'))

BG, PANEL, TEXT, MUTED, TS, TXC = (6, 9, 13), (13, 17, 23), (214, 222, 235), (100, 116, 139), (92, 103, 115), (242, 176, 94)
RXL, TXL = (34, 197, 94), (245, 158, 11)
MAXL = 400

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


class View:
    def __init__(self):
        self.st = {}
        self.lines = []          # [[(boja, tekst), …], …]
        self.start = True        # sledeći bajt počinje novi red
        self.dir = ''
        self.rx = self.tx = 0
        self.led = {'R': 0, 'T': 0}
        self.dirty = True
        self.cfg = {'ts': True, 'hex': False}

    def _new(self):
        self.lines.append([])
        del self.lines[:-MAXL]

    def add(self, d, t, data):
        if d == 'R':
            self.rx += len(data)
        else:
            self.tx += len(data)
        self.led[d] = time.time()
        self.dirty = True
        col = TXC if d == 'T' else TEXT
        stamp = time.strftime('%H:%M:%S', time.localtime(t)) + '.%03d ' % int(t % 1 * 1000) + ('→ ' if d == 'T' else '')
        if d != self.dir and not self.start:
            self.start = True
        self.dir = d
        if self.cfg.get('hex'):
            self._new()
            if self.cfg.get('ts', True):
                self.lines[-1].append((TS, stamp))
            self.lines[-1].append((col, ' '.join('%02X' % b for b in data)))
            self.start = True
            return
        txt = data.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')
        txt = ''.join(ch if ch in '\n\t' or ord(ch) >= 32 else '·' for ch in txt)
        for i, part in enumerate(txt.split('\n')):
            if i > 0:
                self.start = True
            if not part:
                continue
            if self.start or not self.lines:
                self._new()
                if self.cfg.get('ts', True):
                    self.lines[-1].append((TS, stamp))
                self.start = False
            self.lines[-1].append((col, part))


def render(v, w, h):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    mx = w * 0.025
    st = v.st or {}
    cfg = st.get('cfg') or {}
    hf = F(FONTB, h / 26)
    d.text((mx, h * 0.03), 'SERIJSKI TERMINAL', font=hf, fill=(45, 212, 191))
    sub = '%s · %s · %s' % (cfg.get('dev', '').replace('/dev/', ''), cfg.get('baud', ''), cfg.get('fmt', '')) if st.get('open') else (st.get('err') or 'port nije otvoren')
    d.text((mx, h * 0.085), sub, font=F(FONTMB, h / 20), fill=TEXT if st.get('open') else MUTED)
    # lampice TX / RX i brojači
    now = time.time()
    lf = F(FONTMB, h / 30)
    x = w - mx
    for key, name, col in (('R', 'RX', RXL), ('T', 'TX', TXL)):
        on = now - v.led[key] < 0.25
        tw = d.textlength(name, font=lf)
        x -= tw
        d.text((x, h * 0.045), name, font=lf, fill=TEXT if on else MUTED)
        r = h * 0.014
        cx, cy = x - r - 10, h * 0.045 + lf.size / 2 + 1
        if on:
            d.ellipse([cx - r * 2, cy - r * 2, cx + r * 2, cy + r * 2], fill=tuple(int(c * 0.35) for c in col))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col if on else tuple(int(c * 0.3) for c in col))
        x = cx - r - 26
    cnt = 'RX %s · TX %s' % (size(v.rx), size(v.tx))
    cf = F(FONTM, h / 34)
    d.text((w - mx - d.textlength(cnt, font=cf), h * 0.1), cnt, font=cf, fill=MUTED)
    # izlaz
    top, bot = h * 0.165, h - h * 0.03
    d.rounded_rectangle([mx, top, w - mx, bot], radius=int(h * 0.02), fill=PANEL, outline=(31, 38, 48), width=2)
    f = F(FONTM, h / 27)
    lh = f.size * 1.32
    pad = h * 0.02
    maxw = w - 2 * mx - 2 * pad
    cw = d.textlength('M', font=f)
    cols = max(10, int(maxw / cw))
    rows = []   # prelom dugih redova na širinu ekrana
    for ln in v.lines[-120:]:
        cur, n = [], 0
        for col, txt in ln:
            while txt:
                room = cols - n
                piece, txt = txt[:room], txt[room:]
                cur.append((col, piece))
                n += len(piece)
                if n >= cols:
                    rows.append(cur)
                    cur, n = [], 0
        if cur or not ln:
            rows.append(cur)
    nrow = int((bot - top - 2 * pad) / lh)
    y = top + pad
    if not rows:
        msg = 'Čekam podatke…' if st.get('open') else 'Otvori port u Alati → Serijski terminal'
        d.text((mx + pad, y), msg, font=f, fill=MUTED)
    for r in rows[-nrow:]:
        x = mx + pad
        for col, txt in r:
            d.text((x, y), txt, font=f, fill=col)
            x += cw * len(txt)
        y += lh
    return img


def F(path, s):
    return ImageFont.truetype(path, max(8, int(s)))


def size(n):
    return '%d B' % n if n < 1024 else '%.1f kB' % (n / 1024) if n < 1048576 else '%.2f MB' % (n / 1048576)


async def feed(v):
    import aiohttp
    while running:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect('http://127.0.0.1:%d/ws/serial' % PORT, heartbeat=20) as ws:
                    async for m in ws:
                        if not running:
                            break
                        if m.type == aiohttp.WSMsgType.TEXT:
                            j = json.loads(m.data)
                            if 'st' in j:
                                v.st = j['st']
                                v.dirty = True
                        elif m.type == aiohttp.WSMsgType.BINARY and len(m.data) > 9:
                            v.add(chr(m.data[0]), struct.unpack('<d', m.data[1:9])[0], m.data[9:])
        except Exception:
            pass
        await asyncio.sleep(1.5)


async def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('ser')
    w, h = scr.size()
    v = View()
    asyncio.ensure_future(feed(v))
    mt, last, led_was = object(), 0, False
    loop = asyncio.get_running_loop()
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                v.cfg.update(json.loads(CFG.read_text()))
            except (OSError, ValueError):
                pass
            v.dirty = True
        leds = time.time() - max(v.led.values()) < 0.3
        if v.dirty or leds != led_was or time.time() - last > 2:
            v.dirty, led_was = False, leds
            img = await loop.run_in_executor(None, render, v, w, h)
            data = img.convert('RGBA').tobytes('raw', 'BGRA')
            await loop.run_in_executor(None, scr.show, data, w, h)
            last = time.time()
        await asyncio.sleep(0.12)
    scr.close()


if __name__ == '__main__':
    asyncio.run(main())
