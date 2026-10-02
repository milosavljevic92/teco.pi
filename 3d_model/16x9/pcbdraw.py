"""Main board drawing (PNG, dimensioned) + outline for KiCad (DXF R12, mm).
Board coords: X from the left side wall seen from the FRONT, Y from the front edge; view from above,
front at the bottom of the drawing."""
import sys, os, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from layout import *

PNG, DXF = sys.argv[1], sys.argv[2]
X = lambda x: x - PCB_X0
Y = lambda z: z - PCB_Z0
W_, H_ = X(PCB_X1), Y(PCB_Z1)
nx0, nx1, nz0 = X(NOTCH_PORTS[0]), X(NOTCH_PORTS[1]), Y(NOTCH_PORTS[2])
cL = X(WALL_L + NOTCH_CORNER); cR = X(WALL_R - NOTCH_CORNER); cz = Y(NOTCH_CORNER_Z)
if cR - nx1 < 6.0:     # port cut-out next to the right back corner: merge, no thin finger
    outline = [(0, 0), (W_, 0), (W_, cz), (cR, cz), (cR, nz0), (nx0, nz0), (nx0, H_), (cL, H_), (cL, cz), (0, cz)]
    nx1 = cR
elif nx0 - cL < 6.0:
    outline = [(0, 0), (W_, 0), (W_, cz), (cR, cz), (cR, H_), (nx1, H_), (nx1, nz0), (cL, nz0), (cL, cz), (0, cz)]
else:
    outline = [(0, 0), (W_, 0), (W_, cz), (cR, cz), (cR, H_), (nx1, H_), (nx1, nz0), (nx0, nz0), (nx0, H_),
               (cL, H_), (cL, cz), (0, cz)]
holes = [(X(x), Y(z)) for x, z in PI_HOLES]
hdr_x = [X(v) for v in HDR_ROWS]; hdr_y = [Y(v) for v in HDR_Z]

fig, ax = plt.subplots(figsize=(17, 10.5))
ax.add_patch(Polygon(outline, closed=True, fc='#1f6b43', ec='#0b3d24', lw=2, alpha=0.9))
# Pi footprint (upside down above the board) and its ports
ax.add_patch(Rectangle((X(pi_x(0)), Y(pi_z(0))), 56, 85, fc='none', ec='#b9f5c8', lw=1.5, ls='--'))
ax.text(X(pi_x(28)), Y(pi_z(30)), 'Raspberry Pi 3B\n(upside down, 11 mm above)', color='#e8fff0', ha='center', va='center', fontsize=11)
for name, y0, y1, h, L, o in PORTS:
    ax.add_patch(Rectangle((X(pi_x(y0)), Y(PI_EDGE + o - L)), y1 - y0, L, fc='#c9ccd1', ec='#6b6f75', lw=1, alpha=0.9))
    ax.text(X(pi_x((y0 + y1) / 2)), Y(PI_EDGE + o - L / 2), name, ha='center', va='center', fontsize=8, rotation=90)
# header (2x20 female on the main board)
ax.add_patch(Rectangle((hdr_x[0] - 1.27, hdr_y[0] - 1.27), hdr_x[1] - hdr_x[0] + 2.54, hdr_y[1] - hdr_y[0] + 2.54,
                       fc='#111', ec='#000'))
for i in range(20):
    for j, hx in enumerate(hdr_x):
        ax.add_patch(Circle((hx, hdr_y[0] + i * 2.54), 0.5, fc='#d4af37' if (i or j) else '#ff5050'))
ax.annotate('pin 1 (3V3), pin 2 (5V) next to it\nat the front (SD card) end', (hdr_x[0], hdr_y[0]), (hdr_x[0] - 60, hdr_y[0] + 3),
            fontsize=9, arrowprops=dict(arrowstyle='->'), color='#f4f4f4', ha='right')
# Pi 3B chip antenna (Wi-Fi / BT) sits at the corner next to GPIO pin 1: keep copper and parts away
ant = (X(pi_x(43.0)), Y(pi_z(0.0)), 13.0, 12.0)
ax.add_patch(Rectangle(ant[:2], ant[2], ant[3], fc='#3060ff', alpha=0.25, ec='#3060ff', lw=1.5, ls='--'))
ax.text(ant[0] - 1, ant[1] + ant[3] + 1.5, 'antenna above:\nno copper / parts', fontsize=8, color='#1a3a9a', ha='right')
for hx, hy in holes:
    ax.add_patch(Circle((hx, hy), 2.5, fc='#d4af37', ec='#8a6d1a'))
    ax.add_patch(Circle((hx, hy), 1.35, fc='white'))
# keep-outs / notes
ax.add_patch(Rectangle((0, 0), X(WALL_L + SPK['depth']), H_, fc='none', ec='#f0a020', lw=1.2, ls=':'))
ax.add_patch(Rectangle((X(WALL_R - SPK['depth']), 0), X(WALL_R) - X(WALL_R - SPK['depth']), H_, fc='none', ec='#f0a020', lw=1.2, ls=':'))
ax.text(X(WALL_L + SPK['depth'] / 2), Y(30), f'speaker above\nparts <= {PCB_TOP - SPK["y"] - SPK["r"] - 1:.0f} mm', ha='center', fontsize=8, color='#f0c060')
ax.text(X(WALL_R - SPK['depth'] / 2), Y(30), f'speaker above\nparts <= {PCB_TOP - SPK["y"] - SPK["r"] - 1:.0f} mm', ha='center', fontsize=8, color='#f0c060')
ax.add_patch(Rectangle((X(C8['x0']), Y(C8['z0'])), C8['x1'] - C8['x0'], C8['z1'] - C8['z0'], fc='none', ec='#ff4040', lw=1.5, ls='--'))
ax.text(X((C8['x0'] + C8['x1']) / 2), Y(C8['z0']) - 4, '230 V inlet BELOW the board', ha='center', fontsize=8, color='#ff8080')
for tx, tz in ((-41.6, 9.0), (125.9, 9.0), (-41.6, 71.4), (125.9, 71.4)):
    ax.plot(X(tx), Y(tz), marker='x', color='#ffd0d0', ms=7)

def dim(x0, y0, x1, y1, txt, off, horiz=True):
    if horiz:
        ax.annotate('', (x0, y0 + off), (x1, y0 + off), arrowprops=dict(arrowstyle='<->', lw=1))
        ax.plot([x0, x0], [y0, y0 + off], 'k-', lw=0.5); ax.plot([x1, x1], [y1, y0 + off], 'k-', lw=0.5)
        ax.text((x0 + x1) / 2, y0 + off + (1.2 if off > 0 else -3.5), txt, ha='center', fontsize=10)
    else:
        ax.annotate('', (x0 + off, y0), (x0 + off, y1), arrowprops=dict(arrowstyle='<->', lw=1))
        ax.plot([x0, x0 + off], [y0, y0], 'k-', lw=0.5); ax.plot([x1, x0 + off], [y1, y1], 'k-', lw=0.5)
        ax.text(x0 + off + (1.2 if off > 0 else -1.2), (y0 + y1) / 2, txt, va='center', ha='left' if off > 0 else 'right', fontsize=10, rotation=90)
dim(0, 0, W_, 0, f'{W_:.1f}', -8)
dim(0, 0, 0, H_, f'{H_:.1f}', -8, horiz=False)
dim(nx0, H_, nx1, H_, f'{nx1 - nx0:.1f}', 6)
dim(0, H_, cL, H_, f'{cL:.1f}', 6)
dim(cR, H_, W_, H_, f'{W_ - cR:.1f}', 6)
dim(W_, cz, W_, H_, f'{H_ - cz:.1f}', 7, horiz=False)
dim(nx0, nz0, nx0, H_, f'{H_ - nz0:.1f}', -4, horiz=False)
dim(0, 0, nx0, 0, f'{nx0:.1f}', -26)
dim(0, holes[0][1], holes[0][0], holes[0][1], f'{holes[0][0]:.2f}', -17 - holes[0][1])
dim(holes[0][0], holes[0][1], holes[1][0], holes[0][1], f'{holes[1][0] - holes[0][0]:.1f}', -17 - holes[0][1])
dim(W_, holes[0][1], W_, holes[2][1], f'{holes[2][1] - holes[0][1]:.1f}', 16, horiz=False)
dim(W_, 0, W_, holes[0][1], f'{holes[0][1]:.1f}', 16, horiz=False)

ax.text(0, H_ + 21, f'Teco.Pi main board  {W_:.1f} x {H_:.1f} mm  (top view, FRONT of the TV at the bottom)', fontsize=14, weight='bold')
ax.text(0, H_ + 15.5, f'Board top 11.0 mm below the Pi (8.5 mm female header + 2.5 mm Pi header); {TRAY_FLOOR - PCB_BOT:.1f} mm free under the board (tray floor); '
        f'Pi standoffs M2.5 x 11 (gold)', fontsize=10)
ax.text(0, -37, f'x = tray screws (from below, into the shell lips)   dotted orange = speaker above (parts up to {PCB_TOP - SPK["y"] - SPK["r"] - 1:.0f} mm)   '
        'dashed red = 230 V inlet in the tray below the board   grey = Pi ports (no board there)', fontsize=9)
ax.set_xlim(-22, W_ + 26); ax.set_ylim(-41, H_ + 25); ax.set_aspect('equal'); ax.axis('off')
fig.tight_layout(); fig.savefig(PNG, dpi=110, facecolor='white')

# ---- DXF R12: outline on layer Edge.Cuts, Pi holes (2.75 mm) on layer Holes
def dxf_line(a, b, layer):
    return f'0\nLINE\n8\n{layer}\n10\n{a[0]:.4f}\n20\n{a[1]:.4f}\n30\n0.0\n11\n{b[0]:.4f}\n21\n{b[1]:.4f}\n31\n0.0\n'
def dxf_circle(c, r, layer):
    return f'0\nCIRCLE\n8\n{layer}\n10\n{c[0]:.4f}\n20\n{c[1]:.4f}\n30\n0.0\n40\n{r:.4f}\n'
ents = ''.join(dxf_line(outline[i], outline[(i + 1) % len(outline)], 'Edge.Cuts') for i in range(len(outline)))
ents += ''.join(dxf_circle(h, 1.375, 'Holes') for h in holes)
with open(DXF, 'w') as f:
    f.write('0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n' + ents + '0\nENDSEC\n0\nEOF\n')
print('board', round(W_, 2), round(H_, 2), 'holes', [tuple(round(v, 2) for v in h) for h in holes],
      'header rows X', [round(v, 2) for v in hdr_x], 'Y', [round(v, 2) for v in hdr_y])

