"""v2 on top of the make169.py output: 7" screen module in an aluminium case (170 x 105 x 17,
4 threaded holes on the back), case 5 mm taller, bezel + bottom strip 13.2 mm further forward,
round speaker grilles, slot vents, 50 mm fan over the Pi, I/O shield (RCA 3x3 + 1x3 + FM) and
two expansion-card slots in the back cover.

    python make_v2.py <folder with the make169.py STLs> <out folder>

Shell frame: x right seen from the front, y down, z towards the back (as in make169.py).
Heights are stretched by moving every vertex beyond a plane; the planes are chosen where the
parts are prismatic (old grilles are filled first), so the moved band is a clean extrusion."""
import os, sys
import numpy as np
import trimesh

SRC, DST = sys.argv[1], sys.argv[2]
os.makedirs(DST, exist_ok=True)

# ---- screen module (measured): 170 x 105 x 17, border 7 left/right, 9 top/bottom
MOD_W, MOD_H, MOD_T = 170.0, 105.0, 17.0
ACT_W, ACT_H = MOD_W - 2 * 7.0, MOD_H - 2 * 9.0     # 156 x 87 visible
WIN_W, WIN_H = ACT_W - 0.8, ACT_H - 0.8             # window hides 0.4 mm per side
# back holes seen from the front: hole edge 50 mm from the left/right side, 3.6 / 90 mm from the bottom, D5
HOLE_D = 5.0
HOLE_X = 50.0 + HOLE_D / 2                          # centre from the left (and right) side
HOLE_Y = (3.6 + HOLE_D / 2, 90.0 + HOLE_D / 2)      # centres from the bottom
SCREW_CLR = 4.4                                     # brace holes: M4 clearance (M3 + washer also works)

H_ADD = 5.0          # case taller (shell, bezel, back cover, x-brace)
Y_PLANE = 185.0      # shell y of the height stretch
Z_ADD = 13.2         # bezel window + bottom strip further forward (module 17 instead of panel 3.5)
PLATE_CUT = 2.0      # bezel window plate 6 -> 4 mm (compact)
RIM = 3.0            # raised bezel rim with a deep sloped surround (retro monitor look); strip follows it
FUNNEL_GROW = (10.0, 8.0)   # surround: window -> front opening grows this much per side (x, y)
FRONT_ADD = Z_ADD - PLATE_CUT + RIM                 # bezel front / bottom strip further forward than v1 (14.2)
XC = (-51.13 + 135.40) / 2                          # shell centre x (42.135)

def load(name):
    m = trimesh.load(os.path.join(SRC, name + '.stl')); m.merge_vertices(); return m

def clean(r):
    r.merge_vertices()
    if not r.is_volume:
        r = trimesh.util.concatenate([b for b in r.split(only_watertight=False) if abs(b.volume) > 1e-3])
        r.merge_vertices()
    return r

def union(ms): return clean(trimesh.boolean.union(ms, engine='manifold'))
def diff(a, bs): return clean(trimesh.boolean.difference([a] + list(bs), engine='manifold'))

def box(x0, x1, y0, y1, z0, z1):
    b = trimesh.creation.box(extents=[abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)])
    b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]); return b

def cyl(axis, c, r, a0, a1, sections=48):
    """cylinder along axis (0/1/2) through point c (the other two coords), from a0 to a1"""
    m = trimesh.creation.cylinder(radius=r, height=abs(a1 - a0), sections=sections)
    if axis == 0: m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    if axis == 1: m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    p = list(c); p.insert(axis, (a0 + a1) / 2)
    m.apply_translation(p); return m

def slot(axis_long, axis_thru, c, w, L, t0, t1):
    """obround slot (width w, length L along axis_long), through along axis_thru; c = centre (3 coords)"""
    lo = [0, 0, 0]; hi = [0, 0, 0]
    third = 3 - axis_long - axis_thru
    for a in range(3):
        lo[a], hi[a] = c[a], c[a]
    lo[axis_long] -= (L - w) / 2; hi[axis_long] += (L - w) / 2
    lo[third] -= w / 2; hi[third] += w / 2
    lo[axis_thru], hi[axis_thru] = t0, t1
    parts = [box(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])]
    for s in (-1, 1):
        e = list(c); e[axis_long] += s * (L - w) / 2
        e.pop(axis_thru)
        parts.append(cyl(axis_thru, e, w / 2, t0, t1, 32))
    return parts

def stretch(m, axis, at, d):
    """move every vertex with coord > at (d > 0) or < at (d < 0) by d along axis"""
    v = m.vertices.copy()
    sel = v[:, axis] > at if d > 0 else v[:, axis] < at
    v[sel, axis] += d
    r = trimesh.Trimesh(v, m.faces.copy(), process=False); r.merge_vertices(); return r

def report(name, m):
    bodies = [b for b in m.split(only_watertight=False) if abs(b.volume) > 1e-3]
    m = trimesh.util.concatenate(bodies)
    e = m.extents
    print(f'  -> {name}: {e[0]:.1f} x {e[1]:.1f} x {e[2]:.1f} mm, watertight={m.is_watertight}, '
          f'bodies={len(bodies)}, volume={m.volume / 1000:.1f} cm3')
    m.export(os.path.join(DST, name + '.stl'))

# ---- screen position (shell frame, after the stretch): centred in the bezel pocket
POCKET_Y = (-303.05 + 436.05, -201.20 + H_ADD + 436.05)      # 133.00 .. 239.85
MOD_YC = sum(POCKET_Y) / 2
MOD_BOT = MOD_YC + MOD_H / 2
MOD_X0 = XC - MOD_W / 2
SCREEN_HOLES = [(MOD_X0 + x, MOD_BOT - y) for y in HOLE_Y for x in (HOLE_X, MOD_W - HOLE_X)]
MOD_BACK_Z = 1.02 + MOD_T - 16.0 - Z_ADD                     # shell z of the module back face
print(f'module x {MOD_X0:.2f}..{MOD_X0 + MOD_W:.2f}, y {MOD_YC - MOD_H / 2:.2f}..{MOD_BOT:.2f}, back at z {MOD_BACK_Z:.2f}; '
      f'screen holes {[(round(a, 2), round(b, 2)) for a, b in SCREEN_HOLES]}')

# ======================================================================= screen bezel
# bezel frame: y_b = y_s - 436.05, z_b = z_s + 16 (rear part); the front part moves Z_ADD forward
m = load('screen-bezel')
B_Y = Y_PLANE - 436.05
m = stretch(m, 1, B_Y, H_ADD)
m = stretch(m, 2, 3.0, -Z_ADD)                     # pocket layer 1.02..5.2 becomes 1.02-Z_ADD..5.2
# window plate 6 -> 4 mm: move the front face back (no vertices between the front face and the old
# chamfer start, so this is a clean squeeze)
v = m.vertices.copy(); v[v[:, 2] < -4.98 - Z_ADD + 1.0, 2] += PLATE_CUT
m = trimesh.Trimesh(v, m.faces.copy(), process=False); m.merge_vertices()
FRONT = -4.98 - Z_ADD + PLATE_CUT
PLATE_BACK = 1.02 - Z_ADD
# close the old window, cut the new one centred on the module
wcx, wcy = XC, MOD_YC - 436.05
m = union([m, box(-40.0, 124.5, -298.0, -196.5, FRONT, PLATE_BACK)])
# raised rim: the whole front face RIM mm further forward, then one deep sloped surround from the
# window (1 mm straight at the screen) to the front opening
(bx0, by0, _), (bx1, by1, _) = m.bounds
m = union([m, box(bx0, bx1, by0, by1, FRONT - RIM, FRONT + 0.01)])
def frustum(cx, cy, w, h, z_straight, z_front, gx, gy):
    def hull(levels):
        pts = [[cx + sx * (w / 2 + a), cy + sy * (h / 2 + b), z] for z, a, b in levels for sx in (-1, 1) for sy in (-1, 1)]
        return trimesh.convex.convex_hull(np.array(pts))
    return [hull(((z_straight + 3, 0, 0), (z_straight - 0.01, 0, 0))),
            hull(((z_straight, 0, 0), (z_front, gx, gy), (z_front - 1, gx + 1, gy + 1)))]
m = diff(m, frustum(wcx, wcy, WIN_W, WIN_H, PLATE_BACK - 1.0, FRONT - RIM, *FUNNEL_GROW))
FRONT -= RIM
# front frame a bit bigger than the case (left, right, top), then a 45 deg step back to the case size
# (bottom stays: it sits on the button strip)
OVER, OVER_D = 3.0, 8.0
def lip_hull():
    lv = [(FRONT, OVER), (FRONT + OVER_D, OVER), (FRONT + OVER_D + OVER, 0.0)]
    pts = [[x, y, z] for z, g in lv for x in (bx0 - g, bx1 + g) for y in (by0 - g, by1)]
    return trimesh.convex.convex_hull(np.array(pts))
m = union([m, diff(lip_hull(), [box(bx0 + 1, bx1 - 1, by0 + 1, by1 + 1, FRONT - 1, FRONT + 40)])])
# clean pocket for the module (old guides removed), then side guides for the 170 mm module
PK_X = (-44.83, 130.17)
PK_Y = (POCKET_Y[0] - 436.05, POCKET_Y[1] - 436.05)
m = diff(m, [box(PK_X[0], PK_X[1], PK_Y[0], PK_Y[1], PLATE_BACK, 5.2)])
gl, gr = MOD_X0 - 0.3, MOD_X0 + MOD_W + 0.3
guides = []
for y0 in (wcy - 35, wcy + 15):
    guides += [box(PK_X[0] - 0.01, gl, y0, y0 + 20, PLATE_BACK, 5.0), box(gr, PK_X[1] + 0.01, y0, y0 + 20, PLATE_BACK, 5.0)]
m = union([m] + guides)
print(f'  bezel: window {WIN_W:.1f} x {WIN_H:.1f} at ({wcx:.2f}, {wcy + 436.05:.2f}); pocket '
      f'{PK_X[1] - PK_X[0]:.2f} x {PK_Y[1] - PK_Y[0]:.2f} x {5.2 - PLATE_BACK:.2f} deep (module {MOD_W} x {MOD_H} x {MOD_T})')
report('screen-bezel', m)

# ======================================================================= x-brace -> screen brace
# x-brace frame: y_x = y_s - 604.84, z_s = z_x - 8.5. Keep the corners (posts + bezel lugs), replace
# the diagonals by an open frame with two bars on the screen holes; the bottom stays open for the plugs.
m = load('x-brace')
X_Y = 604.84
m = stretch(m, 1, Y_PLANE - X_Y, H_ADD)
up_y = SCREEN_HOLES[2][1] - X_Y                    # upper holes (90 mm row)
lo_y = SCREEN_HOLES[0][1] - X_Y                    # lower holes (3.6 mm row)
m = diff(m, [box(-34.0, 118.0, up_y + 6.0, lo_y - 6.5, -3, 4)])
# pads under the lower holes (the brace edge is above them); pocket edge at that layer is y_x -367.09
pads = [box(x - 6.5, x + 6.5, lo_y - 6.6, -367.4, -2.3, 3.4) for x, _ in SCREEN_HOLES[:2]]
m = union([m] + pads)
m = diff(m, [cyl(2, (x, y - X_Y), SCREW_CLR / 2, -3, 4) for x, y in SCREEN_HOLES])
# corners: M3 countersunk from the front into heat-set inserts in the shell front posts
# (the screen covers the corners, so the heads must be flush)
def csk_front(x, y):
    c = trimesh.creation.cone(radius=3.3 + 1.0, height=3.3 + 1.0, sections=48)
    c.apply_translation([x, y, -2.3 - 1.0])                    # 6.6 wide at the front face, apex into the brace
    return [c, cyl(2, (x, y), 1.7, -3, 4)]
m = diff(m, [p for x, y in ((-41.7, -465.3), (-41.5, -376.3), (125.8, -376.2), (126.3, -465.6)) for p in csk_front(x, y)])
print(f'  screen brace: holes D{SCREW_CLR} at x {SCREEN_HOLES[0][0]:.2f}/{SCREEN_HOLES[1][0]:.2f}, '
      f'shell y {SCREEN_HOLES[2][1]:.2f}/{SCREEN_HOLES[0][1]:.2f}; open y {up_y + 6 + X_Y:.1f}..{lo_y - 6.5 + X_Y:.1f}')
report('x-brace', m)

# ======================================================================= shell (top)
m = load('top')
WALL_L, WALL_R = (-51.13, -46.13), (130.40, 135.40)
# close the old 36 mm slot grilles (y 162..198, z 27..63)
m = union([m, box(WALL_L[0] + 0.01, WALL_L[1], 158.0, 202.0, 24.0, 66.0),
              box(WALL_R[0], WALL_R[1] - 0.01, 158.0, 202.0, 24.0, 66.0)])
m = stretch(m, 1, Y_PLANE, H_ADD)
# leftovers of the old spkr-horn hooks on the inner right wall (loose bodies since make169.py) - remove both
m = diff(m, [box(119.0, WALL_R[0] - 0.01, 150.0, 170.0, 40.0, 66.0),
             box(119.0, WALL_R[0] - 0.01, 203.0, 222.0, 40.0, 66.0)])
# round speaker grilles (40 mm drivers moved up to make room for the cards): rings of round holes
SPK_Y, SPK_Z = 157.0, 45.0
SPK_HOLE, SPK_RINGS = 2.2, (4.2, 7.4, 10.6, 13.8, 17.0)
def ring_points():
    pts = [(0.0, 0.0)]
    for r in SPK_RINGS:
        n = int(2 * np.pi * r / 3.3)
        pts += [(r * np.cos(2 * np.pi * k / n + r), r * np.sin(2 * np.pi * k / n + r)) for k in range(n)]
    return pts
cut = []
for dy, dz in ring_points():
    for x0, x1 in (WALL_L, WALL_R):
        cut.append(cyl(0, (SPK_Y + dy, SPK_Z + dz), SPK_HOLE / 2, x0 - 1, x1 + 1, 16))
# slot vents low in the side walls (intake): vertical rounded slots like the reference photo
VENT_W, VENT_L = 2.6, 10.0
for yc in (203.0, 215.0):
    for zc in np.arange(8.4, 61.0, 5.2):
        for x0, x1 in (WALL_R,):                    # right wall only; the left wall has just the speaker
            cut += slot(1, 0, (0, yc, zc), VENT_W, VENT_L, x0 - 1, x1 + 1)
m = diff(m, cut)
# speaker holders (40 mm, 4 ohm 3 W: frame D40, basket D34, magnet D18, 18 deep, soft surround):
# a tube on the inside of each side wall, open at the back. 2 mm spacer ring next to the wall (only the
# metal rim touches it, the surround stays free), then the D40.4 tube 6 mm long; push the speaker in,
# a drop of glue on the rim. For printing front face down (z up) the outside is a teardrop (45 deg
# below) and the bore has a flat bridged top.
from shapely.geometry import Point, Polygon
SPK_D, SPK_SPACER, SPK_TUBE, SPK_WALL = 40.4, 2.0, 8.0, 1.8
def teardrop(r, cz, up, flat=False):
    """circle r at (SPK_Y, cz) in (y, z) + 45 deg point towards +z (up) or -z; flat: cut at the circle"""
    c = Point(SPK_Y, cz).buffer(r, 96)
    tip = (SPK_Y, cz + (1 if up else -1) * r * np.sqrt(2))
    p = c.union(Polygon(list(c.exterior.coords) + [tip]).convex_hull)
    if flat:
        p = p.intersection(Polygon([(SPK_Y - 99, cz - 99), (SPK_Y + 99, cz - 99), (SPK_Y + 99, cz + r), (SPK_Y - 99, cz + r)]))
    return p
def extrude_x(poly, x0, x1):
    """extrude a (y, z) polygon along x from x0 to x1"""
    mm = trimesh.creation.extrude_polygon(poly, abs(x1 - x0))
    mm.apply_transform(np.array([[0, 0, 1, min(x0, x1)], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float))
    return mm
spk = []
for wall_in, s in ((WALL_L[1], 1), (WALL_R[0], -1)):
    a, b, c = wall_in - s * 0.5, wall_in + s * SPK_SPACER, wall_in + s * SPK_TUBE
    outer = extrude_x(teardrop(SPK_D / 2 + SPK_WALL, SPK_Z, False), a, c)
    bores = [extrude_x(teardrop(18.25, SPK_Z, True, flat=True), wall_in, b + s * 0.01),
             extrude_x(teardrop(SPK_D / 2, SPK_Z, True, flat=True), b, c + s)]
    spk.append(diff(outer, bores))
m = union([m] + spk)
m = diff(m, [cyl(0, (SPK_Y + dy, SPK_Z + dz), SPK_HOLE / 2, x0 - 1, x1 + 1, 16)
             for dy, dz in ring_points() for x0, x1 in (WALL_L, WALL_R)])      # re-open the grille holes
# company logo engraved 0.5 mm on the left side wall, lower front corner (lower right seen from the left:
# viewer's right = -z, up = -y), 20 mm
from logo import logo_polygon
LOGO_H, LOGO_Y, LOGO_Z, LOGO_D = 20.0, 230.0, 11.0, 0.5
lp = logo_polygon(LOGO_H)
lg = trimesh.util.concatenate([trimesh.creation.extrude_polygon(g, LOGO_D + 1.0) for g in getattr(lp, 'geoms', [lp])])
lg.apply_transform(np.array([[0, 0, 1, WALL_L[0] - 1.0], [0, -1, 0, LOGO_Y], [-1, 0, 0, LOGO_Z], [0, 0, 0, 1]], float))
if lg.volume < 0:
    lg.invert()
m = diff(m, [lg])
# power supply: Mean Well 5 V 3 A, 80 x 52 x 28 (80 incl. terminals) against the roof, lengthwise
# front-to-back, terminals to the BACK, slid in from the back (cover off) along 2 rails that are part
# of this shell. The old RS-25 countersunk holes (x 13 / 68, z 44.9) and guide rails go away.
ROOF_OUT, ROOF_IN = 130.50, 134.05
m = union([m] + [cyl(1, (x, 44.9), 4.6, ROOF_OUT, ROOF_IN) for x in (13.0, 68.0)])
m = diff(m, [box(6.0, 76.0, ROOF_IN + 0.001, 137.0, 16.0, 20.0), box(6.0, 76.0, ROOF_IN + 0.001, 137.0, 70.0, 74.0)])
PSU_L, PSU_W, PSU_H = 80.0, 52.0, 28.0            # length incl. terminals, width, thickness
PSU_X0, PSU_Z0 = 4.0, -1.0                          # x 4..56 (W), z -1..79 (L): clear of the fan (x >= 62.6)
CAGE_T, CAGE_CLR = 3.0, 0.4
PSU_YB = ROOF_IN + PSU_H + CAGE_CLR                 # PSU underside (+ clearance)
BAR_Y1 = PSU_YB + CAGE_T                            # bottom of the ledge
PSU_ZF, PSU_ZB = PSU_Z0 - CAGE_CLR, PSU_Z0 + PSU_L + CAGE_CLR      # front stop face / back of the PSU
SHELL_FRONT = -4.98
# rails: 3 mm web from the roof down + 5 mm ledge under the PSU + front stop; 45 deg root gusset outside;
# diamond windows in the web (cooling, light). Every section is in the x-y plane, so with the shell
# printed front face down the rails grow straight up from the bed (no supports).
def psu_rail(side):
    T = CAGE_T
    xi = PSU_X0 - CAGE_CLR if side < 0 else PSU_X0 + PSU_W + CAGE_CLR      # PSU face
    xw = xi + side * T                                                        # outer face of the web
    z0, z1 = SHELL_FRONT, PSU_ZB - 2.0
    parts = [box(xw, xi, ROOF_IN - 0.01, BAR_Y1, z0, z1),                                  # web
             box(xw, xi - side * 5.0, PSU_YB, BAR_Y1, z0, z1),                             # ledge
             box(xw, xi - side * 5.0, PSU_YB - 8.0, BAR_Y1, z0, PSU_ZF)]                   # front stop
    pts = [(x, y, z) for z in (z0, z1) for x, y in ((xw, ROOF_IN - 0.01), (xw + side * 6.0, ROOF_IN - 0.01),
                                                     (xw, ROOF_IN + 6.0))]
    parts.append(trimesh.convex.convex_hull(np.array(pts)))                                # root gusset
    r = union(parts)
    yc, h = (ROOF_IN + 6.0 + PSU_YB - 6.0) / 2, 6.5
    win = []
    for zc in np.arange(7.0, z1 - 6.0, 14.0):
        pts = [(x, y, z) for x in (xw + side, xi - side) for y, z in ((yc - h, zc), (yc + h, zc), (yc, zc - h), (yc, zc + h))]
        win.append(trimesh.convex.convex_hull(np.array(pts)))
    return diff(r, win)
m = union([m, psu_rail(-1), psu_rail(1)])
print(f'  top: PSU rails x {PSU_X0 - CAGE_CLR - CAGE_T:.1f} / {PSU_X0 + PSU_W + CAGE_CLR + CAGE_T:.1f}, ledge y {PSU_YB:.2f}, '
      f'z {SHELL_FRONT}..{PSU_ZB - 2:.1f}; PSU x {PSU_X0}..{PSU_X0 + PSU_W} z {PSU_Z0}..{PSU_Z0 + PSU_L}')
print(f'  top: {len(ring_points())} speaker holes per side at y {SPK_Y} z {SPK_Z}; side vents right wall')
report('top', m)

# ======================================================================= back cover
# back-cover frame: y_bc = 644.59 - y_s, z_bc = 75.54 - z_s (printed outside face down)
m = load('back-cover')
BCY, BCZ = 682.90 - 38.31, 110.04 - 34.5
def bc(x0, x1, ys0, ys1, zs0, zs1):
    return box(x0, x1, BCY - ys1, BCY - ys0, BCZ - zs1, BCZ - zs0)
def bc_cyl(x, ys, r, zs0, zs1):
    return cyl(2, (x, BCY - ys), r, BCZ - zs1, BCZ - zs0)
OUT_Z, IN_Z = BCZ + 17.08, BCZ + 14.08             # shell z of the outer / inner face of the plate (92.62 / 89.62)
# close the old square-hole fan grille + its 4 screw holes (all in x 15..70, y_bc 437..493)
m = union([m, box(15.0, 70.0, 437.0, 493.0, -17.08, -14.08)])
m = stretch(m, 1, BCY - Y_PLANE, -H_ADD)
# flatten the inside (old ribs) where fan, I/O shield, card brackets and the top vents sit;
# bottom rim (y_bc < 408) untouched
m = diff(m, [box(-37.0, 113.5, 410.0, 500.0, -14.08, -9.0),
             box(-39.9, -36.0, 410.0, 500.0, -14.08, -9.0)])    # shave the left side lugs for the slot-2 card tab
# 2 fingers that keep the PSU in its rails (top corners of its back face, clear of the terminals)
m = union([m] + [bc(x0, x0 + 5.0, ROOF_IN + 2.0, ROOF_IN + 7.5, PSU_ZB + 0.3, BCZ + 14.08 + 0.01)
                 for x0 in (PSU_X0 + 1.0, PSU_X0 + PSU_W - 6.0)])

# Pi (as in make169.py, + H_ADD): PCB component side down
LIP_TOP = BCY - 410.65 + H_ADD
PI_COMP = LIP_TOP - 0.6 - 0.75 - 6.0 - 16.0
PI_BACK = PI_COMP - 1.4
PI_XC = XC + 45.5
BOARD_TOP = PI_COMP + 11.0
# 50 mm fan moved over the Pi (the RCA blocks now sit where it was), 5 mm above the Pi PCB
FAN, FAN_HOLES = 50.0, 40.0
FAN_X, FAN_Y = PI_XC, PI_BACK - 5.0 - FAN / 2
assert FAN_X + FAN / 2 < 126.0 - 4.0 and FAN_Y - FAN / 2 > 139.5 + 4.0, 'fan hits the back-cover corner screws'
cut = []
for i, dx in enumerate(np.arange(-3.5, 4.0) * 5.2):
    for j, dy in enumerate((-17.25, -5.75, 5.75, 17.25)):
        if i in (0, 7) and j in (0, 3):
            continue                                # corners: fan screws
        cut += slot(1, 2, (FAN_X + dx, BCY - (FAN_Y + dy), 0), VENT_W, 9.0, -18.5, -13.0)
cut += [bc_cyl(FAN_X + sx * FAN_HOLES / 2, FAN_Y + sy * FAN_HOLES / 2, 1.7, IN_Z - 1, OUT_Z + 1) for sx in (-1, 1) for sy in (-1, 1)]
# top vent strip (intake/exhaust above the cards and the I/O shield)
for yc in (150.5, 161.5):
    for xc in np.arange(-21.6, 50.0, 5.2):
        cut += slot(1, 2, (xc, BCY - yc, 0), VENT_W, 8.0, -18.5, -13.0)
# recess for a stick-on label above the fan grille
LABEL = (63.6, 111.6, 142.0, 160.0)
cut.append(bc(LABEL[0], LABEL[1], LABEL[2], LABEL[3], OUT_Z - 0.4, OUT_Z + 1))
# FM antenna: D9 hole for the glued fm-bracket (its D6.5 ring sits in the hole, SMA nut outside)
FM = (-32.0, 154.0)
cut.append(bc_cyl(FM[0], FM[1], 4.5, IN_Z - 1, OUT_Z + 1))

# I/O shield window: RCA 3x3 + RCA 1x3; shield pushed in from the inside like a PC I/O shield
IO_X = (-2.0, 53.5)
IO_Y = (175.0, 226.0)
cut.append(bc(IO_X[0], IO_X[1], IO_Y[0], IO_Y[1], IN_Z - 6, OUT_Z + 1))
# expansion-card slots (PC style: bracket from the inside, flange against the inner face)
SLOT_XC = (-13.0, -30.7)
SLOT_W = 15.0
SLOT_FL_X = 1.2                                     # bracket flange left/right of the window (top/bottom: FL)
SLOT_Y = (178.0, 216.0)
for xc in SLOT_XC:
    cut.append(bc(xc - SLOT_W / 2, xc + SLOT_W / 2, SLOT_Y[0], SLOT_Y[1], IN_Z - 6, OUT_Z + 1))

# snap latches: a cantilever in the plug edge of the shield / bracket with a ridge that clicks into a
# groove in the window wall (push in from the inside; push out from the outside to release)
CLR, FL, FL_T = 0.2, 2.0, 1.2
BW, SL, BUMP = 1.3, 0.8, 0.6                        # beam width, slot width, ridge height
def latch(xa, xb, y_edge, s):
    """plug edge at shell y_edge, outward direction s (-1 = up / +1 = down), beam fixed at xa, free at xb.
    returns (cuts in the part, ridge to add to the part, groove to cut in the cover)"""
    d = np.sign(xb - xa)
    inner = y_edge - s * BW
    cuts = [bc(xa, xb + d * SL, inner, inner - s * SL, IN_Z - FL_T - 1, OUT_Z + 1),          # slot behind the beam
            bc(xb, xb + d * SL, y_edge + s * 2, inner - s * SL, IN_Z - FL_T - 1, OUT_Z + 1),  # free end
            bc(xa, xb + d * SL, inner - s * SL, y_edge + s * (FL + 1), IN_Z - FL_T - 1, IN_Z)]  # flange off the beam
    def prism(x0, x1, y0, out, za, zb):
        pts = [(x, BCY - y, BCZ - z) for x in (x0, x1)
               for y, z in ((y0, za), (y0, zb), (y0 + s * out, (za + zb) / 2))]
        return trimesh.convex.convex_hull(np.array(pts))
    ridge = prism(xb - d * 4.0, xb - d * 0.3, y_edge + s * 0.2 * -1, BUMP + 0.2, IN_Z + 0.5, OUT_Z - 0.5)
    groove = prism(xb - d * 4.6, xb + d * 0.3, y_edge + s * CLR - s * 0.3, 0.3 + BUMP + 0.05, IN_Z + 0.3, OUT_Z - 0.3)
    return cuts, ridge, groove
IO_LATCHES = [latch(12.0, 26.0, IO_Y[0] + CLR, -1), latch(43.0, 29.0, IO_Y[0] + CLR, -1)]
CARD_LATCHES = lambda xc: [latch(xc - 6.0, xc + 5.0, SLOT_Y[0] + CLR, -1), latch(xc - 6.0, xc + 5.0, SLOT_Y[1] - CLR, 1)]
cut += [g for _, _, g in IO_LATCHES] + [g for xc in SLOT_XC for _, _, g in CARD_LATCHES(xc)]
M3_CLR = 3.4
# back-cover screws (into M3 heat-set inserts in the shell posts): D3.4 through
BC_SCREWS = [(-41.6, 505.3), (-41.5, 411.2), (125.8, 411.2), (126.3, 505.5)]       # back-cover frame (x, y_bc)
cut += [cyl(2, p, M3_CLR / 2, -20, 0) for p in BC_SCREWS]

# engraved labels (0.6 mm) on the outside, readable from the back (viewer's right = -x)
from matplotlib.textpath import TextPath
from matplotlib.font_manager import FontProperties
from shapely.geometry import Polygon
from shapely import affinity
def text_poly(s, h):
    tp = TextPath((0, 0), s, size=10, prop=FontProperties(family='DejaVu Sans', weight='bold'))
    p = Polygon()
    for ring in tp.to_polygons():
        if len(ring) >= 3:
            p = p.symmetric_difference(Polygon(ring).buffer(0))
    x0, y0, x1, y1 = p.bounds
    p = affinity.scale(p, h / (y1 - y0), h / (y1 - y0), origin=(x0, y0))
    x0, y0, x1, y1 = p.bounds
    return affinity.translate(p, -(x0 + x1) / 2, -(y0 + y1) / 2)
def engrave_bc(text, x, ys, h=3.5, depth=0.6, face_z=OUT_Z):
    """cutter in the back-cover frame: text centred at shell (x, ys) on the face at shell z face_z"""
    ms = [trimesh.creation.extrude_polygon(g, depth + 1) for g in getattr(text_poly(text, h), 'geoms', [text_poly(text, h)])]
    t = trimesh.util.concatenate(ms)
    t.apply_transform(np.array([[-1, 0, 0, x], [0, 1, 0, BCY - ys], [0, 0, -1, BCZ - face_z + depth], [0, 0, 0, 1]], float))
    return t                                    # two mirrors (x, z): orientation stays valid
LBL_Y = PI_COMP - 0.5 - 4.6                         # between the fan grille and the Pi port windows
cut += [engrave_bc('ETH', 69.9, LBL_Y), engrave_bc('USB', 88.6, LBL_Y), engrave_bc('USB', 106.6, LBL_Y),
        engrave_bc('EXP1', SLOT_XC[0], SLOT_Y[0] - 6.0), engrave_bc('EXP2', SLOT_XC[1], SLOT_Y[0] - 6.0),
        engrave_bc('FM', FM[0], FM[1] + 9.0)]
m = diff(m, cut)
print(f'  back-cover: fan centre x {FAN_X:.2f} y {FAN_Y:.2f} (shell); I/O window x {IO_X} y {IO_Y}; '
      f'card slots x {SLOT_XC} (w {SLOT_W}) y {SLOT_Y}; board top y {BOARD_TOP:.2f}')
report('back-cover', m)

# ---- I/O shield (printed flat, outside face down): plug 0.2 smaller than the window, 1.2 mm flange inside,
# 2 snap latches on the top edge (bottom edge in first, push the top until it clicks); glue the flange
RCA_HOLE = 9.0                                     # 8.4 mm RCA sleeve
RCA_PITCH_X = 13.0
RCA_ROWS = [BOARD_TOP - h for h in (7.5, 20.0, 32.5)]   # jack centres above the main board (check your block!)
RCA3_X = [33.6 + k * RCA_PITCH_X for k in (1, 0, -1)]  # AV1, AV2, AV3 from the left seen from the back
RCA1_X = [5.6]                                          # OUT
def io_shield():
    s = union([bc(IO_X[0] + CLR, IO_X[1] - CLR, IO_Y[0] + CLR, IO_Y[1] - CLR, IN_Z, OUT_Z),
               bc(IO_X[0] - FL, IO_X[1] + FL, IO_Y[0] - FL, IO_Y[1] + FL, IN_Z - FL_T, IN_Z)])
    s = diff(s, [c for cuts, _, _ in IO_LATCHES for c in cuts])
    s = union([s] + [r for _, r, _ in IO_LATCHES])
    holes = [bc_cyl(x, y, RCA_HOLE / 2, IN_Z - 2, OUT_Z + 1) for x in RCA3_X + RCA1_X for y in RCA_ROWS]
    lbl_y = RCA_ROWS[2] - RCA_HOLE / 2 - 4.0
    labels = [engrave_bc(t, x, lbl_y, h=3.2) for t, x in zip(('AV1', 'AV2', 'AV3', 'OUT'), RCA3_X + RCA1_X)]
    return diff(s, holes + labels)
s = io_shield()
s.apply_translation([0, 0, 17.08])                 # outside face on z=0
report('io-shield', s)

# ---- FM antenna bracket (glue it on the inside of the back cover): D-hole for an SMA bulkhead
# (D6.5, flat 5.9) + ring that centres it in the D9 cover hole; SMA nut on the outside
def fm_bracket():
    b = union([box(-7, 7, -7, 7, 0, 2.5), cyl(2, (0, 0), 4.35, 2.49, 4.5)])
    hole = diff(cyl(2, (0, 0), 3.25, -1, 6), [box(-5, 5, 2.95, 5, -2, 7)])
    return diff(b, [hole])
report('fm-bracket', fm_bracket())

# ---- card bracket (blank; drill/cut your connectors): plug + flange + L-tab with 2x M3 into the card,
# snap latches top + bottom; glue the flange
CARD_BOTTOM = BOARD_TOP - 8.0                      # card edge above the main board (2.54 right-angle header into 8.5 mm female)
CARD_TOP = CARD_BOTTOM - 40.0
CARD_LEN = 58.0
def card_bracket(xc):
    pcb_x = xc - 6.7                               # card PCB left face; component side faces +x, into the slot
    parts = [bc(xc - SLOT_W / 2 + CLR, xc + SLOT_W / 2 - CLR, SLOT_Y[0] + CLR, SLOT_Y[1] - CLR, IN_Z, OUT_Z),
             bc(xc - SLOT_W / 2 - SLOT_FL_X, xc + SLOT_W / 2 + SLOT_FL_X, SLOT_Y[0] - FL, SLOT_Y[1] + FL, IN_Z - FL_T, IN_Z),
             bc(pcb_x - 2.0, pcb_x, SLOT_Y[0] + 4.0, SLOT_Y[1] - 4.0, IN_Z - FL_T - 10.0, IN_Z - FL_T + 0.01)]
    b = union(parts)
    b = diff(b, [c for cuts, _, _ in CARD_LATCHES(xc) for c in cuts])
    b = union([b] + [r for _, r, _ in CARD_LATCHES(xc)])
    return diff(b, [cyl(0, (BCY - y, BCZ - (IN_Z - FL_T - 5.0)), M3_CLR / 2, pcb_x - 3, pcb_x + 1) for y in (SLOT_Y[0] + 8, SLOT_Y[1] - 8)])
b = card_bracket(SLOT_XC[0])
b.apply_translation([-SLOT_XC[0], 0, 17.08])
report('card-bracket', b)
print(f'  cards: PCB {CARD_BOTTOM - CARD_TOP:.0f} high (y {CARD_TOP:.2f}..{CARD_BOTTOM:.2f}), {CARD_LEN:.0f} long (z {IN_Z - FL_T - CARD_LEN:.2f}..{IN_Z - FL_T:.2f}), '
      f'PCB left face at x {[round(x - 6.7, 2) for x in SLOT_XC]}')

# ======================================================================= bottom tray
# tray frame: shell z = y_t + 38.51; its front (y_t < -40, posts for the strip) moves Z_ADD forward
m = load('bottom')
m = stretch(m, 1, -40.0, -FRONT_ADD)
# "AC250V" engraved on the recessed back face (y_t 46.53) next to the C8 inlet; seen from the back:
# right = -x, up = +z_t
t = trimesh.util.concatenate([trimesh.creation.extrude_polygon(g, 1.6) for g in
                              getattr(text_poly('AC250V', 3.5), 'geoms', [text_poly('AC250V', 3.5)])])
t.apply_transform(np.array([[-1, 0, 0, 12.0], [0, 0, 1, 46.53 - 0.6], [0, 1, 0, -13.0], [0, 0, 0, 1]], float))
m = diff(m, [t])
report('bottom', m)

# ======================================================================= front strip v3
# grille (DHT22), headphone jack + symbol, buttons A B < > + - with labels, IR, LED, PWR (frontpanel.py);
# the strip itself moves FRONT_ADD forward with the tray front (assembly only)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frontpanel
m = frontpanel.plate(load('front-plate'))
report('front-plate', m)
frontpanel.caps().export(os.path.join(DST, 'buttons.stl'))

for name in ('back-clips',):                        # usb-c-brace not used (power: C8 inlet + internal PSU)
    load(name).export(os.path.join(DST, name + '.stl'))
