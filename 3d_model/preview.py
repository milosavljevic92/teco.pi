"""Shaded preview of the assembled case from the real STL parts.
Small numpy z-buffer rasterizer: flat shading, outline from depth/normal edges, 2x supersampling,
screen rendered as a textured quad."""
import sys, numpy as np, trimesh
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT, PNG = sys.argv[1], sys.argv[2]
D, DZ = 38.31, 34.5
def T(r): return np.array(r, float)
SHELL = (0.235, 0.24, 0.255); BEZEL = (0.17, 0.175, 0.185); TRAY = (0.13, 0.135, 0.145); PLATE = (0.2, 0.205, 0.22)
PARTS = [
    ('top', np.eye(4), SHELL),
    ('screen-bezel', T([[1, 0, 0, 0], [0, 1, 0, 436.05], [0, 0, 1, -16.0], [0, 0, 0, 1]]), BEZEL),
    ('back-cover', T([[1, 0, 0, 0], [0, -1, 0, 682.90 - D], [0, 0, -1, 110.04 - DZ], [0, 0, 0, 1]]), SHELL),
    ('bottom', T([[1, 0, 0, 0], [0, 0, -1, 238.22 - 5.08], [0, 1, 0, 38.51], [0, 0, 0, 1]]), TRAY),
    ('front-plate', T([[1, 0, 0, 0], [0, 1, 0, 362.94], [0, 0, 1, 8.32], [0, 0, 0, 1]]), PLATE),
]
# world frame: x right, y up, z towards the viewer of the front (shell: y down, z back)
W = T([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]])

tris, cols, ids = [], [], []
for k, (f, M, col) in enumerate(PARTS):
    m = trimesh.load(f'{OUT}\\{f}.stl'); m.apply_transform(W @ M)
    tris.append(m.triangles); cols.append(np.tile(col, (len(m.faces), 1))); ids.append(np.full(len(m.faces), k))
# button caps in their holes (grey) and the PWR cap (orange, like the original)
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frontpanel import caps_assembled
for m, is_pwr in caps_assembled():
    m.apply_transform(W @ PARTS[4][1])
    tris.append(m.triangles); cols.append(np.tile((0.95, 0.5, 0.12) if is_pwr else (0.42, 0.43, 0.46), (len(m.faces), 1)))
    ids.append(np.full(len(m.faces), 50))
tris = np.concatenate(tris); cols = np.concatenate(cols); ids = np.concatenate(ids)

# screen quad (in the bezel window, just behind the chamfer), world coords
wx0, wx1 = -34.62, 118.99
wy0, wy1 = -(-292.10 + 436.05), -(-206.78 + 436.05)
wz = -(-16.0 + 1.3)
SCREEN = np.array([[wx0, wy0, wz], [wx1, wy0, wz], [wx1, wy1, wz], [wx0, wy1, wz]])

def screen_texture(w=1024, h=600):
    img = Image.new('RGB', (w, h))
    px = np.zeros((h, w, 3))
    t = np.linspace(0, 1, h)[:, None]
    top, bot = np.array([10, 74, 82]), np.array([4, 12, 34])
    px[:] = (top * (1 - t) + bot * t)[:, None, :]
    yy, xx = np.mgrid[0:h, 0:w]
    glow = np.exp(-(((xx - w / 2) / (w * 0.35)) ** 2 + ((yy - h * 0.42) / (h * 0.35)) ** 2))
    px += glow[..., None] * np.array([18, 60, 64])
    img = Image.fromarray(np.clip(px, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    def font(sz, bold=True):
        for name in (('segoeuib.ttf' if bold else 'segoeui.ttf'), 'arialbd.ttf', 'arial.ttf'):
            try: return ImageFont.truetype(name, sz)
            except OSError: pass
        return ImageFont.load_default()
    d.text((w / 2, h * 0.40), 'Teco.Pi', font=font(150), fill=(232, 246, 246), anchor='mm')
    d.text((w / 2, h * 0.62), 'YT   ·   AV   ·   FM   ·   TV', font=font(46, False), fill=(140, 205, 205), anchor='mm')
    d.text((w - 40, 40), '21.4°', font=font(40, False), fill=(170, 220, 220), anchor='rt')
    # faint scanlines for the CRT feel
    a = np.asarray(img).astype(float); a[::4] *= 0.86
    return a / 255.0

TEX = screen_texture()

def look_at(eye, target, up=(0, 1, 0)):
    f = np.asarray(target, float) - eye; f /= np.linalg.norm(f)
    r = np.cross(f, up); r /= np.linalg.norm(r); u = np.cross(r, f)
    return r, u, f

def render(eye, target, light, size=(1500, 1000), fov=24.0, ss=2):
    Wd, Ht = size[0] * ss, size[1] * ss
    eye = np.asarray(eye, float)
    r, u, f = look_at(eye, target)
    fpx = (Ht / 2) / np.tan(np.radians(fov) / 2)
    def project(P):
        q = P - eye
        x, y, z = q @ r, q @ u, q @ f
        return np.stack([Wd / 2 + fpx * x / z, Ht / 2 - fpx * y / z, z], axis=-1)
    zbuf = np.full((Ht, Wd), np.inf); img = np.zeros((Ht, Wd, 3)); nbuf = np.zeros((Ht, Wd, 3)); idb = np.full((Ht, Wd), -1)
    L = np.asarray(light, float); L /= np.linalg.norm(L)
    L2 = np.array([-L[0], 0.4, 0.6]); L2 /= np.linalg.norm(L2)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]); ln = np.linalg.norm(n, axis=1); ok = ln > 1e-12
    n[ok] /= ln[ok, None]
    view = (tris.mean(1) - eye); facing = np.einsum('ij,ij->i', n, view) < 0
    sh = 0.32 + 0.75 * np.clip(n @ L, 0, 1) + 0.18 * np.clip(n @ L2, 0, 1)
    shade = np.clip(cols * sh[:, None] + 0.05 * np.clip(n[:, 1:2], 0, 1), 0, 1)
    P = project(tris)
    sel = np.where(ok & facing & (P[:, :, 2].min(1) > 1))[0]

    for i in sel:
        p = P[i]
        x0 = int(max(np.floor(p[:, 0].min()), 0)); x1 = int(min(np.ceil(p[:, 0].max()), Wd - 1))
        y0 = int(max(np.floor(p[:, 1].min()), 0)); y1 = int(min(np.ceil(p[:, 1].max()), Ht - 1))
        if x1 < x0 or y1 < y0:
            continue
        xs = np.arange(x0, x1 + 1) + 0.5; ys = np.arange(y0, y1 + 1) + 0.5
        X, Y = np.meshgrid(xs, ys)
        (ax_, ay, az), (bx, by, bz), (cx, cy, cz) = p
        den = (by - cy) * (ax_ - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-9:
            continue
        w0 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / den
        w1 = ((cy - ay) * (X - cx) + (ax_ - cx) * (Y - cy)) / den
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        z = 1.0 / (w0 / az + w1 / bz + w2 / cz)
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (z < sub)
        if not upd.any():
            continue
        sub[upd] = z[upd]
        img[y0:y1 + 1, x0:x1 + 1][upd] = shade[i]
        nbuf[y0:y1 + 1, x0:x1 + 1][upd] = n[i]
        idb[y0:y1 + 1, x0:x1 + 1][upd] = ids[i]

    # screen quad with texture (perspective-correct uv), slightly emissive
    S = project(SCREEN)
    screen_tris = () if eye[2] < wz else (((0, 1, 2), ((0, 1), (1, 1), (1, 0))), ((0, 2, 3), ((0, 1), (1, 0), (0, 0))))
    for tri, uv in screen_tris:
        p = S[list(tri)]; uv = np.array(uv, float)
        x0 = int(max(np.floor(p[:, 0].min()), 0)); x1 = int(min(np.ceil(p[:, 0].max()), Wd - 1))
        y0 = int(max(np.floor(p[:, 1].min()), 0)); y1 = int(min(np.ceil(p[:, 1].max()), Ht - 1))
        X, Y = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        (ax_, ay, az), (bx, by, bz), (cx, cy, cz) = p
        den = (by - cy) * (ax_ - cx) + (cx - bx) * (ay - cy)
        w0 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / den
        w1 = ((cy - ay) * (X - cx) + (ax_ - cx) * (Y - cy)) / den
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        iz = w0 / az + w1 / bz + w2 / cz; z = 1 / iz
        uu = (w0 * uv[0, 0] / az + w1 * uv[1, 0] / bz + w2 * uv[2, 0] / cz) / iz
        vv = (w0 * uv[0, 1] / az + w1 * uv[1, 1] / bz + w2 * uv[2, 1] / cz) / iz
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (z < sub)
        sub[upd] = z[upd]
        th, tw = TEX.shape[:2]
        tx = np.clip((uu * (tw - 1)).astype(int), 0, tw - 1); ty = np.clip(((1 - vv) * (th - 1)).astype(int), 0, th - 1)
        img[y0:y1 + 1, x0:x1 + 1][upd] = TEX[ty[upd], tx[upd]]
        nbuf[y0:y1 + 1, x0:x1 + 1][upd] = (0, 0, 1)
        idb[y0:y1 + 1, x0:x1 + 1][upd] = 99

    # outlines: depth or normal discontinuities
    bg = ~np.isfinite(zbuf)
    zf = np.where(bg, 1e6, zbuf)
    dz = np.zeros_like(zf)
    dz[1:-1, 1:-1] = np.maximum.reduce([abs(zf[1:-1, 1:-1] - zf[:-2, 1:-1]), abs(zf[1:-1, 1:-1] - zf[2:, 1:-1]),
                                        abs(zf[1:-1, 1:-1] - zf[1:-1, :-2]), abs(zf[1:-1, 1:-1] - zf[1:-1, 2:])])
    dn = np.zeros(zf.shape)
    dn[1:-1, 1:-1] = np.maximum.reduce([np.linalg.norm(nbuf[1:-1, 1:-1] - nbuf[:-2, 1:-1], axis=-1),
                                        np.linalg.norm(nbuf[1:-1, 1:-1] - nbuf[1:-1, :-2], axis=-1)])
    edge = (dz > 0.012 * zf) | (dn > 0.35)
    img[edge & (idb != 99)] *= 0.45
    # background: soft studio gradient + contact shadow
    gy = np.linspace(0, 1, Ht)[:, None, None]
    bgc = np.array([0.93, 0.935, 0.94]) * (1 - gy) + np.array([0.80, 0.81, 0.82]) * gy
    out = np.where(bg[..., None], np.broadcast_to(bgc, img.shape), img)
    sil = Image.fromarray((~bg * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(ss * 18))
    shadow = np.asarray(sil, float)[..., None] / 255.0
    shadow = np.roll(shadow, int(ss * 22), axis=0)
    out = np.where(bg[..., None], out * (1 - 0.35 * shadow), out)
    im = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
    return im.resize(size, Image.LANCZOS)

# bounding box centre of the assembly (world)
lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0); c = (lo + hi) / 2
front = render(eye=c + np.array([-260, 150, 520]), target=c + np.array([0, -8, 0]), light=(-0.5, 0.7, 0.8))
back = render(eye=c + np.array([300, 170, -480]), target=c + np.array([0, -8, 0]), light=(0.6, 0.7, -0.8))
sheet = Image.new('RGB', (front.width + back.width, front.height), 'white')
sheet.paste(front, (0, 0)); sheet.paste(back, (front.width, 0))
sheet.save(PNG)
front.save(PNG.replace('.png', '-front.png')); back.save(PNG.replace('.png', '-back.png'))
# close-up of the button strip
strip_c = np.array([42.0, -248.8, 18.0])
strip = render(eye=strip_c + np.array([-35, 55, 230]), target=strip_c + np.array([0, 4, 0]), light=(-0.4, 0.8, 0.9),
               size=(1800, 700), fov=17)
strip.save(PNG.replace('.png', '-strip.png'))
print('ok', sheet.size)
