"""Assembled views from all sides + close-ups of the seams (uses the scene and renderer of preview.py).

    python views.py <elements folder> <out folder>
"""
import os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [os.path.join(HERE, 'preview.py'), sys.argv[1], os.path.join(OUT_DIR, '_unused.png')]
src = open(os.path.join(HERE, 'preview.py'), encoding='utf-8').read().split('# bounding box centre')[0]
g = {'__file__': os.path.join(HERE, 'preview.py'), '__name__': 'views'}
exec(compile(src, 'preview', 'exec'), g)
render, tris = g['render'], g['tris']
lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0); c = (lo + hi) / 2

def font(sz):
    for n in ('segoeuib.ttf', 'arialbd.ttf'):
        try: return ImageFont.truetype(n, sz)
        except OSError: pass
    return ImageFont.load_default()

def shot(name, eye, target=c, light=(0.4, 0.7, 0.6), size=(1200, 900), fov=26, title=None):
    im = render(eye=np.asarray(eye, float), target=np.asarray(target, float), light=light, size=size, fov=fov)
    if title:
        ImageDraw.Draw(im).text((24, 18), title, font=font(30), fill=(40, 40, 45))
    im.save(os.path.join(OUT_DIR, name))
    return im

# world: x right, y up, z towards the viewer of the front
views = [
    ('view-front.png', c + [0, 20, 560], (-0.3, 0.6, 0.8), 'front'),
    ('view-back.png', c + [0, 40, -560], (0.3, 0.6, -0.8), 'back'),
    ('view-left.png', c + [-560, 30, 0], (-0.8, 0.6, 0.2), 'left'),
    ('view-right.png', c + [560, 30, 0], (0.8, 0.6, 0.2), 'right'),
    ('view-top.png', c + [0, 560, 60], (0.2, 0.9, 0.3), 'top'),
    ('view-bottom.png', c + [0, -560, 60], (0.2, -0.9, 0.3), 'bottom'),
]
ims = [shot(n, e, light=l, title=t, size=(900, 700)) for n, e, l, t in views]
sheet = Image.new('RGB', (900 * 3, 700 * 2), 'white')
for k, im in enumerate(ims):
    sheet.paste(im, ((k % 3) * 900, (k // 3) * 700))
sheet.save(os.path.join(OUT_DIR, 'views.png'))

# seams close-up: bezel / shell / strip / tray (front left corner), back cover / shell / tray (back corner)
z_front = hi[2]; z_back = lo[2]
seams = [
    ('seam-front-top.png', [lo[0] + 10, hi[1] - 5, z_front - 30], [-120, 70, 120], 'bezel - shell, top left'),
    ('seam-front-bottom.png', [lo[0] + 20, lo[1] + 35, z_front - 25], [-110, -20, 140], 'bezel/strip - tray - shell, bottom left'),
    ('seam-back.png', [hi[0] - 15, lo[1] + 40, z_back + 15], [130, 40, -140], 'back cover - shell - tray, back right'),
    ('seam-back-top.png', [lo[0] + 15, hi[1] - 10, z_back + 10], [-120, 90, -120], 'back cover - shell, top left'),
]
ims = [shot(n, np.asarray(t) + d, target=t, light=(np.sign(d[0]) * 0.5, 0.7, np.sign(d[2]) * 0.6), fov=22, title=ti, size=(900, 700))
       for n, t, d, ti in seams]
sheet = Image.new('RGB', (900 * 2, 700 * 2), 'white')
for k, im in enumerate(ims):
    sheet.paste(im, ((k % 2) * 900, (k // 2) * 700))
sheet.save(os.path.join(OUT_DIR, 'seams.png'))
# logo on the left side wall + the strip/tray corner below it
lc = np.array([-51.0, -225.0, -10.0])
shot('preview-logo.png', lc + [-170, 20, 40], target=lc, light=(-0.8, 0.5, 0.3), size=(1200, 800), fov=30)
print('ok')
