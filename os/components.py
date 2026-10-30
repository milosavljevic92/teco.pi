"""Teco.Pi Alati → Komponente: tranzistori (oznaka ili SMD kod → tip, kućište, raspored nožica, granice) i induktivnosti
(prstenovi u boji ili SMD/RKM kod → vrednost). Isti modul koriste server (stranica) i prikaz na TV-u (tools/*_tv.py).

Nožice: redom 1, 2, 3 — TO-92 gledano na ravnu stranu (nožice nadole, sleva nadesno), TO-220/TO-126 sa prednje strane
(natpis ka tebi), SOT-23 odozgo: 1 levo dole, 2 desno dole, 3 gore. TO-3: B i E su nožice, kolektor je kućište.
"""
import re

# naziv: (vrsta, kućište, nožice, granice, komplement, napomena)
#   vrsta: NPN, PNP, NPN-D (Darlington), PNP-D, N-MOS, P-MOS
TR = {
    '2N3904': ('NPN', 'TO-92', 'EBC', '40 V · 200 mA · hFE 100–300', '2N3906', 'opšta namena, prekidač'),
    '2N3906': ('PNP', 'TO-92', 'EBC', '40 V · 200 mA · hFE 100–300', '2N3904', 'opšta namena'),
    '2N2222A': ('NPN', 'TO-92', 'EBC', '40 V · 600 mA · hFE 100–300', '2N2907A', 'PN2222A; metalna verzija TO-18'),
    'PN2222A': ('NPN', 'TO-92', 'EBC', '40 V · 600 mA · hFE 100–300', 'PN2907A', 'prekidač srednje struje'),
    '2N2907A': ('PNP', 'TO-92', 'EBC', '60 V · 600 mA · hFE 100–300', '2N2222A', ''),
    'PN2907A': ('PNP', 'TO-92', 'EBC', '60 V · 600 mA · hFE 100–300', 'PN2222A', ''),
    'BC547': ('NPN', 'TO-92', 'CBE', '45 V · 100 mA · hFE 110–800', 'BC557', 'A/B/C = grupa pojačanja'),
    'BC548': ('NPN', 'TO-92', 'CBE', '30 V · 100 mA · hFE 110–800', 'BC558', ''),
    'BC549': ('NPN', 'TO-92', 'CBE', '30 V · 100 mA · nizak šum', 'BC559', ''),
    'BC550': ('NPN', 'TO-92', 'CBE', '45 V · 100 mA · nizak šum', 'BC560', ''),
    'BC557': ('PNP', 'TO-92', 'CBE', '45 V · 100 mA · hFE 125–800', 'BC547', ''),
    'BC558': ('PNP', 'TO-92', 'CBE', '30 V · 100 mA', 'BC548', ''),
    'BC559': ('PNP', 'TO-92', 'CBE', '30 V · 100 mA · nizak šum', 'BC549', ''),
    'BC560': ('PNP', 'TO-92', 'CBE', '45 V · 100 mA · nizak šum', 'BC550', ''),
    'BC337': ('NPN', 'TO-92', 'CBE', '45 V · 800 mA · hFE 100–630', 'BC327', 'prekidač za relej, LED trake'),
    'BC327': ('PNP', 'TO-92', 'CBE', '45 V · 800 mA · hFE 100–630', 'BC337', ''),
    'S8050': ('NPN', 'TO-92', 'EBC', '25 V · 500 mA · hFE 85–300', 'S8550', 'čest u kineskim modulima'),
    'S8550': ('PNP', 'TO-92', 'EBC', '25 V · 500 mA · hFE 85–300', 'S8050', ''),
    'S9013': ('NPN', 'TO-92', 'EBC', '25 V · 500 mA', 'S9012', ''),
    'S9012': ('PNP', 'TO-92', 'EBC', '25 V · 500 mA', 'S9013', ''),
    'S9014': ('NPN', 'TO-92', 'EBC', '45 V · 100 mA · nizak šum', 'S9015', ''),
    'S9015': ('PNP', 'TO-92', 'EBC', '45 V · 100 mA', 'S9014', ''),
    'S9018': ('NPN', 'TO-92', 'EBC', '15 V · 50 mA · RF do ~1 GHz', '', 'VF pojačavač'),
    '2SC1815': ('NPN', 'TO-92', 'ECB', '50 V · 150 mA · hFE 70–700', '2SA1015', 'oznaka na telu: C1815'),
    '2SA1015': ('PNP', 'TO-92', 'ECB', '50 V · 150 mA · hFE 70–400', '2SC1815', 'oznaka na telu: A1015'),
    'BD135': ('NPN', 'TO-126', 'ECB', '45 V · 1,5 A · 12,5 W', 'BD136', ''),
    'BD136': ('PNP', 'TO-126', 'ECB', '45 V · 1,5 A · 12,5 W', 'BD135', ''),
    'BD139': ('NPN', 'TO-126', 'ECB', '80 V · 1,5 A · 12,5 W', 'BD140', 'drajver, mali pojačavači'),
    'BD140': ('PNP', 'TO-126', 'ECB', '80 V · 1,5 A · 12,5 W', 'BD139', ''),
    'TIP31C': ('NPN', 'TO-220', 'BCE', '100 V · 3 A · 40 W', 'TIP32C', ''),
    'TIP32C': ('PNP', 'TO-220', 'BCE', '100 V · 3 A · 40 W', 'TIP31C', ''),
    'TIP41C': ('NPN', 'TO-220', 'BCE', '100 V · 6 A · 65 W', 'TIP42C', ''),
    'TIP42C': ('PNP', 'TO-220', 'BCE', '100 V · 6 A · 65 W', 'TIP41C', ''),
    'TIP120': ('NPN-D', 'TO-220', 'BCE', '60 V · 5 A · hFE 1000 (Darlington)', 'TIP125', 'motori, solenoidi'),
    'TIP122': ('NPN-D', 'TO-220', 'BCE', '100 V · 5 A · hFE 1000 (Darlington)', 'TIP127', ''),
    'TIP125': ('PNP-D', 'TO-220', 'BCE', '60 V · 5 A · Darlington', 'TIP120', ''),
    'TIP127': ('PNP-D', 'TO-220', 'BCE', '100 V · 5 A · Darlington', 'TIP122', ''),
    '2N3055': ('NPN', 'TO-3', 'BE', '60 V · 15 A · 115 W', 'MJ2955', 'kolektor = metalno kućište'),
    'MJ2955': ('PNP', 'TO-3', 'BE', '60 V · 15 A · 115 W', '2N3055', 'kolektor = metalno kućište'),
    'BS170': ('N-MOS', 'TO-92', 'DGS', '60 V · 500 mA · 5 Ω', 'BS250', 'logički nivo ~2 V'),
    '2N7000': ('N-MOS', 'TO-92', 'SGD', '60 V · 200 mA · 5 Ω', '', 'logički nivo ~2 V'),
    'IRF540N': ('N-MOS', 'TO-220', 'GDS', '100 V · 33 A · 44 mΩ', 'IRF9540N', 'Vgs 10 V za puno uključenje'),
    'IRF520N': ('N-MOS', 'TO-220', 'GDS', '100 V · 9,7 A · 200 mΩ', 'IRF9520', 'nije logički nivo (Arduino slabo)'),
    'IRFZ44N': ('N-MOS', 'TO-220', 'GDS', '55 V · 49 A · 17,5 mΩ', '', 'Vgs 10 V'),
    'IRLZ44N': ('N-MOS', 'TO-220', 'GDS', '55 V · 47 A · 22 mΩ', '', 'logički nivo: radi sa 5 V (Arduino)'),
    'IRLZ34N': ('N-MOS', 'TO-220', 'GDS', '55 V · 30 A · 35 mΩ', '', 'logički nivo'),
    'IRF3205': ('N-MOS', 'TO-220', 'GDS', '55 V · 110 A · 8 mΩ', '', 'Vgs 10 V'),
    'IRF9540N': ('P-MOS', 'TO-220', 'GDS', '−100 V · −23 A · 117 mΩ', 'IRF540N', ''),
    'IRF4905': ('P-MOS', 'TO-220', 'GDS', '−55 V · −74 A · 20 mΩ', '', 'prekidač na plus strani'),
    'MMBT3904': ('NPN', 'SOT-23', 'BEC', '40 V · 200 mA', 'MMBT3906', 'SMD 2N3904, kod 1AM'),
    'MMBT3906': ('PNP', 'SOT-23', 'BEC', '40 V · 200 mA', 'MMBT3904', 'SMD 2N3906, kod 2A'),
    'MMBT2222A': ('NPN', 'SOT-23', 'BEC', '40 V · 600 mA', 'MMBT2907A', 'kod 1P'),
    'MMBT2907A': ('PNP', 'SOT-23', 'BEC', '60 V · 600 mA', 'MMBT2222A', 'kod 2F'),
    'MMBTA42': ('NPN', 'SOT-23', 'BEC', '300 V · 500 mA · visoki napon', 'MMBTA92', 'kod 1D'),
    'MMBTA92': ('PNP', 'SOT-23', 'BEC', '300 V · 500 mA · visoki napon', 'MMBTA42', 'kod 2D'),
    'BC846': ('NPN', 'SOT-23', 'BEC', '65 V · 100 mA', 'BC856', 'SMD BC546'),
    'BC847': ('NPN', 'SOT-23', 'BEC', '45 V · 100 mA', 'BC857', 'SMD BC547'),
    'BC848': ('NPN', 'SOT-23', 'BEC', '30 V · 100 mA', 'BC858', 'SMD BC548'),
    'BC856': ('PNP', 'SOT-23', 'BEC', '65 V · 100 mA', 'BC846', ''),
    'BC857': ('PNP', 'SOT-23', 'BEC', '45 V · 100 mA', 'BC847', ''),
    'BC858': ('PNP', 'SOT-23', 'BEC', '30 V · 100 mA', 'BC848', ''),
    'BC817': ('NPN', 'SOT-23', 'BEC', '45 V · 500 mA', 'BC807', 'SMD BC337'),
    'BC807': ('PNP', 'SOT-23', 'BEC', '45 V · 500 mA', 'BC817', 'SMD BC327'),
    'S8050-SMD': ('NPN', 'SOT-23', 'BEC', '25 V · 500 mA', 'S8550-SMD', 'kod J3Y'),
    'S8550-SMD': ('PNP', 'SOT-23', 'BEC', '25 V · 500 mA', 'S8050-SMD', 'kod 2TY'),
    'BSS138': ('N-MOS', 'SOT-23', 'GSD', '50 V · 220 mA · 3,5 Ω', '', 'logički nivo, I2C nivo-konverteri; kod J1/SS'),
    '2N7002': ('N-MOS', 'SOT-23', 'GSD', '60 V · 115 mA · 7,5 Ω', '', 'SMD 2N7000; kod 702/7002'),
    'AO3400': ('N-MOS', 'SOT-23', 'GSD', '30 V · 5,8 A · 28 mΩ', 'AO3401', 'logički nivo (2,5 V)'),
    'AO3401': ('P-MOS', 'SOT-23', 'GSD', '−30 V · −4 A · 50 mΩ', 'AO3400', ''),
}
# SMD kod (natpis na SOT-23) -> mogući tranzistori (isti kod koriste i drugi proizvođači za druge delove)
SMD = {
    '1AM': ['MMBT3904'], '1A': ['BC846A', 'MMBT3904'], '1B': ['BC846B'], '1E': ['BC847A'], '1F': ['BC847B'], '1G': ['BC847C'],
    '1J': ['BC848A'], '1K': ['BC848B'], '1L': ['BC848C'], '1P': ['MMBT2222A'], '1D': ['MMBTA42'],
    '2A': ['MMBT3906'], '2F': ['MMBT2907A'], '2D': ['MMBTA92'],
    '3A': ['BC856A'], '3B': ['BC856B'], '3E': ['BC857A'], '3F': ['BC857B'], '3G': ['BC857C'], '3J': ['BC858A'], '3K': ['BC858B'], '3L': ['BC858C'],
    '5A': ['BC807-16'], '5B': ['BC807-25'], '5C': ['BC807-40'], '6A': ['BC817-16'], '6B': ['BC817-25'], '6C': ['BC817-40'],
    'J3Y': ['S8050-SMD'], '2TY': ['S8550-SMD'], 'J1': ['BSS138'], 'SS': ['BSS138'], '702': ['2N7002'], '7002': ['2N7002'],
    'A0': ['AO3400'], 'A1': ['AO3401'],
}
PIN_N = {'E': 'emiter', 'B': 'baza', 'C': 'kolektor', 'G': 'gejt', 'D': 'drejn', 'S': 'sors'}
KIND_N = {'NPN': 'NPN bipolarni', 'PNP': 'PNP bipolarni', 'NPN-D': 'NPN Darlington', 'PNP-D': 'PNP Darlington', 'N-MOS': 'N-kanalni MOSFET', 'P-MOS': 'P-kanalni MOSFET'}
TRAN_EX = ['BC547', '2N3904', 'S8050', 'BD139', 'TIP120', 'IRLZ44N', '2N7000', '1AM', 'J3Y', 'BSS138']


def _base(name):
    """BC547B / BC547-B / C1815 / A1015 / MMBT3904LT1 / IRF540NPBF -> ključ u katalogu."""
    if name.upper().strip() in TR:   # S8050-SMD
        return name.upper().strip()
    n = re.sub(r'[\s\-]', '', name.upper())
    if n in TR:
        return n
    for pre in ('', '2S'):   # C1815 -> 2SC1815
        if pre + n in TR:
            return pre + n
    n2 = re.sub(r'(PBF|LT1G?|LT3G?|TA|BU|G)$', '', n)
    if n2 in TR:
        return n2
    m = re.match(r'^(BC\d{3}|BC8[0-9]{2}|2N\d{4}A?|S\d{4})[ABCL]?(\d{2})?$', n)
    if m and m.group(1) in TR:
        return m.group(1)
    m = re.match(r'^(BC8\d\d)-?\d{2}$', n)   # BC817-25 -> BC817
    if m and m.group(1) in TR:
        return m.group(1)
    return None


def tran(q):
    """Oznaka ili SMD kod -> {'q', 'name', 'kind', 'kind_n', 'pkg', 'pins': [(br, slovo, ime)], 'spec', 'comp', 'note', 'cands', 'smd'}."""
    q = (q or '').strip()
    r = {'q': q, 'name': None, 'cands': [], 'smd': None}
    if not q:
        return r
    up = q.upper().replace(' ', '')
    names = []
    if up in SMD:   # SMD natpis (1AM, J3Y…)
        r['smd'] = up
        names = SMD[up]
    elif len(up) >= 2 and up[:2] in SMD and re.fullmatch(r'[A-Z0-9]{2}[A-Z]?\d?', up) and not _base(up):   # A0xx, 1Ax… sa slovom datuma
        r['smd'] = up[:2]
        names = SMD[up[:2]]
    else:
        names = [q]
    found = []
    for n in names:
        b = _base(n)
        if b and b not in [f[1] for f in found]:
            found.append((n.upper(), b))
    if not found:   # deo naziva: predlozi
        r['cands'] = [k for k in TR if up and up in k][:8]
        return r
    show, key = found[0]
    kind, pkg, pins, spec, comp, note = TR[key]
    r.update(name=show, key=key, kind=kind, kind_n=KIND_N[kind], pkg=pkg, spec=spec, comp=comp, note=note,
             pins=[(i + 1, p, PIN_N[p]) for i, p in enumerate(pins)] + ([(0, 'C', 'kolektor (kućište)')] if pkg == 'TO-3' else []),
             cands=[f[0] for f in found[1:]])
    return r


# ---------- induktivnosti
COLORS = ['black', 'brown', 'red', 'orange', 'yellow', 'green', 'blue', 'violet', 'grey', 'white', 'gold', 'silver']
COL_N = {'black': 'crna', 'brown': 'braon', 'red': 'crvena', 'orange': 'narandžasta', 'yellow': 'žuta', 'green': 'zelena', 'blue': 'plava',
         'violet': 'ljubičasta', 'grey': 'siva', 'white': 'bela', 'gold': 'zlatna', 'silver': 'srebrna', 'none': 'bez'}
COL_RGB = {'black': (20, 20, 20), 'brown': (120, 66, 18), 'red': (220, 38, 38), 'orange': (249, 115, 22), 'yellow': (250, 204, 21),
           'green': (22, 163, 74), 'blue': (37, 99, 235), 'violet': (147, 51, 234), 'grey': (148, 163, 184), 'white': (245, 245, 245),
           'gold': (212, 175, 55), 'silver': (192, 192, 192), 'none': None}
DIGIT = {c: i for i, c in enumerate(COLORS[:10])}
MULT = dict(DIGIT, gold=-1, silver=-2)
TOL = {'black': '±20%', 'brown': '±1%', 'red': '±2%', 'orange': '±3%', 'yellow': '±4%', 'gold': '±5%', 'silver': '±10%', 'none': '±20%'}
TOL_L = {'F': '±1%', 'G': '±2%', 'H': '±3%', 'J': '±5%', 'K': '±10%', 'L': '±15%', 'M': '±20%', 'N': '±30%'}
IND_EX = {'smd': ['4R7', '100', '101', '221', 'R47', '2N2', '47N', '1R0M', '330K'], 'bands': [['yellow', 'violet', 'black', 'gold'], ['brown', 'black', 'red', 'silver']]}


def g(v):
    return ('%g' % round(v, 4)).replace('.', ',')


def fmt_uh(uh):
    if uh is None:
        return '?', ''
    main = g(uh / 1000) + ' mH' if uh >= 1000 else g(uh) + ' µH' if uh >= 1 else g(uh * 1000) + ' nH'
    return main, '%s nH = %s µH = %s mH' % (g(uh * 1000), g(uh), g(uh / 1000))


def ind(cfg):
    """{'mode': 'bands'|'smd', 'bands': [4 boje], 'smd': 'kod'} -> {'uh', 'main', 'all3', 'tol', 'desc'}."""
    r = {'uh': None, 'tol': None, 'desc': 'nepoznata oznaka'}
    if cfg.get('mode') == 'bands':
        b = list(cfg.get('bands') or [])[:4] + ['none'] * 4
        if b[0] in DIGIT and b[1] in DIGIT and b[2] in MULT:
            r['uh'] = (DIGIT[b[0]] * 10 + DIGIT[b[1]]) * 10 ** MULT[b[2]]
            r['tol'] = TOL.get(b[3])
            r['desc'] = '%s-%s × 10^%d µH (%s, %s, %s)' % (DIGIT[b[0]], DIGIT[b[1]], MULT[b[2]], COL_N[b[0]], COL_N[b[1]], COL_N[b[2]])
    else:
        s = re.sub(r'\s', '', str(cfg.get('smd') or '')).upper().replace('UH', 'U').replace('µ', 'U')
        if len(s) > 2 and s[-1] in 'FGJKLM' and re.fullmatch(r'\d{3}|\d*R\d*|\d*N\d*', s[:-1]):   # slovo tolerancije na kraju (1R0M, 330K)
            r['tol'], s = TOL_L[s[-1]], s[:-1]
        m = re.fullmatch(r'(\d*)R(\d*)', s)   # RKM u µH: 4R7 = 4,7 µH, R47 = 0,47 µH
        if m and (m.group(1) or m.group(2)):
            r['uh'] = float((m.group(1) or '0') + '.' + (m.group(2) or '0'))
            r['desc'] = 'R je decimalni zarez, vrednost u µH'
        elif re.fullmatch(r'(\d*)N(\d*)', s) and s != 'N':   # nH: 2N2 = 2,2 nH, 47N = 47 nH
            m = re.fullmatch(r'(\d*)N(\d*)', s)
            r['uh'] = float((m.group(1) or '0') + '.' + (m.group(2) or '0')) / 1000
            r['desc'] = 'N je decimalni zarez, vrednost u nH'
        elif re.fullmatch(r'\d{3}', s):   # 3 cifre: dve značajne × 10^treća µH (101 = 100 µH)
            r['uh'] = int(s[:2]) * 10 ** int(s[2])
            r['desc'] = '%s × 10^%s µH' % (s[:2], s[2])
        elif re.fullmatch(r'\d+(\.\d+)?(U|M)?', s.replace(',', '.')):   # napisano: 10U, 4.7, 1M (mH)
            v = float(re.sub(r'[UM]', '', s.replace(',', '.')))
            r['uh'] = v * 1000 if s.endswith('M') else v
            r['desc'] = 'vrednost napisana direktno' + (' (mH)' if s.endswith('M') else ' (µH)')
    r['main'], r['all3'] = fmt_uh(r['uh'])
    return r
