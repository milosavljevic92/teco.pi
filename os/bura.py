"""BURAscale (Tecomatic vaga) preko USB virtuelnog COM porta — protokol v4: 115200 8N1, redovi CR LF.
Red mase: STATUS,MOD,±MASA,JEDINICA,OZNAKE[,ADC]*XX (XX = XOR svih znakova pre '*', hex), npr. ST,GS,+00.000,kg,Z*70
Čitanje ide kroz event loop (add_reader), bez niti; svaka promena mase odmah poziva on_change (server je šalje dalje).
"""
import asyncio
import glob
import time

ST_TXT = {'ST': 'stabilno', 'US': 'nestabilno', 'OL': 'preopterećenje', 'UL': 'ispod nule', 'ER': 'greška'}


def ports():
    return sorted(glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*'))


def checksum_ok(line):
    if '*' not in line:
        return True   # odgovori na komande (OK:…, CFG,…) nemaju kontrolni zbir
    body, cs = line.rsplit('*', 1)
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        return x == int(cs.strip()[:2], 16)
    except ValueError:
        return False


INF_KEYS = 'VN MD FW YR SN WB MC FC AD SP CK PR CF CC GC MT LK EE AL EV RS FT FR UP'.split()
CFG_KEYS = 'U DP D MX MN RG D2 M2 D3 M3 SZ ZR ZT RD SP FM SB SS MB CL TC TS TZ IV CS ID OM T1 T2 LK'.split()


def kv(s, names=()):
    """'A=1;B=2' -> {'A': '1', 'B': '2'}; polja bez '=' dobijaju ime po redosledu iz protokola (names)."""
    out = {}
    for i, p in enumerate(s.split(';')):
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.strip().upper()] = v.strip()
        elif p.strip():
            out[names[i] if i < len(names) else str(i)] = p.strip()
    return out


class Bura:
    def __init__(self, on_change):
        self.on_change = on_change
        self.ser = None
        self.buf = b''
        self.dev = None
        self.err = None
        self.m = {}          # poslednji red mase
        self.info, self.cfg = {}, {}
        self.last_rx = 0
        self.last_tx = 0
        self.reply = ''      # poslednji odgovor na komandu (OK:/E:)
        self.t_mass, self.gaps, self.other = 0, [], []
        self.raw_inf = self.raw_cfg = ''

    # ---------- port
    def open(self, dev):
        import serial
        self.close()
        try:
            s = serial.Serial(dev, 115200, timeout=0)
        except (serial.SerialException, OSError) as e:
            self.err = str(e).split(':')[-1].strip() or 'port se ne otvara'
            return False
        self.ser, self.dev, self.err, self.buf = s, dev, None, b''
        asyncio.get_running_loop().add_reader(s.fileno(), self._read)
        self.last_rx = time.time()
        self.t_ask = 0
        self.send('ALOHA')
        self.send('TIME %d' % int(time.time()))
        return True

    def ask(self):
        """GET INFO / GET CONFIG tek kad vaga već šalje masu (odmah po otvaranju porta ne odgovara); ponavlja na 3 s dok ne stignu."""
        if self.ser is None or not self.m or (self.info and self.cfg) or time.time() - getattr(self, 't_ask', 0) < 3:
            return
        self.t_ask = time.time()
        if not self.info:
            self.send('GET INFO')
        if not self.cfg:
            self.send('GET CONFIG')

    def close(self):
        if self.ser is None:
            return
        try:
            self.send('BYE')
            asyncio.get_running_loop().remove_reader(self.ser.fileno())
        except (OSError, ValueError, RuntimeError):
            pass
        try:
            self.ser.close()
        except OSError:
            pass
        self.ser, self.m = None, {}

    def send(self, cmd):
        if self.ser is None:
            return False
        try:
            self.ser.write((cmd + '\r\n').encode())
            self.last_tx = time.time()
            return True
        except OSError as e:
            self.err = str(e)
            self.close()
            self.on_change()
            return False

    @property
    def online(self):
        return self.ser is not None and time.time() - self.last_rx < 3

    # ---------- prijem
    def _read(self):
        try:
            data = self.ser.read(4096)
        except OSError as e:   # izvučen kabl
            self.err = 'veza prekinuta (%s)' % e
            self.close()
            self.on_change()
            return
        if not data:
            return
        self.last_rx = time.time()
        self.buf += data
        changed = False
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            changed |= self._line(line.decode('ascii', 'replace').strip())
        if len(self.buf) > 4096:
            self.buf = b''
        if changed:
            self.on_change()

    def _line(self, ln):
        if not ln or not checksum_ok(ln):
            return False
        body = ln.rsplit('*', 1)[0]
        p = body.split(',')
        if p[0] in ST_TXT and len(p) >= 5:
            now = time.monotonic()   # razmak između redova mase (dijagnostika zastoja)
            if self.t_mass:
                gap = now - self.t_mass
                self.gaps = (self.gaps + [gap])[-200:]
            self.t_mass = now
            self.raw = (getattr(self, 'raw', []) + [p[0] + ' ' + p[2] + ' ' + p[4]])[-300:]   # dijagnostika: sirove vrednosti
            m = {'st': p[0], 'mod': p[1], 'mass': p[2], 'unit': p[3], 'flags': p[4]}
            if m == self.m:
                return False
            self.m = m
            return True
        if p[0] == 'INF':
            self.raw_inf, self.info = ln, kv(body[4:], INF_KEYS)
            return True
        if p[0] == 'CFG':
            self.raw_cfg, self.cfg = ln, kv(body[4:], CFG_KEYS)
            return True
        if ln.startswith(('OK:', 'E:', 'AL,', 'I:')):
            self.reply = ln
            return True
        self.other = (self.other + [ln])[-10:]   # npr. TMP,… (temperatura) — ne utiče na masu
        return False

    # ---------- prikaz
    def view(self):
        m, f = self.m, self.m.get('flags', '')
        st = m.get('st')
        mass = m.get('mass', '')
        try:
            val = float(mass)
        except ValueError:
            val = None
        if not self.online:
            disp = 'N/A'
        elif st == 'OL':
            disp = '-OL-'
        elif st == 'UL':
            disp = '-UL-'
        elif st == 'ER':
            disp = 'ERR' + ('-ADC' if 'A' in f else '')
        elif not m:
            disp = '------'
        else:
            disp = mass.lstrip('+')
        c = self.cfg
        num = lambda s: ('%g' % float(s)) if s else ''
        try:   # kao na natpisnoj pločici: Max 6/15kg, Min 100/200g (Min po području = MN · d_i / d)
            rg = int(c.get('RG') or 1)
            mx = '/'.join(num(c[k]) for k in ('MX', 'M2', 'M3')[:rg] if c.get(k))
            mins = [float(c['MN']) * float(c[dk]) / float(c['D']) for dk in ('D', 'D2', 'D3')[:rg] if c.get(dk)] if c.get('MN') and c.get('D') else []
            u = c.get('U', 'kg')
            mn = ('/'.join('%g' % round(x * 1000, 3) for x in mins) + 'g') if u == 'kg' and mins and max(mins) < 1 else ('/'.join('%g' % x for x in mins) + u if mins else '')
            c = dict(c, MX=mx, M2='', MN=mn)
        except (ValueError, ZeroDivisionError):
            pass
        cls = {'1': 'I', '2': 'II', '3': 'III', '4': 'IIII'}.get(c.get('CL', ''), c.get('CL', ''))
        return {'online': self.online, 'dev': self.dev, 'err': self.err, 'disp': disp, 'unit': m.get('unit', '') if st == 'ST' else '',
                'stable': st == 'ST', 'net': m.get('mod') == 'NT', 'zero': 'Z' in f, 'tare': 'T' in f or 'P' in f, 'min': 'M' in f,
                'neg': val is not None and val < 0, 'range': '2' if '2' in f else '3' if '3' in f else '1', 'flags': f, 'st': st or '',
                'cls': cls, 'max': c.get('MX', ''), 'max2': c.get('M2', ''), 'minw': c.get('MN', ''), 'u': c.get('U', 'kg'),
                'fw': self.info.get('FW', ''), 'sn': self.info.get('SN', ''), 'ck': self.info.get('CK', ''), 'reply': self.reply}
