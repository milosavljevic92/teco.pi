"""Fit check of the 16:9 remix. Sections of parts are moved into a common frame and
the part that must slide into another is subtracted from the receiving opening."""
import sys, numpy as np, trimesh
from shapely.affinity import affine_transform
from shapely.ops import unary_union

D = 38.31
DZ = 34.5            # depth removed (shell z 67.51..102.01, tray y 29.0..63.5)
OUT = sys.argv[1]

def sec(name, z):
    m = trimesh.load(f'{OUT}\\{name}.stl')
    s = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    p, _ = s.to_2D(to_2D=np.eye(4))
    return unary_union(p.polygons_full)

def opening(poly):
    """largest interior ring of a frame-like section as a polygon"""
    from shapely.geometry import Polygon
    rings = [Polygon(r) for g in getattr(poly, 'geoms', [poly]) for r in g.interiors]
    return max(rings, key=lambda r: r.area)

def move(poly, dy=0.0, flip_c=None):
    if flip_c is not None:   # y -> flip_c - y
        return affine_transform(poly, [1, 0, 0, -1, 0, flip_c])
    return affine_transform(poly, [1, 0, 0, 1, 0, dy])

def check(label, part, hole):
    over = part.difference(hole).area
    gap = hole.difference(part)
    print(f'{label}: part outside opening {over:.2f} mm2  ->  {"OK" if over < 0.5 else "COLLISION"}'
          f'   (part bounds {np.round(part.bounds, 2).tolist()}, opening {np.round(hole.bounds, 2).tolist()})')

# x-brace sits in the bezel pocket (z 5..11 of the bezel), notches + corner lug recesses
pocket = opening(sec('screen-bezel', 8.0))
xb = move(sec('x-brace', 0.5), dy=207.10 - D)
check('x-brace in bezel pocket', xb, pocket)

# panel 164.9 x 100 in the pocket in front of the x-brace (bezel z 1..5, side guides included)
from shapely.geometry import box
pocket_front = opening(sec('screen-bezel', 3.0))
win = opening(sec('screen-bezel', -0.5))
pcx = (win.bounds[0] + win.bounds[2]) / 2
pb = pocket_front.bounds
print(f'window {win.bounds[2]-win.bounds[0]:.2f} x {win.bounds[3]-win.bounds[1]:.2f}; pocket in front of x-brace '
      f'{pb[2]-pb[0]:.2f} x {pb[3]-pb[1]:.2f} (y {pb[1]:.2f}..{pb[3]:.2f}), window y {win.bounds[1]:.2f}..{win.bounds[3]:.2f}')
for top_border in (4.66, 9.42):   # panel with the wide (FPC) border at the top or at the bottom
    # bezel y=-305.6 side is the top of the TV; active area 85.92 high is centred in the window
    wcy = (win.bounds[1] + win.bounds[3]) / 2
    panel_top = wcy - 85.92 / 2 - top_border
    panel = box(pcx - 164.9 / 2, panel_top, pcx + 164.9 / 2, panel_top + 100)
    check(f'panel (top border {top_border} mm)', panel, pocket_front)

# back cover in the rear rebate of the shell (printed upside down: y flipped)
shell_back = sec('top', 126.0 - DZ)                 # U shape, open at the bottom
rebate = shell_back.convex_hull.difference(shell_back)
bc = move(sec('back-cover', -15.0), flip_c=682.90 - D)
check('back-cover in shell rebate', bc, rebate)

# bezel / shell outlines and screw positions
sh = sec('top', 0.0); bz = move(sec('screen-bezel', 10.9), dy=436.05)
print(f'shell outline {np.round(sh.bounds, 2).tolist()}  bezel outline {np.round(bz.bounds, 2).tolist()}')
holes_sh = sorted((round(c[0], 1), round(c[1], 1)) for c in
                  [np.array(r.coords).mean(0) for g in getattr(sh, 'geoms', [sh]) for r in g.interiors if trimesh.path.polygons.Polygon(r).area < 20])
xbp = sec('x-brace', 0.5)
holes_xb = sorted((round(c[0], 1), round(c[1] + 643.15 - D, 1)) for c in
                  [np.array(r.coords).mean(0) for g in getattr(xbp, 'geoms', [xbp]) for r in g.interiors if trimesh.path.polygons.Polygon(r).area > 10 and trimesh.path.polygons.Polygon(r).area < 20])
print('shell front posts', holes_sh)
print('x-brace corners  ', holes_xb)

# bottom tray under the shell: tray screw holes (tray y -> shell z = y + 38.51) vs shell lip holes
def circles(poly, rmax):
    out = []
    for g in getattr(poly, 'geoms', [poly]):
        for r in g.interiors:
            c = np.array(r.coords); ctr = c.mean(0)
            if np.linalg.norm(c - ctr, axis=1).mean() < rmax:
                out.append(ctr)
    return sorted((round(float(a), 1), round(float(b), 1)) for a, b in out)
lip = trimesh.load(f'{OUT}\\top.stl').section(plane_origin=[0, 237.0, 0], plane_normal=[0, 1, 0])
lp, _ = lip.to_2D(to_2D=np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 237.0], [0, 0, 0, 1]], float))
tr = sec('bottom', -20.0)
print('shell lip holes (x, z)', circles(unary_union(lp.polygons_full), 3))
print('tray holes      (x, z)', [(x, round(y + 38.51, 1)) for x, y in circles(tr, 3.2)])
bt = trimesh.load(f'{OUT}\\bottom.stl')
sh_depth = trimesh.load(f'{OUT}\\top.stl').extents[2]
print(f'tray {bt.extents[0]:.2f} x {bt.extents[1]:.2f} vs shell inner width {130.40 + 46.13:.2f}, shell depth {sh_depth:.2f}')

# Raspberry Pi 3B (85 x 56 x 1.4 + ports 16 high) and RS-25 PSU vs shell / back cover
import trimesh.creation as tc
def tbox(x0, x1, y0, y1, z0, z1):
    b = tc.box(extents=[x1 - x0, y1 - y0, z1 - z0]); b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]); return b
# Pi upside down (header into the main board below): PCB on top, ports hang below it
pcb_comp = (682.90 - D - 410.65) - 0.6 - 0.75 - 6.0 - 16.0     # component side, facing down
pcb_back = pcb_comp - 1.4
edge = 124.0 - DZ
xc = (-51.13 + 135.40) / 2 + 45.5   # PI_SHIFT
px = lambda y: xc - 28.0 + y
pi = trimesh.util.concatenate([
    tbox(px(0), px(56), pcb_back, pcb_comp, edge - 85 - 2.6, edge),                     # PCB (+ SD card at the front)
    tbox(px(2.25), px(18.25), pcb_comp, pcb_comp + 13.5, edge - 21.0, edge + 2.5),      # Ethernet
    tbox(px(22.4), px(35.6), pcb_comp, pcb_comp + 16.0, edge - 17.0, edge + 2.1),       # USB
    tbox(px(40.4), px(53.6), pcb_comp, pcb_comp + 16.0, edge - 17.0, edge + 2.1)])      # USB
fan = tbox(42.13 - 25, 42.13 + 25, pcb_back - 5 - 50, pcb_back - 5, edge - 15.0, edge)  # 50x50x15 fan inside the cover
psu = tbox(2.0, 80.0 + 14.0, 134.06, 134.06 + 28.0, 19.5, 70.5)
shell = trimesh.load(f'{OUT}\\top.stl')
bcm = trimesh.load(f'{OUT}\\back-cover.stl')
bcm.apply_transform(np.array([[1, 0, 0, 0], [0, -1, 0, 682.90 - D], [0, 0, -1, 110.04 - DZ], [0, 0, 0, 1]], float))
for n, a in (('Pi', pi), ('PSU', psu), ('fan', fan)):
    for o, b in (('shell', shell), ('back cover', bcm), ('PSU', psu), ('Pi', pi)):
        if n == o:
            continue
        i = trimesh.boolean.intersection([a, b], engine='manifold')
        v = i.volume if len(i.faces) else 0.0
        print(f'{n} vs {o}: {v:.2f} mm3 -> {"OK" if v < 0.5 else "COLLISION"}')
print(f'Pi front (SD card) at shell z {edge - 85 - 2.6:.1f} (shell front face -4.98); back cover z {bcm.bounds[0][2]:.2f}..{bcm.bounds[1][2]:.2f}')
print(f'Pi PCB y {pcb_back:.1f}..{pcb_comp:.1f}, ports to y {pcb_comp + 16:.1f}; fan y {pcb_back - 55:.1f}..{pcb_back - 5:.1f}; PSU bottom y {134.06 + 28:.1f}')

