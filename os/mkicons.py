"""Ikonice za početni ekran telefona (PWA) iz Teco.Pi znaka (static/favicon.svg).

iPhone traži punu kvadratnu ikonicu bez providnih delova (sam zaobljuje ivice),
Android koristi 192/512 i 'maskable' sa znakom unutar bezbedne zone.
Pokreni: python mkicons.py  (piše u static/)
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / 'static'
TOP, BOT = (0x2d, 0xd4, 0xbf), (0x0e, 0x5f, 0x73)


def icon(size, rounded=False, ring=1.0):
    k = 4   # crta se 4x veće pa se smanjuje (glatke ivice)
    s = size * k
    # gradijent kao u favicon.svg: od gore-levo ka dole (x2=.35, y2=1)
    grad = Image.new('RGB', (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(0, s, 4):
            t = min(1, max(0, (0.35 * x / s + y / s) / 1.1225))
            c = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOT))
            for i in range(4):
                if x + i < s:
                    px[x + i, y] = c
    img = grad.convert('RGBA')
    # stakleni odsjaj na gornjoj polovini
    glass = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glass)
    h = int(s * 33 / 60)
    for y in range(h):
        a = max(0, 0.35 * (1 - y / (h * 0.5))) if y < h * 0.5 else 0
        gd.line([(0, y), (s, y)], fill=(255, 255, 255, round(255 * a)))
    img = Image.alpha_composite(img, glass)
    d = ImageDraw.Draw(img)
    u = s / 60 * ring   # jedinica iz viewBox-a (znak je 60 široko)
    c = s / 2
    r, w, dot = 14.4 * u, 5.4 * u, 4.5 * u
    if ring:   # ring=0: samo podloga (ekran pri otvaranju crta prsten sam, animiran)
        d.ellipse([c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2], outline='white', width=round(w))
        d.ellipse([c - dot, c - dot, c + dot, c + dot], fill='white')
    if rounded:   # kao favicon: zaobljen kvadrat (rx 17 od 60)
        m = Image.new('L', (s, s), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, s - 1, s - 1], radius=round(s * 17 / 60), fill=255)
        img.putalpha(m)
    return img.resize((size, size), Image.LANCZOS)


# iPhone ekrani (CSS širina, visina, gustina): iOS prikazuje sliku za otvaranje samo ako tačno odgovara ekranu
IPHONES = [(440, 956, 3), (420, 912, 3), (402, 874, 3), (430, 932, 3), (393, 852, 3), (428, 926, 3), (390, 844, 3),
           (375, 812, 3), (414, 896, 3), (414, 896, 2), (414, 736, 3), (375, 667, 2), (320, 568, 2)]
SPLASH_BG = (0x0b, 0x10, 0x18)


def splash(w, h):
    """Slika dok se otvara prečica: tamna pozadina, znak i Teco.Pi (kao na ekranu pri pokretanju)."""
    from PIL import ImageFont
    img = Image.new('RGB', (w, h), SPLASH_BG)
    s = round(w * 0.28)
    logo = icon(s, rounded=True)
    y = round(h * 0.42 - s / 2)
    img.paste(logo, ((w - s) // 2, y), logo)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/seguisb.ttf', round(w * 0.085))
    except OSError:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    d.text((w / 2, y + s + w * 0.07), 'Teco.Pi', font=font, fill=(0xe6, 0xed, 0xf5), anchor='mt')
    return img


def splash_links():
    return [('<link rel="apple-touch-startup-image" media="screen and (device-width: %dpx) and (device-height: %dpx) and '
             '(-webkit-device-pixel-ratio: %d) and (orientation: portrait)" href="/static/splash/%dx%d.png">') % (cw, ch, r, cw * r, ch * r)
            for cw, ch, r in IPHONES]


if __name__ == '__main__':
    icon(180).convert('RGB').save(OUT / 'apple-touch-icon.png')
    icon(192, rounded=True).save(OUT / 'icon-192.png')
    icon(512, rounded=True).save(OUT / 'icon-512.png')
    icon(512, ring=0.8).convert('RGB').save(OUT / 'icon-maskable-512.png')
    icon(256, rounded=True, ring=0).save(OUT / 'boot-bg.png', optimize=True)   # build.ps1 ga ugrađuje u boot.html
    (OUT / 'splash').mkdir(exist_ok=True)
    for cw, ch, r in IPHONES:
        splash(cw * r, ch * r).save(OUT / 'splash' / ('%dx%d.png' % (cw * r, ch * r)), optimize=True)
    (OUT / 'splash' / 'links.html').write_text('\n'.join(splash_links()) + '\n')   # build.ps1 ih ubacuje u <head>
    print('ikonice su u', OUT)
