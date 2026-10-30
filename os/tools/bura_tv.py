"""BURAscale na ekranu (sloj preko plejera mpv), izgled kao BURAscale.Client: displej sa masom, oznake STABLE / NET / WR / -0-,
dugmad (nula, tara, M+, štampa) i tastatura — dugmad se za sada samo prikazuju (touchscreen kasnije).
Stanje piše server u /dev/shm/teco-bura.json (do ~25x u sekundi); crta se samo kad se promeni, statični deo jednom.

python3 bura_tv.py [ignorisano]
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SRC = Path('/dev/shm/teco-bura.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-bura.json')
FD = '/usr/share/fonts/truetype/dejavu/'
FONT_B = FD + 'DejaVuSans-Bold.ttf'
# broj na displeju: uzan font kao Bahnschrift Light Condensed (prvi koji postoji)
FONT_NUM = next((f for f in (FD + 'DejaVuSansCondensed.ttf', '/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf',
                             '/usr/share/fonts/truetype/freefont/FreeSans.ttf', FD + 'DejaVuSans.ttf') if os.path.exists(f)), FD + 'DejaVuSans.ttf')
BW, BH = 937, 552   # osnovni raspored (kao prozor na slici), skalira se na ekran
BG, WIN, BAR, DISP, LINE = (214, 214, 214), (168, 168, 168), (150, 150, 150), (232, 232, 232), (120, 120, 120)
GREEN, YEL, RED, KEYBG = (200, 212, 0), (255, 232, 0), (220, 20, 60), (236, 236, 236)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


class Draw:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = min(w * 0.985 / BW, h * 0.985 / BH)   # skoro ceo ekran (čitljivo sa daljine)
        self.ox, self.oy = (w - BW * self.s) / 2, (h - BH * self.s) / 2
        self.fonts = {}

    def X(self, x):
        return self.ox + x * self.s

    def Y(self, y):
        return self.oy + y * self.s

    def R(self, x0, y0, x1, y1):
        return [self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)]

    def F(self, path, size):
        k = (path, int(size * self.s))
        if k not in self.fonts:
            self.fonts[k] = ImageFont.truetype(path, max(6, k[1]))
        return self.fonts[k]

    def text(self, d, x, y, t, path, size, fill, anchor='la'):
        d.text((self.X(x), self.Y(y)), t, font=self.F(path, size), fill=fill, anchor=anchor)


def icon(d, g, kind, cx, cy):
    """Ikonice dugmadi tankim crnim linijama: →0←, →◇T, M+, štampač."""
    s, k = g.s, (0, 0, 0)
    lw = max(1, int(2.2 * s))
    X, Y = g.X, g.Y
    if kind in ('zero', 'tare'):
        d.line([X(cx - 30), Y(cy), X(cx - 14), Y(cy)], fill=k, width=lw)
        d.line([X(cx - 19), Y(cy - 5), X(cx - 14), Y(cy), X(cx - 19), Y(cy + 5)], fill=k, width=lw)
    if kind == 'zero':
        d.ellipse([X(cx - 8), Y(cy - 13), X(cx + 8), Y(cy + 13)], outline=k, width=lw)
        d.line([X(cx + 30), Y(cy), X(cx + 14), Y(cy)], fill=k, width=lw)
        d.line([X(cx + 19), Y(cy - 5), X(cx + 14), Y(cy), X(cx + 19), Y(cy + 5)], fill=k, width=lw)
    elif kind == 'tare':
        d.polygon([X(cx - 10), Y(cy), X(cx + 8), Y(cy - 17), X(cx + 26), Y(cy), X(cx + 8), Y(cy + 17)], outline=k, width=lw)
        g.text(d, cx + 8, cy + 1, 'T', FONT_NUM, 17, k, 'mm')
    elif kind == 'mplus':
        g.text(d, cx, cy, 'M+', FONT_NUM, 34, k, 'mm')
    elif kind == 'print':
        d.rectangle([X(cx - 13), Y(cy - 17), X(cx + 13), Y(cy - 6)], outline=k, width=lw)
        d.rounded_rectangle([X(cx - 24), Y(cy - 7), X(cx + 24), Y(cy + 11)], radius=int(4 * s), outline=k, width=lw)
        d.rectangle([X(cx - 13), Y(cy + 3), X(cx + 13), Y(cy + 19)], fill=GREEN, outline=k, width=lw)
        for i in (8, 13):
            d.line([X(cx - 8), Y(cy + i), X(cx + 8), Y(cy + i)], fill=k, width=max(1, lw - 1))


def base(g):
    """Statični deo: prozor, traka menija, okvir displeja, dugmad, tastatura, natpisi."""
    img = Image.new('RGB', (g.w, g.h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle(g.R(0, 0, BW, BH), fill=WIN, outline=(110, 110, 110), width=max(1, int(g.s)))
    d.rectangle(g.R(0, 0, BW, 26), fill=(160, 160, 160))
    g.text(d, 14, 13, '⇕', FONT_B, 15, (20, 20, 20), 'lm')
    g.text(d, 36, 13, 'Komunikacija', FONT_B, 14, (55, 55, 55), 'lm')
    d.ellipse(g.R(160, 4, 179, 23), fill=(20, 20, 20))
    g.text(d, 169.5, 13.5, '?', FONT_B, 13, (255, 255, 255), 'mm')
    # displej
    d.rectangle(g.R(30, 50, 590, 278), fill=DISP, outline=LINE, width=max(1, int(g.s)))
    # dugmad ispod displeja (zelena)
    for i, kind in enumerate(('zero', 'tare', 'mplus', 'print')):
        x0 = 30 + i * 143.5
        d.rectangle(g.R(x0, 296, x0 + 130, 368), fill=KEYBG)
        d.rectangle(g.R(x0 + 6, 302, x0 + 124, 362), fill=GREEN)
        icon(d, g, kind, x0 + 65, 332)
    # tastatura
    keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '.', '0', 'C']
    for i, kk in enumerate(keys):
        c, r = i % 3, i // 3
        x0, y0 = 618 + c * 92, 50 + r * 82
        d.rectangle(g.R(x0, y0, x0 + 84, y0 + 72), fill=KEYBG)
        d.rectangle(g.R(x0 + 6, y0 + 6, x0 + 78, y0 + 66), fill=RED if kk == 'C' else YEL)
        g.text(d, x0 + 42, y0 + 38, kk, FONT_B, 30, (100, 0, 10) if kk == 'C' else (0, 0, 0), 'mm')
    # natpis vage i Tecomatic
    g.text(d, 772, 412, 'BURAscale', FONT_B, 38, (34, 34, 34), 'mm')
    g.text(d, 866, 448, 'Tecomatic', FONT_B, 15, (20, 20, 20), 'mm')
    d.rectangle(g.R(0, BH - 34, BW, BH), fill=BAR)
    return img


DISP_BOX = (30, 50, 590, 278)   # deo koji se menja uz masu (osnovne koordinate)


class Glyphs:
    """Velike cifre nacrtane jednom (maska po znaku), posle se samo lepe — tekst od ~130 px je najsporiji deo crtanja."""

    def __init__(self, font):
        self.font, self.cache = font, {}
        asc, desc = font.getmetrics()
        self.asc, self.hgt = asc, asc + desc
        bb = font.getbbox('0', anchor='ls')
        self.mid = (bb[1] + bb[3]) / 2   # sredina cifre u odnosu na osnovnu liniju (negativno)

    def get(self, ch):
        if ch not in self.cache:
            w = max(1, int(round(self.font.getlength(ch))))
            m = Image.new('L', (w + 2, self.hgt), 0)
            ImageDraw.Draw(m).text((0, self.asc), ch, font=self.font, fill=255, anchor='ls')
            self.cache[ch] = (m, self.font.getlength(ch))
        return self.cache[ch]

    def draw(self, img, x_right, y_mid, s, fill):
        """s poravnat desno na x_right, sredina cifara na y_mid."""
        parts = [self.get(ch) for ch in s]
        x = x_right - sum(adv for _, adv in parts)
        top = int(round(y_mid - self.mid - self.asc))
        for m, adv in parts:
            img.paste(fill, (int(round(x)), top), m)
            x += adv


def static_layer(g, bimg, v):
    """Ono što se retko menja: natpis klase / Max / Min i statusna traka."""
    img = bimg.copy()
    d = ImageDraw.Draw(img)
    on = v.get('online')
    d.rectangle(g.R(30, 386, 600, 464), fill=(208, 208, 208), outline=(110, 110, 110), width=max(2, int(3 * g.s)))
    u = v.get('u') or 'kg'
    mx = (v.get('max2') + '/' if v.get('max2') else '') + (v.get('max') or '—') + u
    line = '   '.join(x for x in ('Klasa ' + v['cls'] if v.get('cls') else '', 'Max ' + mx if v.get('max') else '', 'Min ' + v['minw'] if v.get('minw') else '') if x) or 'BURAscale'
    g.text(d, 315, 412, line, FONT_B, 24, (25, 25, 25), 'mm')
    g.text(d, 315, 446, 'Ne sme se koristiti za direktni obračun', FONT_B, 15, (200, 0, 20), 'mm')
    st = ('Povezano ' + (v.get('dev') or '').replace('/dev/', '') + (' | FW ' + v['fw'] if v.get('fw') else '') + (' CK ' + v['ck'] if v.get('ck') else '')
          + (' | SN ' + v['sn'] if v.get('sn') else '')) if on else ('Nije povezano' + (' · ' + v['err'] if v.get('err') else ''))
    g.text(d, 20, BH - 17, st, FONT_B, 16, (245, 245, 245), 'lm')
    return img


def disp_layer(g, simg, v, glyphs):
    """Displej (oznake, masa, jedinica) kao isečak; ostatak slike se ne dira."""
    box = [int(round(x)) for x in g.R(*DISP_BOX)]
    img = simg.crop(box)
    d = ImageDraw.Draw(img)
    ox, oy = box[0], box[1]
    on = v.get('online')
    dim, blk = (190, 190, 190), (10, 10, 10)

    def txt(x, y, t, size, fill, anchor):
        d.text((g.X(x) - ox, g.Y(y) - oy), t, font=g.F(FONT_B, size), fill=fill, anchor=anchor)
    # oznake stanja krupno (čitljivo sa TV-a): STABLE, NET, < MIN, WR, -0-
    txt(44, 76, 'STABLE', 22, blk if v.get('stable') else dim, 'lm')
    txt(168, 76, 'NET', 22, blk if v.get('net') else dim, 'lm')
    if v.get('min'):
        txt(300, 76, '< MIN', 22, (200, 90, 0), 'mm')
    txt(470, 76, 'WR' + str(v.get('range') or '1'), 22, blk if on else dim, 'mm')
    if v.get('zero'):
        txt(552, 76, '-0-', 22, blk, 'mm')
    disp = str(v.get('disp') or '')
    gl = glyphs[0] if len(disp) <= 7 else glyphs[1]
    gl.draw(img, g.X(480) - ox, g.Y(196) - oy, disp, (0, 0, 0) if on else (110, 110, 110))
    if v.get('unit'):
        d.text((g.X(535) - ox, g.Y(219) - oy), v['unit'], font=g.F(FONT_NUM, 50), fill=(0, 0, 0), anchor='mm')
    return img, box


def render(g, bimg, v):   # cela slika (probni snimak)
    simg = static_layer(g, bimg, v)
    glyphs = (Glyphs(g.F(FONT_NUM, 118)), Glyphs(g.F(FONT_NUM, 92)))
    img, box = disp_layer(g, simg, v, glyphs)
    simg.paste(img, box[:2])
    return simg


def bgra(img):
    import numpy as np
    a = np.asarray(img.convert('RGB'))
    out = np.empty(a.shape[:2] + (4,), np.uint8)
    out[..., 0], out[..., 1], out[..., 2], out[..., 3] = a[..., 2], a[..., 1], a[..., 0], 255
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('bura')
    w, h = scr.size()
    g = Draw(w, h)
    bimg = base(g)
    glyphs = (Glyphs(g.F(FONT_NUM, 118)), Glyphs(g.F(FONT_NUM, 92)))
    SKEYS = ('online', 'dev', 'fw', 'ck', 'sn', 'err', 'cls', 'max', 'max2', 'minw', 'u')
    mt, frame, simg, skey, prev, last, dirty = object(), None, None, None, None, 0, False
    while running:
        try:
            m = SRC.stat().st_mtime_ns
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                v = json.loads(SRC.read_text())
            except (OSError, ValueError):
                v = {'online': False, 'disp': 'N/A'}
            key = json.dumps(v, sort_keys=True)
            if key != prev:   # samo kad se prikaz stvarno promeni
                prev = key
                sk = json.dumps([v.get(k) for k in SKEYS])
                if sk != skey or frame is None:   # natpis / status: cela slika (retko)
                    skey, simg = sk, static_layer(g, bimg, v)
                    frame = bgra(simg)
                img, box = disp_layer(g, simg, v, glyphs)   # masa: samo isečak displeja (brzo)
                if os.environ.get('BURA_SNAP'):
                    simg.paste(img, box[:2])
                    simg.save(os.environ['BURA_SNAP'])
                    return
                frame[box[1]:box[3], box[0]:box[2]] = bgra(img)
                dirty = True
        if frame is not None and (dirty or time.time() - last > 2):   # i bez promene ponovo na 2 s (plejer je mogao da obriše sloj)
            scr.show(frame.tobytes(), w, h)
            last, dirty = time.time(), False
        time.sleep(0.01)
    scr.close()


if __name__ == '__main__':
    main()
