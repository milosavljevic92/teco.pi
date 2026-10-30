#!/usr/bin/env python3
"""Teco.Pi: štampač u CUPS-u nudi samo A4/A5 (PPD bez ostalih formata).
iPhone/Mac sa US podešavanjem inače pošalje Letter, a štampač bez ekrana (npr. Xerox Phaser 3020) onda ćuti i čeka
taj papir. Kad je jedini ponuđen format A4, telefon šalje A4 i uklapa stranu.

sudo python3 cups-a4.py <ime štampača>
"""
import re
import sys
from pathlib import Path

KEEP = {'A4', 'A5'}
OPTS = ('PageSize', 'PageRegion', 'ImageableArea', 'PaperDimension')

name = re.sub(r'[^A-Za-z0-9_-]', '', sys.argv[1] if len(sys.argv) > 1 else '')
f = Path('/etc/cups/ppd/%s.ppd' % name)
lines = f.read_text(encoding='latin-1').splitlines(keepends=True)
out, skip = [], False
for ln in lines:
    if skip:   # višeredna vrednost (do zatvorenog navodnika / *End)
        if ln.startswith('*End') or ln.rstrip().endswith('"'):
            skip = False
        continue
    m = re.match(r'\*(%s) ([^/:\s]+)' % '|'.join(OPTS), ln)
    if m and m.group(2) not in KEEP:
        skip = ln.count('"') == 1   # vrednost se nastavlja u sledećim redovima
        continue
    m = re.match(r'\*Default(%s): (\S+)' % '|'.join(OPTS), ln)
    if m:
        ln = '*Default%s: A4\n' % m.group(1)
    out.append(ln)
f.write_text(''.join(out), encoding='latin-1')
print('A4/A5:', f)
