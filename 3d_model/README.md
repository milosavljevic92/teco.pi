# Teco.Pi — Mini CRT, 7" 16:9

Remix of [Mini CRT](https://www.printables.com/model/1405096-mini-crt) by Xipher Design,
CC BY-NC 4.0 (non-commercial, attribution required).

Adapted for a 7" 1024x600 IPS panel (assumed standard 50-pin panel: 164.9 x 100 x 3.5 mm,
active area 154.21 x 85.92 mm). Sharp outer corners (Sony PVM style) instead of r=4 fillets.
Only as deep as a Raspberry Pi 3B lying flat needs.

Outer size: 186.5 W x 112.7 H (+ 21 mm tray below) x 97.6 D (+ 30.2 mm bezel in front).

## v2 (`scripts/make_v2.py`, applied on top of `make169.py`)

- **Screen module** (measured): 170 x 105 x 17 aluminium case, border 7 mm left/right, 9 mm top/bottom
  (window 155.2 x 86.2 centred on it). 4 holes D5 on the back: hole edge 50 mm from the left/right side,
  3.6 mm and 90 mm from the bottom (seen from the front).
- **Bezel** (30.2 mm deep, 192.5 x 115.7): window plate 4 mm, raised 3 mm rim, sloped surround (window +10 mm per side left/right, +8 mm top/bottom over 7 mm depth), retro monitor look; the front 8 mm are 3 mm bigger than the case (left, right, top) with a 45 deg step back. Bezel front and bottom strip are 14.2 mm further forward than v1.
- Case **5 mm taller** (shell, bezel, back cover, brace stretched at shell y 185), bezel window and the
  bottom strip (tray front + front plate) further forward for the 17 mm module (see Bezel below).
- `x-brace.stl` is now the screen brace: open frame with 2 bars, **4x D4.4** on the screen holes
  (M4, or M3 + washer). The bottom is open for the screen's HDMI / USB-C plugs.
- **Plugs on the back of the screen** (~25 mm above its bottom edge) must be within shell x -34..59,
  i.e. **68..161 mm from the left edge of the screen seen from the back**. Further left (seen from the
  back) they hit the Pi: then use 90 deg adapters. ~40 mm free behind the screen there.
- Speakers: **40 mm 4 ohm 3 W** (frame D40, basket D34, magnet D18, 18 deep, soft surround) at y 157, z 45 (cards go under the left one); **round grilles**: 100 holes D2.2 in rings. Holder printed with the shell: 2 mm spacer ring (only the metal rim touches it, the surround stays free) + D40.4 tube 6 mm long, open at the back; push the speaker in, a drop of glue on the rim. Teardrop outside / bridged bore so it prints front face down without supports.
- **Slot vents** (rounded vertical slots): low in the right side wall (2 rows x 11, z 7..61; left wall: speaker only), a strip in the back cover above the
  cards / I/O, and the fan grille.
- **50 mm fan** now over the Pi (centre x 87.6, shell y 184.2), 4x M3 at 40 mm.
- **I/O shield** (`io-shield.stl`, 59.5 x 55 x 4.2 incl. the 1.2 mm flange; plate 55.1 x 50.6 in a
  55.5 x 51 window): pushed in from the inside like a PC I/O shield, 2 snap latches on the top edge
  (bottom edge first, push the top until it clicks), flange **glued** to the cover (superglue).
  RCA 3x3 (columns AV1 / AV2 / AV3, V+R+L each) + RCA 1x3 (OUT). RCA holes D9 at 13 mm pitch, rows
  7.5 / 20 / 32.5 mm above the main board: **check against your RCA blocks and reprint the shield if
  needed** (`RCA_*` in `make_v2.py`).
- **Card brackets**: window **15 x 38** per slot (bracket 14.6 x 37.6, flange 1.2 left/right and 2 mm
  top/bottom, 17.4 x 42 overall; ~14.6 x 33 usable, the snap latches top and bottom take ~2 mm each). From the inside with the card, flange **glued**.
  Card on the L-tab with **2x M3** (holes D3.4).
- **FM antenna**: SMA bulkhead straight in the back cover (D6.5 with a 5.9 flat, nut outside), upper right seen from the back.
- **Logo** (`assets/logo.png`, vectorised by `scripts/logo.py`) engraved 0.5 mm, 20 mm, on the left side wall, lower front corner (~3 mm above the bottom edge). **TECOMATIC** wordmark (`assets/tecomatic.png`) engraved 0.5 mm, 5 mm tall, left end of the button strip in line with the buttons.
- **Labels** engraved 0.6 mm: USB USB ETH, AV1 AV2 AV3 OUT (on the shield), EXP1 EXP2, FM, and
  AC250V next to the mains inlet. Recess 48 x 18 x 0.4 mm above the fan for a stick-on label.
- **2 expansion-card slots** right of the I/O (seen from the back), centres x -13.0 / -30.7:
  - card PCB **40 x 58 mm** (height x length), bottom edge **8 mm above the main board**: standard 2.54
    right-angle male header on the card (bottom edge, component side) into a standard 8.5 mm female
    header on the main board, row ~2.5-3 mm from the card face towards the components (check your
    header); ribbon cables for anything else. Bracket M3 holes: 5 mm from the card's back edge,
    10.6 / 32.6 mm above its bottom edge
  - PCB left face (seen from the front) at x -19.7 / -37.4, components to the right (+x), max ~12.5 mm tall
  - `later/card-bracket.stl` (print 2 when needed): blank back, drill/cut your connectors;
    L-tab with 2x M3 holes into the card, 5 mm from its back edge, 8 mm from the bracket ends

**Screws: M3 everywhere. M3 heat-set inserts (hole D4.0, M3 x 5.7) only in the shell (`top.stl`), 12**:
4 front posts (screen brace), 4 back corners (back cover: 2 upper posts + 2 lower bosses, the separate
back-clips are gone), 4 bottom lip holes (tray). Nothing in the covers / plates:
- screen brace corners: D3.4 + countersink from the front, **M3 x 8 countersunk** (flat under the screen)
- back cover: D3.4 through, **M3 x 10** (upper) / **M3 x 12** (lower, through the cover's inner rib)
- tray: D3.5 through, **M3 x 10**
- fan: M3 through the cover + nut; C8 inlet: M3 x 10 + nut / insert (see below)
- screen to brace: D4.4 (thread of the screen unknown: M4, or M3 + washer)
Main board top is now at shell y 226.59 (everything below y 185 moved 5 mm down). **Main board slider** in the shell: a 1.9 mm slot (1.6 mm PCB + 0.3) in each side wall, lips 2 mm thick reaching 2.5 mm in (2 mm grip per side), z 1.5..64; slide the board in from the back (cover off) until it stops at the front corner blocks. Keep a **3 mm strip along both side edges free of parts** (top and bottom) for z 0..64 of the board.

## Parts to print (all in `elements/`) - 7 files

| Part | Qty | Note |
|---|---|---|
| `top.stl` | 1 | shell incl. PSU rails, speaker tubes, back-cover bosses, logo; **print front face down** |
| `screen-bezel.stl` | 1 | bezel **and button strip in one part** (192.5 x 136.8 x 30.2); print front face down |
| `x-brace.stl` | 1 | screen brace |
| `back-cover.stl` | 1 | print outside face down |
| `bottom.stl` | 1 | tray |
| `io-shield.stl` | 1 | glued in from the inside |
| `buttons.stl` | 1 | all 7 caps (PWR cap orange) |
| `later/card-bracket.stl` | 2 | only when the expansion cards exist |

Button strip (part of `screen-bezel.stl`): from the left TECOMATIC,
headphone jack with symbol, buttons **A B < > + −** (labels engraved), IR (8 x 8 window with a 0.8 mm
frame lip, 11 x 11 recess behind it for a thin coloured transparent filter) with IR symbol, LED with a
bolt symbol, PWR. Its front is flush with the bezel front (17.1 mm further forward than v1).
## v1 parts (changes against the original)

| Part | Change against the original |
|---|---|
| `top.stl` | 38.3 mm lower, 34.5 mm shallower. Sharp corners. Slot grilles D36 for 40 mm (1.5") speakers on both sides (centre y 180, z 45). RS-25-5 mount under the roof |
| `screen-bezel.stl` | 38.3 mm lower, sharp corners, window 153.6 x 85.3 mm with front chamfer, side guides for the 164.9 mm panel, panel layer of the pocket widened to 101.8 mm |
| `back-cover.stl` | 38.3 mm lower; old round grille and the big-fan holes (incl. their outer counterbores) closed; grille for a standard 50 mm fan (square holes in D46, 4x M3 at 40 mm, fan inside); inside flattened to a 3 mm plate there; windows for the Raspberry Pi 3B Ethernet + 2x USB (Pi upside down) |
| `bottom.stl` | Tray 34.5 mm shallower (screw holes still match the shell). 5 RCA holes and the small slot in the back wall closed; C8 (figure-8) mains inlet on the right as seen from the back |
| `x-brace.stl` | 38.3 mm lower; corner holes match the shell posts; middle side tabs removed |
| `front-plate.stl` | No volume wheel. From left: headphone jack Ø6.2 (M6), IR Ø6, 6 buttons (2 new + 4 original holes, 13 mm pitch), LED Ø3.2, PWR notch with "PWR" engraved below it; pockets behind, 2 bosses for the button board |
| `buttons.stl` | 6 small caps (8 x 8 mm, 1 mm proud of the front) + PWR cap (print orange), face down |
| `back-clips.stl` | unchanged original (`usb-c-brace` is not used in v2) |

Not used from the original: `vol-wheel-sla`, `riser` (potentiometer), `spkr-horn`, `combined-buttons-sla`,
`pwr-button`, `pwr-button-holder` (replaced by `buttons.stl` + the button board).

## Button board (aux PCB)

`aux-pcb.png` (positions, seen from the component side = from the front of the TV) and
`aux-pcb-outline.dxf` (KiCad Edge.Cuts, mm; holes on "Holes", part centres on "Centres").
Generated by `auxpcb.py` from `frontpanel.py`; close-up: `preview-strip.png`.

- **158.0 x 17.9 mm**, upright, 0.46 mm behind the front strip, held by 2x M2.5 self-tapping screws
  into bosses on the strip (H1, H2)
- 3 notches (9.7 / 11 / 11 x 6.8 mm) at the bottom for the tray posts that hold the strip screws
- front side: SW1–SW6 + SW7 (PWR) tact switches **6x6x7 mm** THT (plungers touch the caps),
  D1 3 mm LED (body ~6 mm off the board), U1 IR receiver VS1838B/TSOP (leads bent 90°),
  J1 3.5 mm jack **PJ-392 style** (M6 nut on the front, body ≤ 11 mm deep, pins to the board)
- back side: J2 JST-XH 12 pin (3V3, GND, SW1..SW7, LED, IR, DHT), J3 JST-XH 3 pin (L, R, AGND)
- exact centres: printed by `python auxpcb.py aux-pcb.png aux-pcb-outline.dxf`

## Raspberry Pi 3B

Upside down: the GPIO header plugs down into the main board, USB + Ethernet hang below the PCB
(so they are mirrored on the back). Ports to the back, as far left (seen from the back) as the
back-cover screw bosses allow: x 59.6..115.6, PCB edge ~15 mm from the side wall.
- ports raised 6 mm: lowest port edge at shell y 231.6 (v2), window ~7.5 mm above the bottom edge
  of the back cover
- PCB (component side down) at shell y 215.6 (v2), 47.0 mm above the tray floor (262.6)
- USB is 16 mm tall: the main board has a cut-out under the ports
- PCB edge against the back cover; the ports end 0.6–1 mm behind its outer face
- HDMI / micro-USB / audio face the centre of the case (~80 mm free)

## Main board

`main-pcb.png` (dimensioned top view) and `main-pcb-outline.dxf` (KiCad: import to Edge.Cuts,
mm; Pi holes on layer "Holes"). Interior renders: `interior.png` (`-rear`, `-top`).

- **175.5 x 85.0 mm**, origin = front-left corner seen from the front
- cut-out for the Pi ports merged with the right back-corner cut-out; 13.5 x 14.0 at the left
  back corner (back-cover screw bosses)
- board top 11.0 mm below the Pi (standard 2x20 female header 8.5 mm + Pi header 2.5 mm),
  Pi standoffs M2.5 x 11 at (108.77, 5.0) (157.77, 5.0) (108.77, 63.0) (157.77, 63.0)
- female 2x20 header: rows X 156.53 (odd pins) / 159.08 (even), Y 9.87 .. 58.13, pin 1 at the front
- the Pi Wi-Fi/BT chip antenna is above the pin-1 corner: no copper / parts under it (blue zone;
  position from memory, check on the real Pi)
- 34.4 mm free under the board (tray floor); the C8 inlet (230 V) is in the tray at the back left
- v2: RCA 3x3 block (x 14.1..53.1) and RCA 1x3 (x -0.9..12.1, shell x) on the back edge, card
  headers under the cards at x -19.7 / -37.4; the drawing does not show these yet
- ~65 mm free up to the PSU

## Fan

Standard 50 x 50 mm (10 or 15 mm thick) inside the back cover, M3 screws at 40 mm, directly above
the Pi (5 mm gap); slot grille 44 x 44 mm. Nothing else is behind it (PSU ends at z 70.5).

## Power supply (Mean Well 5 V 3 A, v2)

80 x 52 x 28 mm (80 incl. terminals). Lies against the roof **lengthwise front-to-back, terminals
to the back**: x 4..56, z -1..79 (terminals ~z 74..79, ~10 mm behind them for the wires), left of the
fan. Slid in **from the back** (back cover off), no screws:
- 2 rails **printed with the shell** (`top.stl`): 3 mm web from the roof down, 45 deg root gusset,
  5 mm x 3 mm ledge under the PSU, front stop (PSU front ~3.7 mm behind the screen brace), diamond
  windows in the webs for cooling. They run front-to-back, so with the shell printed **front face
  down** they grow straight up from the bed (no supports).
- The back cover has 2 fingers (top corners of the PSU back face, clear of the terminals) that keep
  the PSU against the front stops.
- Front and back faces (honeycomb) stay open; the top lies against the roof.
## Mains inlet (C8, figure-8)

Back wall of the tray, right side seen from the back. Cut-out obround 20.6 x 12.8 mm
(part: 20.3 x 12.5), screw holes Ø3.5 at 28 mm, flange 35 x 15 lies on the flat outer face.
- right screw: through the 5.5 mm wall, M3 x 10 + nut
- left screw: blind 12.5 mm into the corner block (it would otherwise hit the vertical tray
  screw), M3 heat-set insert or a self-tapping screw

Live 230 V terminals are inside the tray: insulate them (heat-shrink / cover) and keep the
low-voltage board away from them.

## Screen mounting (v2)

Bezel face down, screen in (window centred on it, side guides), brace on its back, 4 screws through
the brace into the screen, then the shell; the brace corners are screwed to the shell posts as before.

## Images

- `preview.png` (`preview-front.png`, `preview-back.png`): outside, rendered by `preview.py`
- `interior.png` (`interior-rear.png`, `interior-top.png`): inside, by `interior.py`
- `assembly-section.png`: side section, by `assembly.py`
- `main-pcb.png`: main board, by `pcbdraw.py`

## Regenerate / check

The original STL files are not kept in the repo. To regenerate, download the Mini CRT files from
Printables into a folder and run:

```
python scripts/make169.py <folder with the original STLs> <tmp>
python scripts/make_v2.py <tmp> elements
python scripts/fitcheck.py <tmp>              (v1 fit check)
python scripts/preview.py elements renders/preview.png
python scripts/interior.py elements renders/interior.png
python scripts/assembly.py elements renders/assembly-section.png
python scripts/pcbdraw.py renders/main-pcb.png main-pcb-outline.dxf
python scripts/auxpcb.py renders/aux-pcb.png aux-pcb-outline.dxf
```

Run from this folder. Layout: `elements/` printable STL parts, `renders/` images, `scripts/` the
Python scripts (they import each other from their own folder, so they stay together).

`make169.py` calls `frontpanel.py` for the front strip v2 and `buttons.stl`.

`fitcheck.py`, `preview.py`, `interior.py` and `assembly.py` read the STL files from the folder
given as the first argument (`elements`). Needs `trimesh manifold3d scipy networkx shapely matplotlib pillow`.
Dimensions are at the top of the sections in `scripts/make169.py` and in `scripts/layout.py`.
