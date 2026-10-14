"""Front button strip v2 + button caps + auxiliary (button) PCB.

Plate frame (front-plate.stl): x as seen from the front (right = +x), y down (top edge -124.72,
bottom edge -103.57), z towards the back (front face -26.38, front wall -26.38..-24.09, back -13.28).
Shell frame = plate + (0, 362.94, 8.32).

The aux PCB stands upright 0.46 mm behind the plate (front face z -12.82), components on its front
side: 7 tact switches 6x6x7 mm, 3 mm LED, IR receiver (VS1838B/TSOP), 3.5 mm jack PJ-392 style
(M6 nut on the front face, pins to the PCB). JST-XH connectors on its back side.

python frontpanel.py <front-plate.stl in> <out folder>   (input = front plate after make169.py)
"""
import os, sys
import numpy as np
import trimesh
from shapely.geometry import Polygon
from shapely.ops import unary_union

FRONT, WALL_IN, PLATE_BACK = -26.38, -24.09, -13.28
PCB_FRONT = -12.82                      # aux PCB front face (0.46 mm behind the plate)
PCB_T = 1.6
ROW_Y = -117.91                         # centre of the button row (original holes -122.21..-113.61)
BTN_X = [6.6, 19.6, 32.6, 45.6, 58.6, 71.6]   # 2 new + the 4 original buttons, 13 mm pitch
BTN_HOLE = 8.6
PWR = dict(x=113.5, y=-119.36, notch=(105.0, 122.0, -124.72, -114.0))
LED = dict(x=99.32, y=-118.44, d=3.2)
IR = dict(x=-10.0, y=ROW_Y, d=6.0)
JACK = dict(x=-22.0, y=-118.0, d=6.2, body=(11.0, 11.0))   # PJ-392 style, M6 thread
SW_H = 7.0                              # tact switch 6x6, height incl. plunger
PLUNGER = PCB_FRONT - SW_H              # -19.82
MOUNT = [(-2.0, -109.0), (86.0, -109.0)]      # M2.5 self-tapping, PCB to plate

# aux PCB outline (plate frame); notches around the tray posts that hold the plate screws
PCB_X0, PCB_X1, PCB_Y0, PCB_Y1 = -36.5, 121.5, -124.4, -106.5
POSTS = [(-36.5, -26.8), (41.5, 52.5), (94.5, 105.5)]   # x ranges, from y -113.3 down
POST_TOP = -113.3

def box(x0, x1, y0, y1, z0, z1):
    b = trimesh.creation.box(extents=[abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)])
    b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]); return b

def cyl(x, y, r, z0, z1):
    c = trimesh.creation.cylinder(radius=r, height=z1 - z0, sections=64)
    c.apply_translation([x, y, (z0 + z1) / 2]); return c

def clean(r):
    r.merge_vertices()
    if not r.is_volume:
        r = trimesh.util.concatenate([b for b in r.split(only_watertight=False) if abs(b.volume) > 1e-3])
        r.merge_vertices()
    return r

def union(ms): return clean(trimesh.boolean.union(ms, engine='manifold'))
def diff(a, bs): return clean(trimesh.boolean.difference([a] + list(bs), engine='manifold'))

def text_solid(txt, x, y, h, z0, z1):
    """engraving cutter: text centred at (x, y) on the front face, glyph up = -y"""
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties
    tp = TextPath((0, 0), txt, size=h / 0.72, prop=FontProperties(family='DejaVu Sans', weight='bold'))
    rings = [Polygon(r) for r in tp.to_polygons() if len(r) > 2]
    shape = None
    for r in rings:                      # even-odd fill (holes in P, R ...)
        shape = r if shape is None else shape.symmetric_difference(r)
    minx, miny, maxx, maxy = shape.bounds
    out = []
    for g in getattr(shape, 'geoms', [shape]):
        m = trimesh.creation.extrude_polygon(g.buffer(0), z1 - z0)
        m.apply_transform(np.array([[1, 0, 0, x - (minx + maxx) / 2], [0, -1, 0, y + (miny + maxy) / 2],
                                    [0, 0, -1, z1], [0, 0, 0, 1]], float))
        m.fix_normals()
        out.append(m)
    return out

def plate(m):
    # close the previous jack (x -19) / IR (x 0) holes and the 3x3 LED window in the front wall
    m = union([m, box(-23.0, 4.0, -121.6, -112.5, FRONT, WALL_IN),
                  box(97.6, 101.1, -120.2, -116.7, FRONT, WALL_IN)])
    cut = []
    for x in BTN_X:                      # button holes + pockets for the cap wings and the switch
        cut.append(box(x - BTN_HOLE / 2, x + BTN_HOLE / 2, ROW_Y - BTN_HOLE / 2, ROW_Y + BTN_HOLE / 2, FRONT - 1, WALL_IN + 0.01))
        cut.append(box(x - 5.4, x + 5.4, ROW_Y - BTN_HOLE / 2, ROW_Y + BTN_HOLE / 2, WALL_IN, PLATE_BACK + 1.5))
    n = PWR['notch']                     # PWR: existing notch, pocket behind for the cap flange
    cut.append(box(n[0] - 2.0, n[1] + 2.0, n[2] - 0.5, n[3] + 2.4, WALL_IN, PLATE_BACK + 1.5))
    cut += [cyl(LED['x'], LED['y'], LED['d'] / 2, FRONT - 1, WALL_IN + 0.01), cyl(LED['x'], LED['y'], 2.3, WALL_IN, PLATE_BACK + 1.5)]
    cut += [cyl(IR['x'], IR['y'], IR['d'] / 2, FRONT - 1, WALL_IN + 0.01), box(IR['x'] - 5, IR['x'] + 5, -122.2, -112.6, WALL_IN, PLATE_BACK + 1.5)]
    bw, bh = JACK['body']
    cut += [cyl(JACK['x'], JACK['y'], JACK['d'] / 2, FRONT - 1, WALL_IN + 0.01),
            box(JACK['x'] - bw / 2 - 0.5, JACK['x'] + bw / 2 + 0.5, JACK['y'] - bh / 2 - 0.2, JACK['y'] + bh / 2 + 0.2, WALL_IN, PLATE_BACK + 1.5)]
    m = diff(m, cut)
    # PCB mounting bosses (M2.5 self-tapping, pilot 2.2 mm), 0.1 mm short of the PCB
    m = union([m] + [cyl(x, y, 3.0, WALL_IN - 0.01, PCB_FRONT - 0.1) for x, y in MOUNT])
    m = diff(m, [cyl(x, y, 1.1, -21.0, PCB_FRONT + 1) for x, y in MOUNT])
    # "PWR" engraved 0.6 mm under the power button
    m = diff(m, text_solid('PWR', PWR['x'], -108.8, 3.2, FRONT - 1.0, FRONT + 0.6))
    return m

def caps_assembled():
    """caps in their place (plate frame): [(mesh, is_pwr)]"""
    out = []
    for x in BTN_X:
        out.append((union([box(x - 4, x + 4, ROW_Y - 4, ROW_Y + 4, FRONT - 1.0, PLUNGER),
                           box(x - 5, x + 5, ROW_Y - 4, ROW_Y + 4, WALL_IN + 0.09, WALL_IN + 1.49)]), False))
    n = PWR['notch']
    out.append((union([box(n[0] + 0.3, n[1] - 0.3, n[2] + 0.12, n[3] - 0.3, FRONT - 1.0, WALL_IN + 0.09),
                       box(n[0] - 1.5, n[1] + 1.5, n[2] + 0.12, n[3] + 1.9, WALL_IN + 0.09, WALL_IN + 1.49),
                       box(PWR['x'] - 3, PWR['x'] + 3, PWR['y'] - 3, PWR['y'] + 3, WALL_IN + 1.48, PLUNGER)]), True))
    return out

AUX_PARTS = [  # name, x, y (plate frame), side, note
    *[(f'SW{i + 1}', x, ROW_Y, 'F', 'tact switch 6x6x7 THT') for i, x in enumerate(BTN_X)],
    ('SW7 PWR', PWR['x'], PWR['y'], 'F', 'tact switch 6x6x7 THT'),
    ('D1', LED['x'], LED['y'], 'F', 'LED 3 mm, body ~6 mm off the board (dome in the D3.2 hole)'),
    ('U1', IR['x'], IR['y'], 'F', 'IR receiver VS1838B/TSOP, leads bent 90 deg, dome in the D6 hole'),
    ('J1', JACK['x'], JACK['y'], 'F', '3.5 mm jack PJ-392 style, M6 nut on the front, body 11 mm'),
    ('J2', 23.0, -109.6, 'B', 'JST-XH 11 pin: 3V3, GND, SW1..SW7, LED, IR'),
    ('J3', -11.0, -109.6, 'B', 'JST-XH 3 pin: L, R, AGND'),
    *[(f'H{i + 1}', x, y, '-', 'M2.5 hole (D2.7) to the plate boss') for i, (x, y) in enumerate(MOUNT)],
]

def caps():
    """6 small caps + PWR cap, laid out face down for printing (z = 0 is the front face)"""
    parts = []
    def small(dx):
        L = PLUNGER - (FRONT - 1.0)                                   # 7.56
        body = box(-4.0, 4.0, -4.0, 4.0, 0, L)
        wings = box(-5.0, 5.0, -4.0, 4.0, (WALL_IN + 0.09) - (FRONT - 1.0), (WALL_IN + 1.49) - (FRONT - 1.0))
        c = union([body, wings]); c.apply_translation([dx, 0, 0]); return c
    for i in range(6):
        parts.append(small(i * 13.0))
    n = PWR['notch']; cx, cy = PWR['x'], PWR['y']
    front = box(n[0] + 0.3 - cx, n[1] - 0.3 - cx, n[2] + 0.12 - cy, n[3] - 0.3 - cy, 0, (WALL_IN + 0.09) - (FRONT - 1.0))
    flange = box(n[0] - 1.5 - cx, n[1] + 1.5 - cx, n[2] + 0.12 - cy, n[3] + 1.9 - cy,
                 (WALL_IN + 0.09) - (FRONT - 1.0), (WALL_IN + 1.49) - (FRONT - 1.0))
    stem = box(-3.0, 3.0, -3.0, 3.0, (WALL_IN + 1.49) - (FRONT - 1.0) - 0.01, PLUNGER - (FRONT - 1.0))
    p = union([front, flange, stem]); p.apply_translation([6 * 13.0 + 14.0, 0, 0])
    parts.append(p)
    return trimesh.util.concatenate(parts)

def pcb_outline():
    """polygon in plate coords (x, y_p)"""
    pts = [(PCB_X0, PCB_Y0), (PCB_X1, PCB_Y0), (PCB_X1, PCB_Y1)]
    for x0, x1 in reversed(POSTS):
        if x0 <= PCB_X0:                 # notch open to the left board edge
            pts += [(x1, PCB_Y1), (x1, POST_TOP), (PCB_X0, POST_TOP)]
            return Polygon(pts)
        pts += [(x1, PCB_Y1), (x1, POST_TOP), (x0, POST_TOP), (x0, PCB_Y1)]
    pts.append((PCB_X0, PCB_Y1))
    return Polygon(pts)

if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]
    m = trimesh.load(src); m.merge_vertices()
    m = plate(m)
    print('front-plate watertight', m.is_watertight)
    m.export(os.path.join(dst, 'front-plate.stl'))
    c = caps(); c.export(os.path.join(dst, 'buttons.stl'))
    print('buttons', np.round(c.extents, 1), 'watertight', all(b.is_watertight for b in c.split()))
    print('aux PCB outline', np.round(pcb_outline().bounds, 2), 'area', round(pcb_outline().area))
