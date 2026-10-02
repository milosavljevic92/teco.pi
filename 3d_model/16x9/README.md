# Teco.Pi â€” Mini CRT, 7" 16:9

Remix of [Mini CRT](https://www.printables.com/model/1405096-mini-crt) by Xipher Design,
CC BY-NC 4.0 (non-commercial, attribution required).

Adapted for a 7" 1024x600 IPS panel (assumed standard 50-pin panel: 164.9 x 100 x 3.5 mm,
active area 154.21 x 85.92 mm). Sharp outer corners (Sony PVM style) instead of r=4 fillets.
Only as deep as a Raspberry Pi 3B lying flat needs.

Outer size: 186.5 W x 107.7 H (+ 21 mm tray below) x 97.6 D (+ 16 mm bezel in front).

## Parts (this folder)

| Part | Change |
|---|---|
| `top.stl` | 38.3 mm lower, 34.5 mm shallower. Sharp corners. Slot grilles D36 for 40 mm (1.5") speakers on both sides (centre y 180, z 45). RS-25-5 mount under the roof |
| `screen-bezel.stl` | 38.3 mm lower, sharp corners, window 153.6 x 85.3 mm with front chamfer, side guides for the 164.9 mm panel, panel layer of the pocket widened to 101.8 mm |
| `back-cover.stl` | 38.3 mm lower; old round grille and the big-fan holes (incl. their outer counterbores) closed; grille for a standard 50 mm fan (square holes in D46, 4x M3 at 40 mm, fan inside); inside flattened to a 3 mm plate there; windows for the Raspberry Pi 3B Ethernet + 2x USB (Pi upside down) |
| `bottom.stl` | Tray 34.5 mm shallower (screw holes still match the shell). 5 RCA holes and the small slot in the back wall closed; C8 (figure-8) mains inlet on the right as seen from the back |
| `x-brace.stl` | 38.3 mm lower; corner holes match the shell posts; middle side tabs removed |
| `front-plate.stl` | No volume wheel. Headphone jack Ă6.2 mm (3.5 mm panel jack, M6 nut) and IR receiver Ă6 mm at button height, pockets behind both |

Unchanged, from `../`: `back-clips`, `usb-c-brace`, `pwr-button`, `pwr-button-holder`,
`combined-buttons-sla`. Not used: `vol-wheel-sla`, `riser` (potentiometer), `spkr-horn`.

`assembly-section.png` is a side section through the middle of the assembly.

## Raspberry Pi 3B

Upside down: the GPIO header plugs down into the main board, USB + Ethernet hang below the PCB
(so they are mirrored on the back). Ports to the back, as far left as seen from the back as the back-clips allow
(x 59.6..115.6, PCB edge ~15 mm from the side wall, under the 40 mm speaker; 15 mm gap).
- ports raised 6 mm above the lowest possible position: lowest port edge shell y 226.6, window ~7.5 mm above the bottom edge of the back cover
- PCB (component side down) at shell y 210.6, i.e. 47.0 mm above the tray floor (257.6); 8.7 mm below the right speaker
- USB is 16 mm tall: the main board must end ~22 mm before the back cover (or have a cut-out)
  unless the header stack is â‰Ą 17 mm
- PCB edge against the back cover; the ports end 0.6â€“1 mm behind its outer face
- HDMI / micro-USB / audio face the centre of the case (~80 mm free)

## Main board

`main-pcb.png` (dimensioned top view) and `main-pcb-outline.dxf` (KiCad: import to Edge.Cuts,
mm; Pi holes on layer "Holes"). Interior renders: `interior.png` (`-rear`, `-top`).

- **175.5 x 85.0 mm**, origin = front-left corner seen from the front
- cut-out 53.3 x 18.0 at the back for the Pi ports, 13.5 x 14.0 at both back corners (back-clips)
- board top 11.0 mm below the Pi (standard 2x20 female header 8.5 mm + Pi header 2.5 mm),
  Pi standoffs M2.5 x 11 at (108.77, 5.0) (157.77, 5.0) (108.77, 63.0) (157.77, 63.0)
- female 2x20 header: rows X 156.53 (odd pins) / 159.08 (even), Y 9.87 .. 58.13, pin 1 at the front
- Pi Wi-Fi/BT chip antenna is above the pin-1 corner: no copper / parts under it (blue zone)
- port cut-out and the right back-corner cut-out are merged into one
- 34.4 mm free under the board (tray floor); the C8 inlet (230 V) is in the tray at the back left
- the 40 mm speakers end ~21 mm above the board; ~65 mm free elsewhere up to the PSU

## Fan

Standard 50 x 50 mm (10 or 15 mm thick) inside the back cover, M3 screws at 40 mm, directly above
the Pi (5 mm gap); grille D46 mm. Nothing else is behind it (PSU ends at z 70.5).

`preview.png` (`preview-front.png`, `preview-back.png`) is rendered from the STL files by `preview.py`.

## Power supply (Mean Well RS-25-5)

Under the roof, base plate against the roof, mesh down, lying across the case, terminals to the
right (x 80..94), body x 2..80, z 19.5..70.5. Two countersunk holes in the roof, 55 mm apart,
plus 2 mm guide rails. ~25 mm in front of it for the panel driver board.

Screws: **M3 x 6 countersunk** (ISO 10642). The roof is 3.55 mm, so 2.45 mm goes into the
PSU; the datasheet allows max 3 mm (M3 x 8 would hit the PCB).

## Mains inlet (C8, figure-8)

Back wall of the tray, right side seen from the back. Cut-out obround 20.6 x 12.8 mm
(part: 20.3 x 12.5), screw holes D3.5 at 28 mm, flange 35 x 15 lies on the flat outer face.
- right screw: through the 5.5 mm wall, M3 x 10 + nut
- left screw: blind 12.5 mm into the corner block (it would otherwise hit the vertical tray
  screw), M3 heat-set insert or a self-tapping screw

Live 230 V terminals are inside the tray: insulate them (heat-shrink / cover) and keep the
low-voltage board away from them.

## Panel orientation

The panel pocket is offset towards the top of the TV (as in the original). Mount the panel
with its wide border (FPC side, ~9.4 mm) at the TOP. With the wide border at the bottom it
does not fit.

## Regenerate / check

```
python make169.py ..\ .\
python fitcheck.py .
python assembly.py . assembly-section.png 42
```

Needs `trimesh manifold3d scipy networkx shapely matplotlib`. Dimensions are at the top of the
sections in `make169.py`.


