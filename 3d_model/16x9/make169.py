"""Mini CRT (Xipher Design, CC BY-NC 4.0) -> 7" 16:9 (1024x600) remix.
Height is reduced by cutting a horizontal band out of each tall part and joining
the halves, so screw holes/clips keep their shape."""
import os, sys
import numpy as np
import trimesh
from trimesh.intersections import slice_mesh_plane

SRC = sys.argv[1]
DST = sys.argv[2]
os.makedirs(DST, exist_ok=True)

# 7" 50-pin IPS panel (164.9 x 100 x 3.5, active 154.21 x 85.92)
ACTIVE_W, ACTIVE_H = 154.21, 85.92
WIN_OVERLAP = 0.3                      # window hides 0.3 mm of active area per side
WIN_W = ACTIVE_W - 2 * WIN_OVERLAP     # 153.61
WIN_H = ACTIVE_H - 2 * WIN_OVERLAP     # 85.32
PANEL_W, PANEL_H = 164.9, 100.0

ORIG_WIN_H = 292.10 - 168.47           # straight part of original window: 123.63
D = ORIG_WIN_H - WIN_H                 # height removed from every tall part (~38.3)

def load(name):
    m = trimesh.load(os.path.join(SRC, name + '.stl'))
    m.merge_vertices()
    return m

def unit(axis):
    v = [0.0, 0.0, 0.0]; v[axis] = 1.0
    return np.array(v)

def half(m, y, keep_below, axis=1):
    n = -unit(axis) if keep_below else unit(axis)
    return slice_mesh_plane(m, plane_normal=n, plane_origin=unit(axis) * y, cap=True)

def section_sig(m, y, axis=1):
    s = m.section(plane_origin=unit(axis) * y, plane_normal=unit(axis))
    a, b = [i for i in range(3) if i != axis]
    T = np.zeros((4, 4)); T[0, a] = 1; T[1, b] = 1; T[2, axis] = 1; T[2, 3] = -y; T[3, 3] = 1
    p, _ = s.to_2D(to_2D=T)
    return round(p.area, 2), np.round(p.bounds, 2).tolist(), len(p.polygons_full)

def cut_band(m, y1, y2, name, axis=1):
    """remove the slab y1..y2 along axis and close the gap; both cut sections must be equal"""
    a, b = section_sig(m, y1, axis), section_sig(m, y2, axis)
    print(f'  {name}: band {"xyz"[axis]} {y1:.2f}..{y2:.2f}  section@1 {a}  section@2 {b}  {"MATCH" if a == b else "DIFF"}')
    lo = half(m, y1, True, axis)
    hi = half(m, y2, False, axis)
    hi.apply_translation(-unit(axis) * (y2 - y1))
    return union([lo, hi])

def clean(r):
    r.merge_vertices()
    if not r.is_volume:   # manifold sometimes leaves zero-volume slivers next to coplanar faces
        r = trimesh.util.concatenate([b for b in r.split(only_watertight=False) if abs(b.volume) > 1e-3])
        r.merge_vertices()
    return r

def union(ms):
    return clean(trimesh.boolean.union(ms, engine='manifold'))

def diff(a, bs):
    return clean(trimesh.boolean.difference([a] + list(bs), engine='manifold'))

def box(x0, x1, y0, y1, z0, z1):
    b = trimesh.creation.box(extents=[x1 - x0, y1 - y0, z1 - z0])
    b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2])
    return b

def cyl(x, y, r, z0, z1):
    c = trimesh.creation.cylinder(radius=r, height=z1 - z0, sections=64)
    c.apply_translation([x, y, (z0 + z1) / 2])
    return c

def frustum(cx, cy, w, h, z_straight, z_front, grow):
    """window cutters: straight w x h prism from z_straight back, 45deg chamfer to z_front
    (two convex pieces, the combined shape is not convex)."""
    def hull(levels):
        pts = [[cx + sx * (w / 2 + g), cy + sy * (h / 2 + g), z]
               for z, g in levels for sx in (-1, 1) for sy in (-1, 1)]
        return trimesh.convex.convex_hull(np.array(pts))
    return [hull(((z_straight + 3, 0), (z_straight - 0.01, 0))),
            hull(((z_straight, 0), (z_front, grow), (z_front - 1, grow + 1)))]

def sharp_corners(m, r=4.1):
    """Sony PVM look: fill the r=4 fillets on the four outer corners of the front outline."""
    (x0, y0, z0), (x1, y1, z1) = m.bounds
    return union([m] + [box(a, a + r, b, b + r, z0, z1) for a in (x0, x1 - r) for b in (y0, y1 - r)])

def report(name, m):
    bodies = [b for b in m.split(only_watertight=False) if abs(b.volume) > 1e-3]  # drop zero-volume slivers
    m = trimesh.util.concatenate(bodies)
    e = m.extents
    print(f'  -> {name}: {e[0]:.1f} x {e[1]:.1f} x {e[2]:.1f} mm, watertight={m.is_watertight}, volume={m.volume/1000:.1f} cm3')
    m.export(os.path.join(DST, name + '.stl'))

print(f'window {WIN_W:.2f} x {WIN_H:.2f}, height reduced by {D:.2f} mm')

# ---- screen bezel: band through the middle of the window, then narrow the window
m = load('screen-bezel')
wc = (-292.10 + -168.47) / 2           # window centre (y)
m = cut_band(m, wc - D / 2, wc + D / 2, 'screen-bezel')
win_cx = (-37.63 + 122.00) / 2
win_cy = wc - D / 2                    # upper half moved down by D
fill = box(-37.63 - 4.2, 122.00 + 4.2, win_cy - WIN_H / 2 - 4.2, win_cy + WIN_H / 2 + 4.2, -4.98, 1.02)
m = union([m, fill])
m = diff(m, frustum(win_cx, win_cy, WIN_W, WIN_H, -1.0, -4.98, 3.98))
# after the cut the panel layer of the pocket (z 1.02..5.2, in front of the x-brace) is
# only 98.6 mm high (rim y -302.0 .. -203.41); widen it to -303.05 .. -201.20 for the 100 mm panel
m = diff(m, [box(-44.83, 130.17, -303.05, -302.0 + 0.01, 1.0, 5.2),
             box(-44.83, 130.17, -165.10 - D - 0.01, -201.20, 1.0, 5.2)])
# panel side guides inside the pocket (panel is narrower than the 175 mm pocket)
gap_l = win_cx - PANEL_W / 2 - 0.3     # guide inner faces leave 0.3 mm clearance
gap_r = win_cx + PANEL_W / 2 + 0.3
gy0, gy1 = win_cy - 15, win_cy + 15
m = union([m, box(-44.83 - 0.01, gap_l, gy0, gy1, 1.0, 5.0), box(gap_r, 130.17 + 0.01, gy0, gy1, 1.0, 5.0)])
m = sharp_corners(m)
report('screen-bezel', m)

# ---- top shell: side vent rows repeat every ~7 mm; plain wall between rows at
# y 190.3..228.9 (section scan), so the band starts in one gap and ends in another
m = load('top')
assert D < 228.9 - 190.3 - 0.2
m = cut_band(m, 190.45, 190.45 + D, 'top')
# speaker grilles on both side walls: close the old 75 mm right grille (cut by the
# band) and the left vent slots, then cut identical 2-2.5" grilles in the
# original style (3.9 mm square holes, 7.1 mm pitch)
WALL_L, WALL_R = (-51.13, -46.13), (130.40, 135.40)
m = union([m, box(WALL_L[0] + 0.01, WALL_L[1], 137.0, 179.5, 9.5, 74.0),
              box(WALL_R[0], WALL_R[1] - 0.01, 163.5, 203.5, 18.5, 96.5)])
# depth: only as deep as the Raspberry Pi needs (85 mm + ports at the back cover).
# Band between the spkr-horn hooks (z<=64.7) and the rear tray screw holes in the bottom
# lips (z 105.9, r 2); the walls there are plain after the fills above.
# The tray (shell z = tray y + 38.51) is plain only for y 28.75..63.6 -> band tray y 29.0..63.5.
DZ, DZ0 = 34.5, 29.0 + 38.51
if os.environ.get('DUMP_TOP'):
    m.export(os.path.join(DST, '_top_before_depth.stl'))
m = cut_band(m, DZ0, DZ0 + DZ, 'top', axis=2)
# 40 mm (1.5") speakers: D36 slot grille on both side walls (same style as the back fan grille:
# vertical 3.5 mm slots at 7.25 mm, 4 mm centre bar)
SPK_Y, SPK_Z, SPK_OPEN = 180.0, 45.0, 36.0
SLOT_W, SLOT_P, BAR = 3.5, 7.25, 4.0
cutters = []
for k in range(-2, 3):
    dz = k * SLOT_P
    h = np.sqrt(max(0.0, (SPK_OPEN / 2) ** 2 - (abs(dz) + SLOT_W / 2) ** 2))
    for y0, y1 in ((SPK_Y - h, SPK_Y - BAR / 2), (SPK_Y + BAR / 2, SPK_Y + h)):
        for x0, x1 in (WALL_L, WALL_R):
            cutters.append(box(x0 - 1, x1 + 1, y0, y1, SPK_Z + dz - SLOT_W / 2, SPK_Z + dz + SLOT_W / 2))
# old spkr-horn hooks on the inner right wall no longer match and cover grille holes
cutters += [box(123.5, WALL_R[0] - 0.01, 155.0, 163.0, 44.5, 64.7),
            box(123.5, WALL_R[0] - 0.01, 203.8, 211.8, 44.5, 64.7)]
m = diff(m, cutters)
# Mean Well RS-25-5 (78 x 51 x 28, terminals +14) under the roof, base plate up, mesh down.
# Base has 2x M3 (max 3 mm deep), 55 mm apart, 12 mm from the terminal end, 25.4 mm from one
# long edge. Lying across the case (the shallow case is too short for 78 + 14 + wires),
# terminals to the right; above y 148 the right speaker is not in the way of the wires.
ROOF_OUT, ROOF_IN = 130.50, 134.05
PSU_W, PSU_L = 51.0, 78.0
PSU_X1 = 80.0                                   # terminal end of the body (terminals x 80..94)
PSU_Z0 = 19.5                                   # front long edge of the body
psu_x0 = PSU_X1 - PSU_L
hz = PSU_Z0 + 25.4
hxs = [PSU_X1 - 12.0, PSU_X1 - 12.0 - 55.0]
def csk(x, z):       # M3 countersunk from the top: 3.4 through, 90 deg cone to 6.6 at the surface
    c = trimesh.creation.cone(radius=6.6 / 2 + 1.0, height=6.6 / 2 + 1.0, sections=64)
    c.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))    # apex points +y (into the roof)
    c.apply_translation([x, ROOF_OUT - 1.0, z])                                          # 6.6 wide at the outer surface
    s = trimesh.creation.cylinder(radius=1.7, height=10, sections=48)
    s.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    s.apply_translation([x, ROOF_IN, z])
    return [c, s]
m = diff(m, [p for x in hxs for p in csk(x, hz)])
# 2 mm guide rails along the long sides of the PSU base
m = union([m, box(psu_x0 + 5, PSU_X1 - 5, ROOF_IN - 0.01, ROOF_IN + 2.0, PSU_Z0 - 2.3, PSU_Z0 - 0.3),
              box(psu_x0 + 5, PSU_X1 - 5, ROOF_IN - 0.01, ROOF_IN + 2.0, PSU_Z0 + PSU_W + 0.3, PSU_Z0 + PSU_W + 2.3)])
print(f'  top: RS-25 holes x={hxs} z={hz:.2f}, body x {psu_x0:.2f}..{PSU_X1} z {PSU_Z0}..{PSU_Z0 + PSU_W}')
m = sharp_corners(m)
print(f'  top: speaker grilles {(len(cutters) - 2) // 2} holes per side, centre y={SPK_Y} z={SPK_Z}')
report('top', m)

# ---- back cover: band centred on the grille, then put the centre bar back
m0 = load('back-cover')
c = 480.01
m = cut_band(m0, c - D / 2, c + D / 2, 'back-cover')
bar = m0.slice_plane([0, 477.51 - 0.0, 0], [0, 1, 0], cap=True).slice_plane([0, 482.51, 0], [0, -1, 0], cap=True)
bar.apply_translation([0, (c - D / 2) - 480.01, 0])
m = union([m, bar])
# Raspberry Pi 3B upside down: its GPIO header plugs down into the user's main board, so USB +
# Ethernet hang below the PCB and appear mirrored on the back cover.
# Shell frame -> back-cover frame: x same, y_bc = BC_Y - y_s, z_bc = 110.04 - DZ - z_s (printed upside down)
BC_Y = 682.90 - D
LIP_TOP = BC_Y - 410.65             # shell y of the top of the back-cover bottom lip (lip reaches in to z 80.5)
CLR = 0.75
PORT_RAISE = 6.0                    # ports this much higher than the lowest possible (lip + 0.6 mm)
PORT_LOW = LIP_TOP - 0.6 - CLR - PORT_RAISE
pcb_comp = PORT_LOW - 16.0          # PCB component side (facing down), USB is 16 mm tall
pcb_back = pcb_comp - 1.4           # PCB bottom side (facing up)
PI_SHIFT = 45.5                     # towards +x = left seen from the back, as far as the back-clips (14 mm from the wall) allow
PI_XC = (-51.13 + 135.40) / 2 + PI_SHIFT
def pi_x(y_pi):                     # Pi y (0..56, Ethernet side = 0) -> shell x, component side DOWN, ports to the back
    return PI_XC - 28.0 + y_pi
PORTS = [(2.25, 18.25, 13.5), (22.4, 35.6, 16.0), (40.4, 53.6, 16.0)]   # Ethernet, USB, USB: (y0, y1, height)
def bc_box(xs0, xs1, ys0, ys1, zb0, zb1):
    return box(min(xs0, xs1), max(xs0, xs1), BC_Y - max(ys0, ys1), BC_Y - min(ys0, ys1), zb0, zb1)
# plate is 3 mm (z_bc -17.08..-14.08). Close the old round grille and fan holes, then a grille for a
# standard 50 mm fan (mounted inside): D46 opening with vertical slots, 4x M3 at 40 mm.
OLD_FAN = [(-11.74, 427.43), (93.32, 427.48), (-11.74, 532.60 - D), (93.32, 532.58 - D)]
m = union([m, box(-15.0, 97.0, 413.0, 504.0, -17.08, -14.08)] +
             [cyl(x, y, 5.5, -17.08, -14.08) for x, y in OLD_FAN])   # holes + their shallow outer counterbores
# the old slats (4 mm) and the inner rib below the grille (to z_bc -10.19) stick out of the 3 mm
# plate; flatten the inside where the fan and the Pi PCB edge sit (bottom rim / lip untouched)
m = diff(m, [box(-15.0, 95.0, 417.0, 504.0, -14.08, -9.0)])
# ... and behind the Pi PCB edge, wherever the Pi sits left-right (bottom rim below y_bc 417 untouched)
m = diff(m, [box(PI_XC - 29.0, PI_XC + 29.0, 417.0, BC_Y - (pcb_back - 1.0) + 2.0, -14.08, -9.0)])
FAN, FAN_HOLES, FAN_OPEN = 50.0, 40.0, 46.0
G_CX = (-46.63 + 130.90) / 2
G_CY = BC_Y - (pcb_back - 1.0) + 4.0 + FAN / 2        # fan bottom 4 mm above the Pi PCB
# square holes (3.9 mm, 7.1 mm pitch) inside the D46 opening, same style the speakers had before
PITCH, HOLE = 7.1, 3.9
slots = []
for i in range(-4, 4):
    for j in range(-4, 4):
        dx, dy = (i + 0.5) * PITCH, (j + 0.5) * PITCH
        if np.hypot(abs(dx) + HOLE / 2, abs(dy) + HOLE / 2) <= FAN_OPEN / 2 + 0.6:
            slots.append(box(G_CX + dx - HOLE / 2, G_CX + dx + HOLE / 2, G_CY + dy - HOLE / 2, G_CY + dy + HOLE / 2, -18.5, -13.0))
slots += [cyl(G_CX + sx * FAN_HOLES / 2, G_CY + sy * FAN_HOLES / 2, 1.7, -18.5, -13.0) for sx in (-1, 1) for sy in (-1, 1)]
m = diff(m, slots)
# port windows (from the PCB down) through plate, inner rib and thick bottom rim; PCB edge at shell
# z 124.0-DZ (plate inner face 124.12-DZ), ports overhang 2.1..2.5 mm -> 0.6..1 mm behind the outer face
cut = [bc_box(pi_x(y0) - CLR, pi_x(y1) + CLR, pcb_comp - 0.5, pcb_comp + h + CLR, -18.5, -9.0) for y0, y1, h in PORTS]
m = diff(m, cut)
assert G_CY + FAN / 2 < 505.2 - 3.5, 'fan hits the top corner screws'
print(f'  back-cover: Pi 3B upside down, PCB y {pcb_back:.2f}..{pcb_comp:.2f}, ports down to y {PORT_LOW:.2f}, '
      f'Pi x {pi_x(0):.1f}..{pi_x(56):.1f}; 50 mm fan centre y_bc {G_CY:.1f} (shell y {BC_Y - G_CY:.1f})')
report('back-cover', m)

# ---- bottom tray: same depth band (shell z = tray y + 38.51); its shell screw holes at
# y -29.5 / 67.4 stay outside the band and end up 61.9 mm apart like the shell lip holes
m = load('bottom')
m = cut_band(m, DZ0 - 38.51, DZ0 + DZ - 38.51, 'bottom', axis=1)
# back wall (after the cut: sides y 47.03..52.53, recessed centre y 42.03..46.53):
# close the 5 RCA holes and the small oval slot, add a C8 (figure-8) mains inlet on the
# right as seen from the back: panel cut-out obround 20.3 x 12.5, 2x D3.5 at 28 mm, flange 35 x 15
m = union([m, box(1.0, 79.0, 42.03, 46.53, -19.0, -6.5), box(-26.8, -22.2, 47.03, 52.53, -19.5, -9.5)])
C8_X, C8_Z, C8_W, C8_H = -27.5, -15.0, 20.3 + 0.3, 12.5 + 0.3
r = C8_H / 2
def ybar(x, z, rad, y0, y1):   # cylinder along y
    c = trimesh.creation.cylinder(radius=rad, height=y1 - y0, sections=64)
    c.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    c.apply_translation([x, (y0 + y1) / 2, z]); return c
c8 = [box(C8_X - C8_W / 2 + r, C8_X + C8_W / 2 - r, 45.0, 60.0, C8_Z - r, C8_Z + r),
      ybar(C8_X - C8_W / 2 + r, C8_Z, r, 45.0, 60.0), ybar(C8_X + C8_W / 2 - r, C8_Z, r, 45.0, 60.0),
      # left screw goes into the solid corner block: blind 12.5 mm (M3 heat-set insert or self-tapping),
      # stops 4 mm before the vertical tray screw at y 32.9; right screw: through the 5.5 mm wall + nut
      ybar(C8_X - 14.0, C8_Z, 1.75, 40.0, 60.0), ybar(C8_X + 14.0, C8_Z, 1.75, 40.0, 60.0)]
m = diff(m, c8)
print(f'  bottom: RCA holes closed, C8 inlet at x {C8_X} z {C8_Z} (flange x {C8_X - 17.5}..{C8_X + 17.5})')
report('bottom', m)

# ---- front plate (button strip): no volume wheel; headphone jack + IR receiver instead.
# Front face is z=-26.38 (printed face down), front wall 2.29 mm, top edge is y=-124.72.
m = load('front-plate')
FRONT_Z0, FRONT_Z1 = -26.38, -24.09
m = union([m, box(-27.34, 8.51, -122.22, -113.46, FRONT_Z0, FRONT_Z1)])   # close the wheel slot
JACK_X, IR_X, ROW_Y = -19.0, 0.0, -117.0   # jack body (~10 mm flange) clears the top lip at y=-122.21
JACK_D, IR_D = 6.2, 6.0
m = diff(m, [cyl(JACK_X, ROW_Y, JACK_D / 2, FRONT_Z0 - 1, FRONT_Z1 + 1),
             cyl(IR_X, ROW_Y, IR_D / 2, FRONT_Z0 - 1, FRONT_Z1 + 1),
             # rear pockets through the old wheel ramp so the jack body / IR module sit on the front wall
             box(JACK_X - 6, JACK_X + 6, -122.21, -110.0, FRONT_Z1, -12.0),
             box(IR_X - 4.5, IR_X + 4.5, -122.21, -110.0, FRONT_Z1, -12.0)])
print(f'  front-plate: wheel slot closed; jack D{JACK_D} at x={JACK_X}, IR D{IR_D} at x={IR_X}, y={ROW_Y}')
report('front-plate', m)

# ---- x-brace: keep both ends, compress the middle (diagonals), re-drill holes
m = load('x-brace')
y_top, y_bot = -400.0, -480.0          # keep y > -400 as is, shift y < -480 up by D
top = half(m, y_top, False)
mid = half(half(m, y_top, True), y_bot, False)
bot = half(m, y_bot, True)
k = (y_top - y_bot - D) / (y_top - y_bot)
mid.apply_transform(np.array([[1, 0, 0, 0], [0, k, 0, y_top * (1 - k)], [0, 0, 1, 0], [0, 0, 0, 1]]))
bot.apply_translation([0, D, 0])
m = union([top, mid, bot])
ny = y_top - (y_top - -435.07) * k
m = diff(m, [cyl(21.50, ny, 0.85, -3, 6), cyl(79.50, ny, 0.85, -3, 6)])
# the middle side tabs keyed into bezel notches that were inside the removed band
y_mid0 = y_top - (y_top - y_bot) * k
m = diff(m, [box(-50, -44.6, y_mid0 + 0.5, y_top - 0.5, -3, 6), box(130.0, 135, y_mid0 + 0.5, y_top - 0.5, -3, 6)])
print(f'  x-brace: middle compressed x{k:.3f}; hub holes y -435.07 -> {ny:.2f}, -484.07 -> {-484.07 + D:.2f}')
report('x-brace', m)

