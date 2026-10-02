"""Interior layout of the 16:9 remix in the shell frame (x right seen from the front, y down,
z towards the back) + the main PCB outline. Shared by interior.py (3D) and pcbdraw.py (2D)."""
import numpy as np

D, DZ = 38.31, 34.5
BC_Y = 682.90 - D
LIP_TOP = BC_Y - 410.65                     # 233.94: top of the back-cover bottom lip
TRAY_FLOOR = 257.62                         # top of the tray floor
SHELL_BOTTOM = 238.22
WALL_L, WALL_R = -46.13, 130.40             # inner faces of the side walls
BACK_IN = 124.12 - DZ                       # inner face of the back-cover plate (89.62)

# Raspberry Pi 3B upside down, header into the main board
PI_XC = (-51.13 + 135.40) / 2 + 45.5
PORT_RAISE = 6.0
PI_COMP = LIP_TOP - 0.6 - 0.75 - PORT_RAISE - 16.0   # component side (down)
PI_BACK = PI_COMP - 1.4
PI_EDGE = 124.0 - DZ                        # port edge, 89.5
def pi_x(y): return PI_XC - 28.0 + y        # Pi y (0..56) -> shell x
def pi_z(x): return PI_EDGE - 85.0 + x      # Pi x (0..85) -> shell z
PI_HOLES = [(pi_x(y), pi_z(x)) for x in (3.5, 61.5) for y in (3.5, 52.5)]
HDR_ROWS = (pi_x(51.27), pi_x(53.81))       # inner row (odd pins 1,3..), outer row (even pins 2,4..)
HDR_Z = (pi_z(8.37), pi_z(56.63))           # pin 1 / 2 at the SD-card end (front)
PORTS = [('Ethernet', 2.25, 18.25, 13.5, 21.3, 2.5), ('USB', 22.4, 35.6, 16.0, 17.1, 2.1),
         ('USB', 40.4, 53.6, 16.0, 17.1, 2.1)]   # name, y0, y1, height, body length, overhang

# main board: standard 2x20 female header (8.5 mm) + Pi header plastic 2.5 mm
STACK = 11.0
PCB_T = 1.6
PCB_TOP = PI_COMP + STACK                   # 227.59
PCB_BOT = PCB_TOP + PCB_T
CLR = 0.5
PCB_X0, PCB_X1 = WALL_L + CLR, WALL_R - CLR  # -45.63 .. 129.90
PCB_Z0 = 3.0                                 # front: clear of the shell corner posts (z -5..2)
PCB_Z1 = BACK_IN - 1.6                       # 88.0
# cut-out under the Pi ports (they hang 16 mm, below the board top)
port_x0 = min(pi_x(p[1]) for p in PORTS) - 1.0
port_x1 = max(pi_x(p[2]) for p in PORTS) + 1.0
port_z0 = min(PI_EDGE + p[5] - p[4] for p in PORTS) - 0.7
NOTCH_PORTS = (port_x0, port_x1, port_z0)
# back corners: back-clips (14.5 x 21 x 9.5) hold the lower back-cover screws (x -41.7 / 125.8)
NOTCH_CORNER = 14.0                          # x from each side wall
NOTCH_CORNER_Z = 74.0

PSU = dict(x0=2.0, x1=80.0, term=94.0, y0=134.06, y1=162.06, z0=19.5, z1=70.5)
FAN = dict(x0=42.13 - 25, x1=42.13 + 25, y0=PI_BACK - 55, y1=PI_BACK - 5, z0=PI_EDGE - 15, z1=PI_EDGE)
SPK = dict(y=180.0, z=45.0, r=20.5, depth=22.0)   # 40 mm driver
PANEL = dict(x0=42.18 - 82.45, x1=42.18 + 82.45, y0=-301.82 + 436.05, y1=-201.82 + 436.05, z0=-14.98, z1=-11.48)
C8 = dict(x0=-37.8, x1=-17.2, y0=233.14 + 8.6, y1=233.14 + 21.4, z0=70.0, z1=85.04)

def board_xy(x, z):
    """shell (x, z) -> board drawing coords (X from the left wall seen from the front, Y from the front edge)"""
    return x - PCB_X0, z - PCB_Z0

if __name__ == '__main__':
    W = PCB_X1 - PCB_X0; Dp = PCB_Z1 - PCB_Z0
    print(f'board {W:.1f} x {Dp:.1f} mm, top at shell y {PCB_TOP:.2f} ({TRAY_FLOOR - PCB_BOT:.1f} mm free under it to the tray floor)')
    print('port notch X %.1f..%.1f, Y from %.1f' % (*[v - PCB_X0 for v in NOTCH_PORTS[:2]], NOTCH_PORTS[2] - PCB_Z0))
    print('Pi holes', [tuple(round(v, 2) for v in board_xy(*h)) for h in PI_HOLES])
    print('header rows X', [round(r - PCB_X0, 2) for r in HDR_ROWS], 'Y', [round(z - PCB_Z0, 2) for z in HDR_Z])

