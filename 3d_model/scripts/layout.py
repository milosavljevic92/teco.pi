"""Interior layout of the 16:9 remix in the shell frame (x right seen from the front, y down,
z towards the back) + the main PCB outline. Shared by interior.py (3D) and pcbdraw.py (2D)."""
import numpy as np

D, DZ = 38.31, 34.5
H_ADD, Z_ADD, FRONT_ADD = 5.0, 13.2, 14.2          # v2 (make_v2.py): 5 mm taller below y 185, bezel window 13.2 forward, bezel front + strip 14.2
BC_Y = 682.90 - D
LIP_TOP = BC_Y - 410.65 + H_ADD             # 238.94: top of the back-cover bottom lip
TRAY_FLOOR = 257.62 + H_ADD                 # top of the tray floor
SHELL_BOTTOM = 238.22 + H_ADD
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
PCB_TOP = PI_COMP + STACK                   # 226.59
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

# Mean Well 5 V 3 A, 80 x 52 incl. terminals (height 28 assumed) in psu-cage.stl under the roof
# PSU lengthwise front-to-back, terminals to the back (z 74..79), slid in from the back between 2 rails
PSU = dict(x0=4.0, x1=56.0, y0=134.05, y1=162.05, z0=-1.0, z1=74.0, term=79.0)
CAGE_YB = 164.45                             # psu-rail-* print frame -> shell: y = CAGE_YB - z, z = y
PSU_RAIL = np.array([[1, 0, 0, 0], [0, 0, -1, CAGE_YB], [0, 1, 0, 0], [0, 0, 0, 1]], float)
FAN = dict(x0=PI_XC - 25, x1=PI_XC + 25, y0=PI_BACK - 55, y1=PI_BACK - 5, z0=PI_EDGE - 15, z1=PI_EDGE)  # over the Pi
SPK = dict(y=157.0, z=45.0, r=20.0, depth=20.0)   # 40 mm driver (18 deep) on a 2 mm spacer in a tube on the wall
# screen module 170 x 105 x 17 in the bezel; HDMI + USB-C plugs on its back ~25 mm above the bottom edge
MODULE = dict(x0=42.135 - 85, x1=42.135 + 85, y0=133.93, y1=238.93, z0=-28.18, z1=-11.18)
PLUG_Y = (MODULE['y1'] - 25 - 6, MODULE['y1'] - 25 + 6)   # plug body height band behind the module
PLUG_X = (-34.0, 59.0)                       # plugs clear the brace side bar and the Pi only inside this x range
C8 = dict(x0=-37.8, x1=-17.2, y0=233.14 + H_ADD + 8.6, y1=233.14 + H_ADD + 21.4, z0=70.0, z1=85.04)
# back panel (make_v2.py): RCA 3x3 + 1x3 on the main board's back edge, 2 card slots, I/O shield
RCA3 = dict(x0=33.6 - 19.5, x1=33.6 + 19.5, h=39.0, depth=30.0)
RCA1 = dict(x0=5.6 - 6.5, x1=5.6 + 6.5, h=39.0, depth=30.0)
CARD_XC = (-13.0, -30.7)                    # slot centres; card PCB left face at xc - 6.7, parts towards +x
CARD = dict(h=40.0, length=58.0, above_board=8.0)   # 2.54 right-angle male on the card into a 8.5 mm female header

def board_xy(x, z):
    """shell (x, z) -> board drawing coords (X from the left wall seen from the front, Y from the front edge)"""
    return x - PCB_X0, z - PCB_Z0

if __name__ == '__main__':
    W = PCB_X1 - PCB_X0; Dp = PCB_Z1 - PCB_Z0
    print(f'board {W:.1f} x {Dp:.1f} mm, top at shell y {PCB_TOP:.2f} ({TRAY_FLOOR - PCB_BOT:.1f} mm free under it to the tray floor)')
    print('port notch X %.1f..%.1f, Y from %.1f' % (*[v - PCB_X0 for v in NOTCH_PORTS[:2]], NOTCH_PORTS[2] - PCB_Z0))
    print('Pi holes', [tuple(round(v, 2) for v in board_xy(*h)) for h in PI_HOLES])
    print('header rows X', [round(r - PCB_X0, 2) for r in HDR_ROWS], 'Y', [round(z - PCB_Z0, 2) for z in HDR_Z])

