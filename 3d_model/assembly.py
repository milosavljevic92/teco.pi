"""side section (plane x = X) of the assembled 16:9 remix in the shell frame"""
import sys, numpy as np, trimesh, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

OUT, PNG = sys.argv[1], sys.argv[2]
X = float(sys.argv[3]) if len(sys.argv) > 3 else 42.0
D, DZ = 38.31, 34.5

def T(rows):
    return np.array(rows, float)

parts = {
    # name: (file, transform part -> shell frame, colour)
    'shell': ('top', np.eye(4), 'C1'),
    'bezel': ('screen-bezel', T([[1, 0, 0, 0], [0, 1, 0, 436.05], [0, 0, 1, -16.0], [0, 0, 0, 1]]), 'C0'),
    'x-brace': ('x-brace', T([[1, 0, 0, 0], [0, 1, 0, 643.15 - D], [0, 0, 1, -8.38], [0, 0, 0, 1]]), 'C2'),
    'back cover': ('back-cover', T([[1, 0, 0, 0], [0, -1, 0, 682.90 - D], [0, 0, -1, 110.04 - DZ], [0, 0, 0, 1]]), 'C3'),
    # tray: x same, tray y -> shell z (+38.51), tray z -> shell y (top -5.08 at shell 238.22, pointing down)
    'tray': ('bottom', T([[1, 0, 0, 0], [0, 0, -1, 238.22 - 5.08], [0, 1, 0, 38.51], [0, 0, 0, 1]]), 'C4'),
}
fig, ax = plt.subplots(figsize=(14, 10))
for name, (f, M, col) in parts.items():
    m = trimesh.load(f'{OUT}\\{f}.stl'); m.apply_transform(M)
    s = m.section(plane_origin=[X, 0, 0], plane_normal=[1, 0, 0])
    if s is None:
        continue
    for e in s.discrete:
        ax.fill(e[:, 2], e[:, 1], color=col, alpha=0.55, lw=0.6, ec=col)
    ax.plot([], [], color=col, lw=6, label=name)
pcb_comp = (682.90 - D - 410.65) - 0.6 - 0.75 - 6.0 - 16.0; pcb_back = pcb_comp - 1.4; edge = 124.0 - DZ
for (z0, z1, y0, y1, lab, col) in [
        (edge - 85, edge, pcb_back, pcb_comp, 'Pi PCB (upside down)', 'k'),
        (edge - 17, edge + 2.1, pcb_comp, pcb_comp + 16.0, 'USB', 'grey'),
        (edge - 15, edge, pcb_back - 55, pcb_back - 5, '50 mm fan', 'teal'),
        (19.5, 70.5, 134.06, 162.06, 'RS-25 PSU', 'purple')]:
    if lab:
        ax.add_patch(Rectangle((z0, y0), z1 - z0, y1 - y0, fill=False, ec=col, lw=1.5, hatch='//'))
        ax.text((z0 + z1) / 2, (y0 + y1) / 2, lab, ha='center', va='center', fontsize=9)
ax.set_aspect('equal'); ax.invert_yaxis(); ax.grid(True, alpha=0.3); ax.minorticks_on()
ax.set_xlabel('z (front -> back)'); ax.set_ylabel('y (top -> bottom)'); ax.legend(loc='upper right')
ax.set_title(f'assembly section at x = {X}')
fig.tight_layout(); fig.savefig(PNG, dpi=80)

