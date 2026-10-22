"""Top view of the tray with 6 x 18650 cells (rough fit study).

    python battery.py <elements folder> <out.png>
"""
import os, sys
import numpy as np, trimesh
from PIL import ImageDraw, ImageFont
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rast import render
from layout import H_ADD, TRAY_FLOOR, PCB_BOT

OUT, PNG = sys.argv[1], sys.argv[2]
W = np.array([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]], float)   # shell -> world (y up)
TRAY = np.array([[1, 0, 0, 0], [0, 0, -1, 233.14 + H_ADD], [0, 1, 0, 38.51], [0, 0, 0, 1]], float)

CELL_D, CELL_L, RAISE, PITCH = 18.3, 65.0, 4.0, 19.0
CELL_X = [-7.0 + PITCH * k for k in range(6)]           # 6 cells side by side, axis front-to-back (clear of the C8 inlet)
CELL_Z = (10.0, 10.0 + CELL_L)                          # behind the strip posts, in front of the back wall
CELL_Y = TRAY_FLOOR - RAISE - CELL_D / 2                # on 4 mm holders over the low floor ribs

scene = []
m = trimesh.load(os.path.join(OUT, 'bottom.stl')); m.apply_transform(TRAY); scene.append((m, (0.22, 0.225, 0.24)))
for x in CELL_X:
    c = trimesh.creation.cylinder(radius=CELL_D / 2, height=CELL_L - 2.0, sections=48)
    c.apply_translation([x, CELL_Y, sum(CELL_Z) / 2]); scene.append((c, (0.16, 0.38, 0.78)))
    for z0 in (CELL_Z[0], CELL_Z[1] - 1.0):                             # metal ends
        e = trimesh.creation.cylinder(radius=CELL_D / 2 - 0.3, height=1.0, sections=48)
        e.apply_translation([x, CELL_Y, z0 + 0.5]); scene.append((e, (0.75, 0.76, 0.78)))
# power board on the right (floor cleared there): 22 x 70 on 4 mm standoffs. Front: 2-pole terminal
# (+/- always 5 V out), 5 V 3 A boost, diodes / MOSFETs (power path), 3 A Li-ion charger with protection,
# back: 4-pole terminal (BAT +/-, 5 V +/- from the PSU)
PB = dict(x0=99.0, x1=121.0, z0=8.0, z1=78.0)
pcb_y1 = TRAY_FLOOR - RAISE; pcb_y0 = pcb_y1 - 1.6
def blk(x0, x1, z0, z1, h, col):
    b = trimesh.creation.box(extents=[x1 - x0, h, z1 - z0]); b.apply_translation([(x0 + x1) / 2, pcb_y0 - h / 2, (z0 + z1) / 2])
    scene.append((b, col))
pb = trimesh.creation.box(extents=[PB['x1'] - PB['x0'], 1.6, PB['z1'] - PB['z0']])
pb.apply_translation([(PB['x0'] + PB['x1']) / 2, (pcb_y0 + pcb_y1) / 2, (PB['z0'] + PB['z1']) / 2]); scene.append((pb, (0.1, 0.45, 0.25)))
blk(104.9, 115.1, 9.0, 16.6, 10.0, (0.15, 0.45, 0.85))                 # 2-pole 5.08 terminal: 5 V out
blk(101.5, 118.5, 19.0, 41.0, 5.0, (0.12, 0.12, 0.14))                 # boost 5 V 3 A (~17 x 22)
blk(102.0, 118.0, 43.0, 47.0, 4.0, (0.3, 0.3, 0.32))                   # diodes / MOSFETs
blk(99.5, 120.5, 49.0, 68.0, 4.0, (0.12, 0.12, 0.14))                  # charger 3 A + protection (~21 x 19)
blk(99.8, 120.2, 69.5, 77.1, 10.0, (0.15, 0.45, 0.85))                 # 4-pole 5.08 terminal: BAT +/-, PSU 5 V +/-

tris, cols = [], []
for mm, col in scene:
    mm = mm.copy(); mm.apply_transform(W)
    tris.append(mm.triangles); cols.append(np.tile(col, (len(mm.faces), 1)))
tris, cols = np.concatenate(tris), np.concatenate(cols)
c = np.array([42.0, -(TRAY_FLOOR - 10), -34.0])
im = render(tris, cols, eye=c + np.array([0, 330, 25]), target=c, light=(0.2, 0.9, 0.3), fov=32)
d = ImageDraw.Draw(im)
try: f = ImageFont.truetype('segoeuib.ttf', 30); f2 = ImageFont.truetype('segoeui.ttf', 24)
except OSError: f = f2 = ImageFont.load_default()
d.text((30, 24), '6 x 18650 in the tray (top view, front at the bottom)', font=f, fill=(35, 35, 40))
d.text((30, 64), f'cells D{CELL_D} x {CELL_L:.0f}, pitch {PITCH:.0f} mm, on {RAISE:.0f} mm holders; '
                 f'{TRAY_FLOOR - PCB_BOT - RAISE - CELL_D:.1f} mm free up to the main board', font=f2, fill=(60, 60, 65))
d.text((30, 96), 'right: power board 22 x 70 - front: 5 V out terminal, 5 V 3 A boost, diodes, 3 A charger, back: BAT / PSU terminal', font=f2, fill=(60, 60, 65))
im.save(PNG); print('ok')
