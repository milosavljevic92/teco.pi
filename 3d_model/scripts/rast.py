"""Tiny numpy z-buffer rasterizer: flat shading, outlines from depth/normal edges, supersampling."""
import numpy as np
from PIL import Image, ImageFilter

def look_at(eye, target, up=(0, 1, 0)):
    f = np.asarray(target, float) - eye; f /= np.linalg.norm(f)
    r = np.cross(f, up); r /= np.linalg.norm(r); u = np.cross(r, f)
    return r, u, f

def render(tris, cols, eye, target, light, size=(1500, 1000), fov=24.0, ss=2, quads=()):
    """tris (n,3,3) world coords (y up), cols (n,3); quads: [(4x3 corners, texture HxWx3)]"""
    Wd, Ht = size[0] * ss, size[1] * ss
    eye = np.asarray(eye, float)
    r, u, f = look_at(eye, target)
    fpx = (Ht / 2) / np.tan(np.radians(fov) / 2)
    def project(P):
        q = P - eye
        x, y, z = q @ r, q @ u, q @ f
        return np.stack([Wd / 2 + fpx * x / z, Ht / 2 - fpx * y / z, z], axis=-1)
    zbuf = np.full((Ht, Wd), np.inf); img = np.zeros((Ht, Wd, 3)); nbuf = np.zeros((Ht, Wd, 3))
    tex_mask = np.zeros((Ht, Wd), bool)
    L = np.asarray(light, float); L /= np.linalg.norm(L)
    L2 = np.array([-L[0], 0.4, 0.6]); L2 /= np.linalg.norm(L2)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]); ln = np.linalg.norm(n, axis=1); ok = ln > 1e-12
    n[ok] /= ln[ok, None]
    facing = np.einsum('ij,ij->i', n, tris.mean(1) - eye) < 0
    sh = 0.32 + 0.75 * np.clip(n @ L, 0, 1) + 0.18 * np.clip(n @ L2, 0, 1)
    shade = np.clip(cols * sh[:, None] + 0.05 * np.clip(n[:, 1:2], 0, 1), 0, 1)
    P = project(tris)

    def bary(p, x0, x1, y0, y1):
        X, Y = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        (ax, ay, _), (bx, by, _), (cx, cy, _) = p
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-9:
            return None
        w0 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / den
        w1 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / den
        return w0, w1, 1 - w0 - w1

    def bbox(p):
        return (int(max(np.floor(p[:, 0].min()), 0)), int(min(np.ceil(p[:, 0].max()), Wd - 1)),
                int(max(np.floor(p[:, 1].min()), 0)), int(min(np.ceil(p[:, 1].max()), Ht - 1)))

    for i in np.where(ok & facing & (P[:, :, 2].min(1) > 1))[0]:
        p = P[i]; x0, x1, y0, y1 = bbox(p)
        if x1 < x0 or y1 < y0:
            continue
        b = bary(p, x0, x1, y0, y1)
        if b is None:
            continue
        w0, w1, w2 = b
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        z = 1.0 / (w0 / p[0, 2] + w1 / p[1, 2] + w2 / p[2, 2])
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (z < sub)
        if not upd.any():
            continue
        sub[upd] = z[upd]
        img[y0:y1 + 1, x0:x1 + 1][upd] = shade[i]
        nbuf[y0:y1 + 1, x0:x1 + 1][upd] = n[i]
        tex_mask[y0:y1 + 1, x0:x1 + 1][upd] = False

    for corners, tex in quads:
        nq = np.cross(corners[1] - corners[0], corners[3] - corners[0])
        if nq @ (corners.mean(0) - eye) >= 0:
            continue
        S = project(corners)
        for tri, uv in (((0, 1, 2), ((0, 1), (1, 1), (1, 0))), ((0, 2, 3), ((0, 1), (1, 0), (0, 0)))):
            p = S[list(tri)]; uv = np.array(uv, float); x0, x1, y0, y1 = bbox(p)
            b = bary(p, x0, x1, y0, y1)
            if b is None:
                continue
            w0, w1, w2 = b
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            iz = w0 / p[0, 2] + w1 / p[1, 2] + w2 / p[2, 2]; z = 1 / iz
            uu = (w0 * uv[0, 0] / p[0, 2] + w1 * uv[1, 0] / p[1, 2] + w2 * uv[2, 0] / p[2, 2]) / iz
            vv = (w0 * uv[0, 1] / p[0, 2] + w1 * uv[1, 1] / p[1, 2] + w2 * uv[2, 1] / p[2, 2]) / iz
            sub = zbuf[y0:y1 + 1, x0:x1 + 1]
            upd = inside & (z < sub)
            sub[upd] = z[upd]
            th, tw = tex.shape[:2]
            tx = np.clip((uu * (tw - 1)).astype(int), 0, tw - 1); ty = np.clip(((1 - vv) * (th - 1)).astype(int), 0, th - 1)
            img[y0:y1 + 1, x0:x1 + 1][upd] = tex[ty[upd], tx[upd]]
            nbuf[y0:y1 + 1, x0:x1 + 1][upd] = nq / np.linalg.norm(nq)
            tex_mask[y0:y1 + 1, x0:x1 + 1][upd] = True

    bg = ~np.isfinite(zbuf)
    zf = np.where(bg, 1e6, zbuf)
    dz = np.zeros_like(zf)
    dz[1:-1, 1:-1] = np.maximum.reduce([abs(zf[1:-1, 1:-1] - zf[:-2, 1:-1]), abs(zf[1:-1, 1:-1] - zf[2:, 1:-1]),
                                        abs(zf[1:-1, 1:-1] - zf[1:-1, :-2]), abs(zf[1:-1, 1:-1] - zf[1:-1, 2:])])
    dn = np.zeros(zf.shape)
    dn[1:-1, 1:-1] = np.maximum.reduce([np.linalg.norm(nbuf[1:-1, 1:-1] - nbuf[:-2, 1:-1], axis=-1),
                                        np.linalg.norm(nbuf[1:-1, 1:-1] - nbuf[1:-1, :-2], axis=-1)])
    edge = (dz > 0.012 * zf) | (dn > 0.35)
    img[edge & ~tex_mask] *= 0.45
    gy = np.linspace(0, 1, Ht)[:, None, None]
    bgc = np.array([0.93, 0.935, 0.94]) * (1 - gy) + np.array([0.80, 0.81, 0.82]) * gy
    out = np.where(bg[..., None], np.broadcast_to(bgc, img.shape), img)
    sil = Image.fromarray((~bg * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(ss * 18))
    shadow = np.roll(np.asarray(sil, float)[..., None] / 255.0, int(ss * 22), axis=0)
    out = np.where(bg[..., None], out * (1 - 0.35 * shadow), out)
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).resize(size, Image.LANCZOS)
