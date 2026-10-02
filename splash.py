"""Teco.Pi logo za ekran: pri pokretanju Raspberry-ja (framebuffer) i kad plejer nema šta da prikaže.

python3 splash.py  ->  data/splash.fb (sirova slika za /dev/fb0, koristi je teco-splash.service)
"""
import subprocess
import sys
from pathlib import Path


def svg(w, h):
    """Logo u „virtuelnoj“ širini (ekran je 16:9, slika se posle stisne na 720x576)."""
    s = h * 0.24                       # znak (zaobljeni kvadrat)
    cx, my = w / 2, h * 0.36
    x0, y0 = cx - s / 2, my - s / 2
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs>
  <radialGradient id="bg" cx="50%" cy="38%" r="70%"><stop offset="0" stop-color="#0f3b3a"/><stop offset=".55" stop-color="#0a1418"/><stop offset="1" stop-color="#05080b"/></radialGradient>
  <linearGradient id="mk" x1="0" y1="0" x2=".35" y2="1"><stop offset="0" stop-color="#2dd4bf"/><stop offset="1" stop-color="#0e5f73"/></linearGradient>
  <linearGradient id="gl" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".35"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient>
</defs>
<rect width="{w}" height="{h}" fill="url(#bg)"/>
<rect x="{x0:.0f}" y="{y0 + s * 0.06:.0f}" width="{s:.0f}" height="{s:.0f}" rx="{s * 0.28:.0f}" fill="#000" opacity=".35"/>
<rect x="{x0:.0f}" y="{y0:.0f}" width="{s:.0f}" height="{s:.0f}" rx="{s * 0.28:.0f}" fill="url(#mk)"/>
<rect x="{x0:.0f}" y="{y0:.0f}" width="{s:.0f}" height="{s * 0.55:.0f}" rx="{s * 0.28:.0f}" fill="url(#gl)"/>
<circle cx="{cx:.0f}" cy="{my:.0f}" r="{s * 0.24:.0f}" fill="none" stroke="#fff" stroke-width="{s * 0.09:.0f}"/>
<circle cx="{cx:.0f}" cy="{my:.0f}" r="{s * 0.075:.0f}" fill="#fff"/>
<text x="{cx:.0f}" y="{h * 0.72:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.14:.0f}" letter-spacing="-2" fill="#ffffff">Teco.Pi</text>
<text x="{cx:.0f}" y="{h * 0.82:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.042:.0f}" letter-spacing="6" fill="#7dd3c7">by Tecomatic</text>
</svg>'''


def make_fb(out, w=720, h=576, vw=1024):
    """Slika za /dev/fb0: RGB565 (16 bita), kao što ekran (vc4drmfb) očekuje."""
    from PIL import Image
    src = Path(out).with_suffix('.svg')
    png = Path(out).with_suffix('.png')
    src.write_text(svg(vw, h))
    subprocess.run(['rsvg-convert', '-w', str(vw), '-h', str(h), '-o', str(png), str(src)], check=True)
    im = Image.open(png).convert('RGB').resize((w, h), Image.LANCZOS)
    data = bytearray(w * h * 2)
    for i, (r, g, b) in enumerate(im.getdata()):   # RGB565, little-endian
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        data[2 * i] = v & 0xFF
        data[2 * i + 1] = v >> 8
    Path(out).write_bytes(bytes(data))
    src.unlink()
    return png


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).with_name('data') / 'splash.fb')
    print(make_fb(out))
