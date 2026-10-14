"""Auxiliary (button) PCB behind the front strip: dimensioned drawing (PNG) + outline for KiCad (DXF).
PCB coords: seen from its front (component) side = from the front of the TV, origin bottom-left."""
import sys, os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon as MPoly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frontpanel import *

PNG, DXF = sys.argv[1], sys.argv[2]
X = lambda x: x - PCB_X0
Y = lambda y: PCB_Y1 - y               # y_p down -> PCB Y up
poly = pcb_outline()
pts = [(X(x), Y(y)) for x, y in poly.exterior.coords[:-1]]
W_, H_ = X(PCB_X1), Y(PCB_Y0)

fig, ax = plt.subplots(figsize=(18, 6.2))
ax.add_patch(MPoly(pts, closed=True, fc='#1f6b43', ec='#0b3d24', lw=2))
for name, x, y, side, note in AUX_PARTS:
    px, py = X(x), Y(y)
    if name.startswith('SW'):
        ax.add_patch(Rectangle((px - 3, py - 3), 6, 6, fc='#222', ec='#000'))
        ax.add_patch(Circle((px, py), 1.75, fc='#555'))
        for sx in (-3.25, 3.25):
            for sy in (-2.25, 2.25):
                ax.add_patch(Circle((px + sx, py + sy), 0.5, fc='#d4af37'))
    elif name == 'D1':
        ax.add_patch(Circle((px, py), 1.6, fc='#ff8c1a', ec='#a04000'))
    elif name == 'U1':
        ax.add_patch(Rectangle((px - 3.25, py - 3.8), 6.5, 7.6, fc='#111', ec='#000'))
        ax.add_patch(Circle((px, py), 2.3, fc='#333'))
    elif name == 'J1':
        bw, bh = JACK['body']
        ax.add_patch(Rectangle((px - bw / 2, py - bh / 2), bw, bh, fc='#444', ec='#000'))
        ax.add_patch(Circle((px, py), 3.1, fc='#888'))
    elif name in ('J2', 'J3'):
        n = 11 if name == 'J2' else 3
        L = 2.5 * (n - 1) + 4.9
        ax.add_patch(Rectangle((px - L / 2, py - 2.9), L, 5.75, fc='#f5f0e0', ec='#998', ls='--'))
        for k in range(n):
            ax.add_patch(Circle((px - 2.5 * (n - 1) / 2 + 2.5 * k, py), 0.45, fc='#d4af37'))
    elif name.startswith('H'):
        ax.add_patch(Circle((px, py), 1.35, fc='white', ec='#d4af37', lw=2))
    lbl = name + (' (back side)' if side == 'B' else '')
    ax.text(px, py + (4.6 if not name.startswith('J2') else -4.6), lbl, ha='center', fontsize=8,
            color='white' if side != 'B' else '#ffe9a8')
    print(f'{name:8s} X {px:7.2f}  Y {py:6.2f}  {side}  {note}')

def dim(x0, y0, x1, y1, txt, off, horiz=True):
    if horiz:
        ax.annotate('', (x0, y0 + off), (x1, y0 + off), arrowprops=dict(arrowstyle='<->', lw=1))
        ax.plot([x0, x0], [y0, y0 + off], 'k-', lw=0.5); ax.plot([x1, x1], [y1, y0 + off], 'k-', lw=0.5)
        ax.text((x0 + x1) / 2, y0 + off + (0.6 if off > 0 else -2.4), txt, ha='center', fontsize=9)
    else:
        ax.annotate('', (x0 + off, y0), (x0 + off, y1), arrowprops=dict(arrowstyle='<->', lw=1))
        ax.plot([x0, x0 + off], [y0, y0], 'k-', lw=0.5); ax.plot([x1, x0 + off], [y1, y1], 'k-', lw=0.5)
        ax.text(x0 + off + (0.6 if off > 0 else -0.6), (y0 + y1) / 2, txt, va='center', ha='left' if off > 0 else 'right', fontsize=9, rotation=90)
dim(0, 0, W_, 0, f'{W_:.1f}', -5)
dim(0, 0, 0, H_, f'{H_:.1f}', -4, horiz=False)
for x0, x1 in POSTS:
    a, b = X(max(x0, PCB_X0)), X(x1)
    dim(a, H_, b, H_, f'{b - a:.1f}', 3)
dim(W_, 0, W_, Y(POST_TOP), f'{Y(POST_TOP):.1f}', 4, horiz=False)
ax.text(0, H_ + 9, f'Teco.Pi button board  {W_:.1f} x {H_:.1f} mm  (seen from the component side = from the front of the TV)',
        fontsize=13, weight='bold')
ax.text(0, H_ + 6.2, 'Stands upright 0.46 mm behind the front strip; switch plungers 7 mm in front of the board; '
        'notches for the 3 tray posts that hold the strip screws. Connectors on the back side.', fontsize=9)
ax.set_xlim(-8, W_ + 8); ax.set_ylim(-9, H_ + 11); ax.set_aspect('equal'); ax.axis('off')
fig.tight_layout(); fig.savefig(PNG, dpi=110, facecolor='white')

def L(a, b, layer): return f'0\nLINE\n8\n{layer}\n10\n{a[0]:.4f}\n20\n{a[1]:.4f}\n30\n0.0\n11\n{b[0]:.4f}\n21\n{b[1]:.4f}\n31\n0.0\n'
def C(c, r, layer): return f'0\nCIRCLE\n8\n{layer}\n10\n{c[0]:.4f}\n20\n{c[1]:.4f}\n30\n0.0\n40\n{r:.4f}\n'
ents = ''.join(L(pts[i], pts[(i + 1) % len(pts)], 'Edge.Cuts') for i in range(len(pts)))
ents += ''.join(C((X(x), Y(y)), 1.35, 'Holes') for x, y in MOUNT)
ents += ''.join(C((X(x), Y(y)), 0.3, 'Centres') for _, x, y, s, _ in AUX_PARTS if s != '-')
with open(DXF, 'w') as f:
    f.write('0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n' + ents + '0\nENDSEC\n0\nEOF\n')
