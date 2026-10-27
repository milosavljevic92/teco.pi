"""Teco.Pi pinout na ekranu (sloj preko plejera mpv): crtež izabranog čipa, izabrani pin i napomene.
Podaci o čipu dolaze sa web stranice (tamo je biblioteka čipova) u JSON fajlu.

python3 pinout.py <data/tool-pin.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-pin.json')
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

BG = (6, 9, 13)
COL = {'p': (214, 69, 69), 'g': (90, 98, 110), 'io': (100, 116, 139), 'a': (47, 168, 79), 'c': (29, 143, 224), 's': (240, 124, 26), 'n': (163, 169, 179)}
NAME = {'p': 'napajanje', 'g': 'masa', 'io': 'GPIO', 'a': 'analogno', 'c': 'UART/I2C/SPI', 's': 'reset/boot/strap', 'n': 'ne koristiti'}
TEAL, MUTED, TEXT, SEL = (45, 212, 191), (148, 163, 184), (235, 238, 242), (250, 204, 21)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(path, size):
    return ImageFont.truetype(path, max(8, int(size)))


def wrap(d, s, font, wmax):
    out, line = [], ''
    for word in str(s).split():
        t = (line + ' ' + word).strip()
        if d.textlength(t, font=font) <= wmax or not line:
            line = t
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


def pins_all(c):
    """Svi pinovi redom kao na stranici (ključ = indeks): isto kao piAll u JS-u."""
    k = c.get('kind')
    if k in ('dip', 'row'):
        return list(c.get('pins') or [])
    if k == 'grid':
        return [p for r in c.get('grid') or [] for p in r]
    return list(c.get('left') or []) + list(c.get('right') or []) + list(c.get('bottom') or [])


def render(cfg, w, h):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    c, sel = cfg.get('chip') or {}, int(cfg.get('sel', -1))
    mx = w * 0.03
    d.text((mx, h * 0.03), 'PINOUT', font=F(FONTB, h / 26), fill=TEAL)
    if not c.get('kind'):
        f = F(FONTB, h / 16)
        s = 'Izaberi čip u Alati → Pinout i datasheet'
        d.text(((w - d.textlength(s, font=f)) / 2, h * 0.45), s, font=f, fill=MUTED)
        return img
    d.text((mx, h * 0.075), c.get('n', ''), font=F(FONTB, h / 13), fill=TEXT)
    d.text((mx, h * 0.165), '%s · %s' % (c.get('pkg', ''), c.get('sub', '')), font=F(FONT, h / 32), fill=MUTED)

    # oblast crteža (levo) i bočna kolona (desno)
    ax, ay, aw, ah = mx, h * 0.23, w * 0.60, h * 0.74
    allp = pins_all(c)
    kind = c['kind']
    boxes = {}   # ključ -> (x0, y0, x1, y1) kvadratića pina (za isticanje)

    def pin(x, y, pw, ph, p, key, side, font):
        col = COL.get(p[1], COL['io'])
        on = key == sel
        d.rounded_rectangle([x, y, x + pw, y + ph], radius=max(2, ph // 4), fill=col, outline=SEL if on else None, width=max(2, int(ph / 6)) if on else 0)
        label = str(p[0])
        tw = d.textlength(label, font=font)
        ty = y + (ph - font.size) / 2 - 1
        if side == 'L':
            d.text((x - 8 - tw, ty), label, font=font, fill=SEL if on else TEXT)
        elif side == 'R':
            d.text((x + pw + 8, ty), label, font=font, fill=SEL if on else TEXT)
        boxes[key] = (x, y, x + pw, y + ph)

    if kind in ('dip', 'dual'):
        L = c['pins'][:len(c['pins']) // 2] if kind == 'dip' else c.get('left') or []
        R = list(reversed(c['pins'][len(c['pins']) // 2:])) if kind == 'dip' else c.get('right') or []
        bottom = c.get('bottom') or []
        rows = max(len(L), len(R), 1)
        lh = min(ah * (0.86 if bottom else 0.96) / (rows + 0.6), h / 12)
        ph, pw = lh * 0.66, lh * 0.95
        font = F(FONTM, lh * 0.55)
        bw = aw * (0.24 if kind == 'dip' else 0.30)
        bx = ax + (aw - bw) / 2
        by = ay + 4
        bh = rows * lh + lh * 0.6
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(lh * (0.3 if kind == 'dip' else 0.5)), fill=(31, 35, 40), outline=(0, 0, 0), width=2)
        if kind == 'dip':
            r = lh * 0.5
            d.pieslice([bx + bw / 2 - r, by - r, bx + bw / 2 + r, by + r], 0, 180, fill=BG)
            d.ellipse([bx + lh * 0.4, by + lh * 0.45, bx + lh * 0.65, by + lh * 0.7], fill=(58, 64, 72))
        else:
            d.rounded_rectangle([bx + bw * 0.12, by + lh * 0.4, bx + bw * 0.88, by + min(bh * 0.22, lh * 2.6)], radius=int(lh * 0.25), fill=(43, 49, 56))
        # ime čipa uspravno na telu
        nf = F(FONTM, min(bw * 0.42, lh * 0.75))
        tmp = Image.new('RGBA', (int(d.textlength(c.get('n', ''), font=nf)) + 4, nf.size + 6), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((2, 0), c.get('n', ''), font=nf, fill=(201, 209, 217))
        tmp = tmp.rotate(90, expand=True)
        if tmp.height < bh * 0.9:
            img.paste(tmp, (int(bx + (bw - tmp.width) / 2), int(by + (bh - tmp.height) / 2)), tmp)
        nfont = F(FONTM, lh * 0.42)
        for i, p in enumerate(L):
            y = by + lh * 0.45 + i * lh
            pin(bx - pw, y, pw, ph, p, i, 'L', font)
            if kind == 'dip':
                d.text((bx + lh * 0.2, y + (ph - nfont.size) / 2), str(i + 1), font=nfont, fill=(139, 148, 158))
        n = len(c.get('pins') or [])
        for i, p in enumerate(R):
            y = by + lh * 0.45 + i * lh
            key = (n - 1 - i) if kind == 'dip' else len(L) + i
            pin(bx + bw, y, pw, ph, p, key, 'R', font)
            if kind == 'dip':
                s = str(n - i)
                d.text((bx + bw - lh * 0.2 - d.textlength(s, font=nfont), y + (ph - nfont.size) / 2), s, font=nfont, fill=(139, 148, 158))
        if bottom:   # ESP-12: pinovi na donjoj ivici, natpisi uspravno ispod
            step = min(bw / (len(bottom) + 0.5), lh * 1.1)
            sx = bx + (bw - step * len(bottom)) / 2
            for i, p in enumerate(bottom):
                x = sx + i * step
                key = len(L) + len(R) + i
                col = COL.get(p[1], COL['io'])
                on = key == sel
                d.rounded_rectangle([x + step * 0.2, by + bh, x + step * 0.8, by + bh + pw], radius=3, fill=col, outline=SEL if on else None, width=3 if on else 0)
                t = Image.new('RGBA', (int(d.textlength(p[0], font=font)) + 4, font.size + 6), (0, 0, 0, 0))
                ImageDraw.Draw(t).text((2, 0), p[0], font=font, fill=SEL if on else TEXT)
                t = t.rotate(90, expand=True)
                img.paste(t, (int(x + step / 2 - t.width / 2), int(by + bh + pw + 6)), t)
    elif kind == 'grid':   # header 2×N (Raspberry Pi, ESP-01)
        grid = c.get('grid') or []
        cols = int(c.get('cols') or 2)
        rows = max(len(grid), 1)
        lh = min(ah * 0.98 / (rows + 0.4), h / 10)
        ps = lh * 0.78
        font = F(FONTM, lh * 0.56)
        bw = cols * lh * 1.15 + lh * 0.4
        bx = ax + (aw - bw) / 2
        by = ay + 2
        d.rounded_rectangle([bx, by, bx + bw, by + rows * lh + lh * 0.4], radius=int(lh * 0.3), fill=(31, 35, 40), outline=(0, 0, 0), width=2)
        numf = F(FONTM, ps * 0.42)
        for ri, r in enumerate(grid):
            for ci, p in enumerate(r):
                key = ri * cols + ci
                x = bx + lh * 0.3 + ci * lh * 1.15
                y = by + lh * 0.3 + ri * lh
                col = COL.get(p[1], COL['io'])
                on = key == sel
                if ri == 0 and ci == 0:
                    d.rounded_rectangle([x, y, x + ps, y + ps], radius=3, fill=col, outline=SEL if on else None, width=3 if on else 0)
                else:
                    d.ellipse([x, y, x + ps, y + ps], fill=col, outline=SEL if on else None, width=3 if on else 0)
                if c.get('num'):
                    s = str(key + 1)
                    d.text((x + (ps - d.textlength(s, font=numf)) / 2, y + (ps - numf.size) / 2 - 1), s, font=numf, fill=(255, 255, 255))
                label = str(p[0])
                ty = y + (ps - font.size) / 2 - 1
                if ci == 0:
                    d.text((x - 10 - d.textlength(label, font=font), ty), label, font=font, fill=SEL if on else TEXT)
                else:
                    d.text((x + ps + 10, ty), label, font=font, fill=SEL if on else TEXT)
    elif kind == 'row':   # SOT-223: nožice dole
        pins = c.get('pins') or []
        bw, bh = aw * 0.5, ah * 0.4
        bx, by = ax + (aw - bw) / 2, ay + ah * 0.12
        d.rectangle([bx + bw * 0.12, by - bh * 0.18, bx + bw * 0.88, by], fill=(31, 35, 40), outline=(0, 0, 0))
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=10, fill=(31, 35, 40), outline=(0, 0, 0), width=2)
        nf = F(FONTM, bh * 0.2)
        s = c.get('n', '')
        d.text((bx + (bw - d.textlength(s, font=nf)) / 2, by + bh * 0.38), s, font=nf, fill=(201, 209, 217))
        font = F(FONTM, h / 24)
        for i, p in enumerate(pins):
            x = bx + bw * (0.2 + 0.3 * i)
            on = i == sel
            d.rectangle([x - 8, by + bh, x + 8, by + bh + ah * 0.12], fill=(154, 164, 177))
            d.rounded_rectangle([x - 26, by + bh + ah * 0.13, x + 26, by + bh + ah * 0.2], radius=6, fill=COL.get(p[1], COL['io']), outline=SEL if on else None, width=3 if on else 0)
            d.text((x - d.textlength(p[0], font=font) / 2, by + bh + ah * 0.23), p[0], font=font, fill=SEL if on else TEXT)

    # bočna kolona: izabrani pin, legenda, napomene
    sx, sw = w * 0.66, w * 0.31
    y = h * 0.24
    if 0 <= sel < len(allp):
        p = allp[sel]
        col = COL.get(p[1], COL['io'])
        bh_ = h * 0.2
        d.rounded_rectangle([sx, y, sx + sw, y + bh_], radius=14, fill=(20, 26, 33), outline=SEL, width=3)
        d.text((sx + 16, y + 12), 'IZABRAN PIN', font=F(FONTB, h / 40), fill=SEL)
        d.rounded_rectangle([sx + 16, y + h * 0.065, sx + 16 + h * 0.035, y + h * 0.1], radius=4, fill=col)
        d.text((sx + 24 + h * 0.035, y + h * 0.058), str(p[0]), font=F(FONTM, h / 20), fill=TEXT)
        lines = wrap(d, p[2] or NAME.get(p[1], ''), F(FONT, h / 34), sw - 32)[:3]
        for i, ln in enumerate(lines):
            d.text((sx + 16, y + h * 0.115 + i * h * 0.033), ln, font=F(FONT, h / 34), fill=MUTED)
        y += bh_ + h * 0.03
    used = []
    for p in allp:
        if p[1] not in used:
            used.append(p[1])
    lf = F(FONT, h / 36)
    cx, cy = sx, y
    for k in [k for k in COL if k in used]:
        s = NAME[k]
        wd = h * 0.025 + 8 + d.textlength(s, font=lf) + 18
        if cx + wd > sx + sw:
            cx, cy = sx, cy + h * 0.042
        d.rounded_rectangle([cx, cy + 3, cx + h * 0.025, cy + 3 + h * 0.025], radius=3, fill=COL[k])
        d.text((cx + h * 0.025 + 8, cy), s, font=lf, fill=MUTED)
        cx += wd
    y = cy + h * 0.06
    nf = F(FONT, h / 32)
    for note in c.get('notes') or []:
        lines = wrap(d, note, nf, sw - 22)
        if y + len(lines) * h * 0.04 > h * 0.97:
            break
        d.ellipse([sx, y + nf.size * 0.35, sx + 8, y + nf.size * 0.35 + 8], fill=(240, 124, 26))
        for ln in lines:
            d.text((sx + 18, y), ln, font=nf, fill=(203, 213, 225))
            y += h * 0.038
        y += h * 0.012
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen   # slika ide kao sloj preko plejera (sve ostalo radi ispod)
    scr = Screen('pin')
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
            if os.environ.get('PIN_SNAP'):
                img.save(os.environ['PIN_SNAP'])
                return
            data, last = img.convert('RGBA').tobytes('raw', 'BGRA'), 0
        if time.time() - last > 2:
            scr.show(data, w, h)
            last = time.time()
        time.sleep(0.15)
    scr.close()


if __name__ == '__main__':
    main()
