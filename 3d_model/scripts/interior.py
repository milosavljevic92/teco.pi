"""Interior views: back cover removed (rear) and roof cut away (top), with main board, Pi, PSU,
speakers, panel and mains inlet as simple blocks."""
import sys, os, numpy as np, trimesh
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rast import render
from layout import *

OUT, PNG = sys.argv[1], sys.argv[2]
def T(r): return np.array(r, float)
FP_Y, FP_Z = 362.94 + H_ADD, 8.32 - FRONT_ADD    # front-plate frame -> shell
W = T([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]])   # shell -> world (y up, front towards +z)
SHELL = (0.235, 0.24, 0.255); BEZEL = (0.17, 0.175, 0.185); TRAY = (0.13, 0.135, 0.145); PLATE = (0.2, 0.205, 0.22)
GREEN = (0.08, 0.36, 0.2); PIGREEN = (0.1, 0.45, 0.22); SILVER = (0.72, 0.73, 0.75); BLACK = (0.08, 0.08, 0.09)
PSUC = (0.62, 0.64, 0.66); SPKC = (0.18, 0.18, 0.2); PANELC = (0.3, 0.31, 0.33); C8C = (0.05, 0.05, 0.05)
GOLD = (0.8, 0.65, 0.25); FANC = (0.12, 0.12, 0.13)

def part(name, M, col, clip_y=None):
    m = trimesh.load(f'{OUT}\\{name}.stl'); m.apply_transform(M)
    if clip_y is not None:           # keep shell y >= clip_y (cut the roof away)
        keep = trimesh.creation.box(extents=[400, 200, 400]); keep.apply_translation([0, clip_y + 100, 0])
        m = trimesh.boolean.intersection([m, keep], engine='manifold')
    return m, col

def box(x0, x1, y0, y1, z0, z1, col):
    b = trimesh.creation.box(extents=[abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)])
    b.apply_translation([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]); return b, col

def cyl_x(xa, xb, y, z, r, col):
    c = trimesh.creation.cylinder(radius=r, height=abs(xb - xa), sections=48)
    c.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    c.apply_translation([(xa + xb) / 2, y, z]); return c, col

def main_board():
    b = trimesh.creation.box(extents=[PCB_X1 - PCB_X0, PCB_T, PCB_Z1 - PCB_Z0])
    b.apply_translation([(PCB_X0 + PCB_X1) / 2, (PCB_TOP + PCB_BOT) / 2, (PCB_Z0 + PCB_Z1) / 2])
    cuts = [trimesh.creation.box(extents=[NOTCH_PORTS[1] - NOTCH_PORTS[0], 10, 40])]
    cuts[0].apply_translation([(NOTCH_PORTS[0] + NOTCH_PORTS[1]) / 2, PCB_TOP, NOTCH_PORTS[2] + 20])
    for x0, x1 in ((PCB_X0 - 1, WALL_L + NOTCH_CORNER), (WALL_R - NOTCH_CORNER, PCB_X1 + 1)):
        c = trimesh.creation.box(extents=[x1 - x0, 10, 40]); c.apply_translation([(x0 + x1) / 2, PCB_TOP, NOTCH_CORNER_Z + 20])
        cuts.append(c)
    return trimesh.boolean.difference([b] + cuts, engine='manifold'), GREEN

def components():
    out = [main_board()]
    # Pi upside down: PCB, ports below it, header + female header down to the main board
    out.append(box(pi_x(0), pi_x(56), PI_BACK, PI_COMP, PI_EDGE - 85, PI_EDGE, PIGREEN))
    for name, y0, y1, h, L, o in PORTS:
        out.append(box(pi_x(y0), pi_x(y1), PI_COMP, PI_COMP + h, PI_EDGE + o - L, PI_EDGE + o, SILVER))
    out.append(box(HDR_ROWS[0] - 2.5, HDR_ROWS[1] + 2.5, PI_COMP, PI_COMP + 2.5, HDR_Z[0] - 1.3, HDR_Z[1] + 1.3, BLACK))
    out.append(box(HDR_ROWS[0] - 2.5, HDR_ROWS[1] + 2.5, PI_COMP + 2.5, PCB_TOP, HDR_Z[0] - 1.3, HDR_Z[1] + 1.3, BLACK))
    for hx, hz in PI_HOLES:
        out.append(cyl_y(hx, hz, PI_COMP, PCB_TOP, 2.5, GOLD))
    # RCA 3x3 + 1x3 blocks on the back edge, expansion cards in the two slots (component side +x)
    for r in (RCA3, RCA1):
        out.append(box(r['x0'], r['x1'], PCB_TOP - r['h'], PCB_TOP, PCB_Z1 + 0.4 - r['depth'], PCB_Z1 + 0.4, BLACK))
    for xc in CARD_XC:
        cb = PCB_TOP - CARD['above_board']
        out.append(box(xc - 6.7, xc - 5.1, cb - CARD['h'], cb, BACK_IN - 1.2 - CARD['length'], BACK_IN - 1.2, PIGREEN))
        out.append(box(xc - 6.7, xc - 4.2, cb, PCB_TOP, BACK_IN - 1.2 - 45, BACK_IN - 1.2 - 15, BLACK))   # card header
    # HDMI / USB-C plugs behind the screen: free zone where they clear the Pi
    out.append(box(PLUG_X[0], PLUG_X[1], PLUG_Y[0], PLUG_Y[1], MODULE['z1'], MODULE['z1'] + 40, (0.6, 0.22, 0.2)))
    # a few placeholder parts on the main board (amp, PCF8574, FM tuner), just for scale
    out.append(box(-30, -5, PCB_TOP - 6, PCB_TOP, 15, 40, BLACK))
    out.append(box(0, 25, PCB_TOP - 4, PCB_TOP, 20, 35, BLACK))
    # PSU under the roof (terminals to +x)
    out.append(box(PSU['x0'], PSU['x1'], PSU['y0'], PSU['y1'], PSU['z0'], PSU['z1'], PSUC))
    out.append(box(PSU['x0'] + 6, PSU['x1'] - 6, PSU['y0'] + 10, PSU['y1'], PSU['z1'], PSU['term'], BLACK))  # terminals
    # speakers on both side walls
    out.append(cyl_x(WALL_L, WALL_L + SPK['depth'], SPK['y'], SPK['z'], SPK['r'], SPKC))
    out.append(cyl_x(WALL_R - SPK['depth'], WALL_R, SPK['y'], SPK['z'], SPK['r'], SPKC))
    # panel back (in the bezel) and the mains inlet in the tray
    out.append(box(MODULE['x0'], MODULE['x1'], MODULE['y0'], MODULE['y1'], MODULE['z0'], MODULE['z1'], PANELC))
    out.append(box(C8['x0'], C8['x1'], C8['y0'], C8['y1'], C8['z0'], C8['z1'], C8C))
    # button board behind the front strip (plate frame -> shell: y + FP_Y, z + FP_Z)
    import frontpanel as fp
    pl = trimesh.creation.extrude_polygon(fp.pcb_outline(), fp.PCB_T)
    pl.apply_translation([0, FP_Y, fp.PCB_FRONT + FP_Z])
    out.append((pl, GREEN))
    for name, x, y, side, _ in fp.AUX_PARTS:
        if name.startswith('SW'):
            out.append(box(x - 3, x + 3, y + FP_Y - 3, y + FP_Y + 3, fp.PCB_FRONT + FP_Z - 3.5, fp.PCB_FRONT + FP_Z, BLACK))
        elif name in ('J2', 'J3'):
            n = 11 if name == 'J2' else 3; L = 2.5 * (n - 1) + 4.9
            out.append(box(x - L / 2, x + L / 2, y + FP_Y - 2.9, y + FP_Y + 2.9,
                           fp.PCB_FRONT + FP_Z + fp.PCB_T, fp.PCB_FRONT + FP_Z + fp.PCB_T + 7, (0.93, 0.9, 0.82)))
    return out

def cyl_y(x, z, y0, y1, r, col):
    c = trimesh.creation.cylinder(radius=r, height=abs(y1 - y0), sections=32)
    c.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    c.apply_translation([x, (y0 + y1) / 2, z]); return c, col

def scene(clip_roof, with_fan):
    S = [part('top', np.eye(4), SHELL, clip_y=clip_roof),
         part('screen-bezel', T([[1, 0, 0, 0], [0, 1, 0, 436.05], [0, 0, 1, -16.0], [0, 0, 0, 1]]), BEZEL),
         part('bottom', T([[1, 0, 0, 0], [0, 0, -1, 238.22 - 5.08 + H_ADD], [0, 1, 0, 38.51], [0, 0, 0, 1]]), TRAY),
         part('front-plate', T([[1, 0, 0, 0], [0, 1, 0, FP_Y], [0, 0, 1, FP_Z], [0, 0, 0, 1]]), PLATE),
         part('x-brace', T([[1, 0, 0, 0], [0, 1, 0, 604.84], [0, 0, 1, -8.5], [0, 0, 0, 1]]), PLATE)]
    S += components()
    if clip_roof is not None:
        S = [s for s in S if not (s[1] is PSUC or s[1] is SPKC or s[1] is BLACK and s[0].bounds[0][1] < 170)]
    if with_fan:
        S.append(box(FAN['x0'], FAN['x1'], FAN['y0'], FAN['y1'], FAN['z0'], FAN['z1'], FANC))
    tris, cols = [], []
    for m, col in S:
        m = m.copy(); m.apply_transform(W)
        tris.append(m.triangles); cols.append(np.tile(col, (len(m.faces), 1)))
    return np.concatenate(tris), np.concatenate(cols)

def label(img, items):
    d = ImageDraw.Draw(img)
    try: fnt = ImageFont.truetype('segoeui.ttf', 22)
    except OSError: fnt = ImageFont.load_default()
    for (x, y), text in items:
        d.text((x, y), text, font=fnt, fill=(30, 30, 35))
    return img

tris, cols = scene(None, False)
c = np.array([42.1, -190.0, -45.0])
rear = render(tris, cols, eye=c + np.array([150, 120, -330]), target=c + np.array([0, -25, 0]), light=(0.4, 0.8, -0.6), fov=28)
tris, cols = scene(200.0, False)
top = render(tris, cols, eye=c + np.array([-70, 420, -200]), target=c + np.array([0, -38, 5]), light=(-0.3, 0.9, -0.3), fov=32)
sheet = Image.new('RGB', (rear.width + top.width, rear.height), 'white')
sheet.paste(rear, (0, 0)); sheet.paste(top, (rear.width, 0))
sheet.save(PNG); rear.save(PNG.replace('.png', '-rear.png')); top.save(PNG.replace('.png', '-top.png'))
print('ok')
