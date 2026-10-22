"""Teco.Pi mobilni modem (SIMCom A7672E / SIM800) na TX/RX ploče: stalna veza (Link) koja odmah hvata
pozive i poruke (RING, +CLIP, +CLCC, +CMTI…), AT komande jedna po jedna, SMS (tekst mod, UCS2 = i šđčćž), USSD.
SimSerial je simulator modema (model 'SIM'): ponaša se kao pravi, da se sve testira bez hardvera.

Sve funkcije su blokirajuće (server ih zove iz niti, jednu po jednu)."""
import re
import threading
import time
from datetime import datetime

OK_RE = rb'\r\n(OK|ERROR|\+CM[ES] ERROR[^\r]*)\r\n'
URC_RE = re.compile(r'^(RING|\+CLIP:|\+CLCC:|NO CARRIER|BUSY|NO ANSWER|VOICE CALL:|MISSED_CALL|\+CMTI:|\+CUSD:)')


# ---------- veza
class Link:
    def __init__(self, port, baud, on_urc, sim=False):
        if sim:
            self.ser = SimSerial()
        else:
            import serial
            self.ser = serial.Serial(port, baud, timeout=0.3)
        self.sim = sim
        self.on_urc = on_urc
        self.lock = threading.Lock()   # jedna AT komanda u isto vreme
        self.cv = threading.Condition()
        self.buf, self.pending, self.lb = b'', False, b''
        self.alive, self.err = True, None
        self.th = threading.Thread(target=self._reader, daemon=True, name='modem-rx')
        self.th.start()

    def _reader(self):
        while self.alive:
            try:
                data = self.ser.read(256)
            except Exception as e:   # port nestao / modem ugašen
                self.err, self.alive = e, False
                self.on_urc('__closed__')
                break
            if not data:
                continue
            with self.cv:
                if self.pending:
                    self.buf += data
                    self.cv.notify_all()
            self.lb += data
            while b'\n' in self.lb:
                line, self.lb = self.lb.split(b'\n', 1)
                s = line.strip().decode('latin-1', 'replace')
                if s and URC_RE.match(s):
                    try:
                        self.on_urc(s)
                    except Exception:
                        pass
            if len(self.lb) > 4096:
                self.lb = b''

    def at(self, cmd, wait=3.0, until=OK_RE, raw=None):
        """Pošalji komandu, sačekaj odgovor (do 'until' ili isteka vremena), vrati ceo odgovor kao tekst."""
        if not self.alive:
            raise OSError('veza sa modemom je prekinuta')
        with self.lock:
            with self.cv:
                self.buf, self.pending = b'', True
            self.ser.write(raw if raw is not None else (cmd + '\r').encode('latin-1'))
            end = time.time() + wait
            with self.cv:
                while not re.search(until, self.buf):
                    left = end - time.time()
                    if left <= 0:
                        break
                    self.cv.wait(left)
                self.pending = False
                return self.buf.decode('latin-1', 'replace')

    def close(self):
        self.alive = False
        try:
            self.ser.close()
        except Exception:
            pass


def setup(link, model, pin=''):
    """Posle otvaranja: eho isključen, tekst SMS, broj pozivaoca (CLIP), stanje poziva (CLCC), nova poruka (CMTI)."""
    r = link.at('AT', 2)
    if 'OK' not in r:
        r = link.at('AT', 3)
        if 'OK' not in r:
            raise OSError('modem ne odgovara na AT (TX/RX, napajanje, brzina?)')
    link.at('ATE0')
    cpin = link.at('AT+CPIN?')
    if 'SIM PIN' in cpin:
        if not pin:
            raise OSError('SIM kartica traži PIN (upiši ga u Telefonska linija)')
        if 'OK' not in link.at('AT+CPIN=' + pin, 8):
            raise OSError('pogrešan PIN SIM kartice')
        time.sleep(3)
    for c in ('AT+CMGF=1', 'AT+CLIP=1', 'AT+CLCC=1', 'AT+CNMI=2,1,0,0,0', 'AT+CSCS="GSM"'):
        link.at(c)
    if model == 'SIM800':
        link.at('AT+COLP=0')


def info(link, model):
    r = {c: link.at(c, 4) for c in ['ATI', 'AT+CPIN?', 'AT+CSQ', 'AT+CREG?', 'AT+COPS?'] + (['AT+CEREG?', 'AT+CPSI?'] if model != 'SIM800' else ['AT+CBC'])}
    csq = re.search(r'\+CSQ: (\d+)', r['AT+CSQ'])
    reg = re.search(r'\+CREG: \d,(\d)', r['AT+CREG?'])
    ereg = re.search(r'\+CEREG: \d,(\d)', r.get('AT+CEREG?', ''))   # LTE (A7672E): registracija u 4G mreži
    if ereg and ereg.group(1) in ('1', '5') and not (reg and reg.group(1) in ('1', '5')):
        reg = ereg
    psi = re.search(r'\+CPSI: ([^,\r\n]+)', r.get('AT+CPSI?', ''))
    op = re.search(r'\+COPS: \d,\d,"([^"]+)"', r['AT+COPS?'])
    bat = re.search(r'\+CBC: \d,(\d+),(\d+)', r.get('AT+CBC', ''))
    q = int(csq.group(1)) if csq else 99
    cp = r['AT+CPIN?']
    return {'ok': True, 'model': re.sub(r'\s*OK\s*$', '', r['ATI'].strip())[:40],
            'sim': 'spremna' if 'READY' in cp else 'traži PIN' if 'SIM PIN' in cp else 'nema SIM kartice' if 'ERROR' in cp else cp.strip()[:30],
            'reg': {'1': 'u mreži', '5': 'roming', '2': 'traži mrežu…', '3': 'odbijen', '0': 'nije u mreži'}.get(reg.group(1) if reg else '', '?'),
            'op': op.group(1) if op else '', 'csq': None if q == 99 else q, 'dbm': None if q == 99 else -113 + 2 * q,
            'volt': int(bat.group(2)) / 1000 if bat else None, 'net': psi.group(1) if psi else ('GSM' if model == 'SIM800' else None), 't': time.time()}


# ---------- SMS
def _ucs2(s):
    return s.encode('utf-16-be').hex().upper()


def _unucs2(h):
    h = h.strip()
    if re.fullmatch(r'([0-9A-Fa-f]{4})+', h):
        try:
            return bytes.fromhex(h).decode('utf-16-be', 'replace')
        except ValueError:
            pass
    return h


def is_gsm7(s):
    return all(32 <= ord(c) < 127 or c in '\n\r' for c in s) and not any(c in s for c in '[]{}\\^~|`')


def parts(text):
    """Delovi poruke: 160 znakova (latinica bez kvačica) ili 70 (šđčćž, ćirilica, emoji…)."""
    n = 160 if is_gsm7(text) else 70
    return [text[i:i + n] for i in range(0, len(text), n)] or ['']


def _ts(s):
    try:
        return datetime.strptime(s[:17], '%y/%m/%d,%H:%M:%S').timestamp()
    except ValueError:
        return time.time()


def poll_sms(link):
    """Primljene poruke sa SIM kartice (i obriše ih sa nje) + jačina signala."""
    link.at('AT+CSCS="UCS2"')
    try:
        out = link.at('AT+CMGL="ALL"', 10)
    finally:
        link.at('AT+CSCS="GSM"')
    msgs = []
    for hdr, body in re.findall(r'\+CMGL: ([^\r\n]+)\r\n([^\r\n]*)', out):
        vals = [a or b for a, b in re.findall(r'"([^"]*)"|(\d+)', hdr)]
        if len(vals) < 3 or 'REC' not in vals[1]:
            continue
        ts = next((v for v in vals[3:] if re.match(r'\d\d/\d\d/\d\d,', v)), '')
        msgs.append({'idx': int(vals[0]), 'num': _unucs2(vals[2]), 'text': _unucs2(body), 't': _ts(ts)})
    for x in msgs:
        link.at('AT+CMGD=%d' % x['idx'])
    csq = re.search(r'\+CSQ: (\d+)', link.at('AT+CSQ'))
    return msgs, (int(csq.group(1)) if csq else None)


def send_sms(link, num, text):
    sent = 0
    try:
        for p in parts(text):
            if is_gsm7(p):
                link.at('AT+CSCS="GSM"')
                link.at('AT+CSMP=17,167,0,0')
                dest, body = num, p.encode('ascii', 'replace')
            else:
                link.at('AT+CSCS="UCS2"')
                link.at('AT+CSMP=17,167,0,8')
                dest, body = _ucs2(num), _ucs2(p).encode()
            r = link.at('AT+CMGS="%s"' % dest, 6, until=rb'>|ERROR')
            if '>' not in r:
                raise OSError('modem ne prima poruku: ' + r.strip()[:60])
            r = link.at('', 60, raw=body + b'\x1a', until=rb'\+CMGS: \d+[\s\S]*OK|ERROR')
            if '+CMGS' not in r:
                raise OSError('nije poslato: ' + (r.strip()[:60] or 'nema odgovora mreže'))
            sent += 1
    finally:
        link.at('AT+CSCS="GSM"')
    return sent


def ussd(link, code):
    link.at('AT+CSCS="GSM"')
    r = link.at('AT+CUSD=1,"%s",15' % code, 25, until=rb'(?s)\+CUSD: \d(,".*?"(,\d+)?)?\r\n|ERROR')
    mt = re.search(r'(?s)\+CUSD: (\d)(?:,"(.*?)"(?:,(\d+))?)?\r\n', r)
    if not mt:
        raise OSError('nema odgovora mreže' if 'ERROR' not in r else 'mreža je odbila kod')
    txt, dcs = mt.group(2) or '', int(mt.group(3) or 0)
    if dcs == 72 or (re.fullmatch(r'([0-9A-Fa-f]{4})+', txt) and len(txt) >= 8):
        txt = _unucs2(txt)
    if mt.group(1) == '1':   # mreža čeka odgovor (meni): zatvori sesiju
        link.at('AT+CUSD=2')
    return txt.strip() or '(prazan odgovor)'


# ---------- simulator modema
class SimSerial:
    """Ponaša se kao SIMCom modem na serijskom portu: odgovara na AT komande i šalje URC-ove.
    Spolja (sa stranice) se okida: dolazni poziv, pozivalac spustio, druga strana se javila/spustila, dolazni SMS."""

    def __init__(self):
        self.out = b''
        self.cv = threading.Condition()
        self.inbuf = b''
        self.cs = 'GSM'
        self.cmgs = None
        self.inbox = []   # [idx, broj, tekst, vreme]
        self.idx = 0
        self.call = None   # {'num','dir','state'}: state dialing/alerting/active/incoming
        self.closed = False
        self.answer = True   # druga strana se javlja na odlazni poziv

    # serijski interfejs
    def read(self, n):
        with self.cv:
            if not self.out:
                self.cv.wait(0.3)
            if self.closed:
                raise OSError('zatvoren')
            d, self.out = self.out[:n], self.out[n:]
            return d

    def write(self, b):
        self.inbuf += b
        while True:
            if self.cmgs is not None:
                if b'\x1a' not in self.inbuf:
                    return
                body, self.inbuf = self.inbuf.split(b'\x1a', 1)
                self.cmgs = None
                self.idx += 1
                self._emit('\r\n+CMGS: %d\r\n\r\nOK\r\n' % self.idx)
                continue
            if b'\r' not in self.inbuf:
                return
            line, self.inbuf = self.inbuf.split(b'\r', 1)
            self._cmd(line.decode('latin-1').strip())

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def _emit(self, s, delay=0):
        def go():
            with self.cv:
                self.out += s.encode('latin-1')
                self.cv.notify_all()
        threading.Timer(delay, go).start() if delay else go()

    def _clcc(self, stat):
        c = self.call or {}
        return '\r\n+CLCC: 1,%d,%d,0,0,"%s",%d\r\n' % (0 if c.get('dir') == 'out' else 1, stat, c.get('num', ''), 145 if c.get('num', '').startswith('+') else 129)

    def _enc(self, s):
        return _ucs2(s) if self.cs == 'UCS2' else s

    def _cmd(self, c):
        ok = '\r\nOK\r\n'
        u = c.upper()
        if not u:
            return
        if u.startswith('AT+CSCS='):
            self.cs = 'UCS2' if 'UCS2' in u else 'GSM'
            return self._emit(ok)
        if u == 'ATI':
            return self._emit('\r\nSIMCom SIMULATOR (Teco.Pi)\r\n' + ok)
        if u == 'AT+CPIN?':
            return self._emit('\r\n+CPIN: READY\r\n' + ok)
        if u == 'AT+CSQ':
            return self._emit('\r\n+CSQ: 22,0\r\n' + ok)
        if u in ('AT+CREG?', 'AT+CEREG?'):
            return self._emit('\r\n%s: 0,1\r\n' % u[2:-1] + ok)
        if u == 'AT+COPS?':
            return self._emit('\r\n+COPS: 0,0,"Teco Test",7\r\n' + ok)
        if u == 'AT+CPSI?':
            return self._emit('\r\n+CPSI: LTE,Online,220-03,0x1234,1234567,100,EUTRAN-BAND3\r\n' + ok)
        if u == 'AT+CBC':
            return self._emit('\r\n+CBC: 0,90,4000\r\n' + ok)
        if u.startswith('AT+CMGL'):
            s = ''.join('\r\n+CMGL: %d,"REC UNREAD","%s","","%s"\r\n%s' % (i, self._enc(n), time.strftime('%y/%m/%d,%H:%M:%S+08', time.localtime(t)), self._enc(x))
                        for i, n, x, t in self.inbox)
            return self._emit(s + '\r\n' + ok)
        if u.startswith('AT+CMGD='):
            i = int(re.sub(r'\D', '', u) or 0)
            self.inbox = [m for m in self.inbox if m[0] != i]
            return self._emit(ok)
        if u.startswith('AT+CMGS='):
            self.cmgs = True
            return self._emit('\r\n> ')
        if u.startswith('AT+CUSD=1'):
            self._emit(ok)
            return self._emit('\r\n+CUSD: 0,"Stanje racuna: 1.234,56 din. Vazi do 31.12. (simulator)",15\r\n', 1.0)
        if u.startswith('ATD'):
            num = re.sub(r'[^\d+*#]', '', c[3:])
            self.call = {'num': num, 'dir': 'out', 'state': 'dialing'}
            self._emit(ok + self._clcc(2))
            self._emit(self._clcc(3), 1.0)
            if self.answer:
                def ans():
                    if self.call and self.call['dir'] == 'out' and self.call['state'] != 'active':
                        self.call['state'] = 'active'
                        self._emit(self._clcc(0) + '\r\nVOICE CALL: BEGIN\r\n')
                threading.Timer(4.0, ans).start()
            return
        if u == 'ATA':
            if self.call and self.call['state'] == 'incoming':
                self.call['state'] = 'active'
                return self._emit(ok + self._clcc(0) + '\r\nVOICE CALL: BEGIN\r\n')
            return self._emit('\r\nNO CARRIER\r\n')
        if u in ('ATH', 'AT+CHUP'):
            had = self.call
            self.call = None
            return self._emit(ok + (('\r\n+CLCC: 1,%d,6,0,0,"%s",129\r\n\r\nVOICE CALL: END: 0\r\n' % (0 if had['dir'] == 'out' else 1, had['num'])) if had else ''))
        return self._emit(ok)   # ostalo (ATE0, CMGF, CLIP, CNMI, CSMP, VTS…): OK

    # okidači sa stranice (simulacija)
    def sim_incoming(self, num):
        self.call = {'num': num, 'dir': 'in', 'state': 'incoming'}
        me = self.call

        def ring():
            n = 0
            while self.call is me and me['state'] == 'incoming' and n < 10 and not self.closed:
                self._emit('\r\nRING\r\n\r\n+CLIP: "%s",145,"",0,"",0\r\n' % num + (self._clcc(4) if n == 0 else ''))
                n += 1
                time.sleep(3)
            if self.call is me and me['state'] == 'incoming':   # niko se nije javio: pozivalac odustao
                self.call = None
                self._emit('\r\n+CLCC: 1,1,6,0,0,"%s",145\r\n\r\nNO CARRIER\r\n\r\nMISSED_CALL: %s %s\r\n' % (num, time.strftime('%I:%M%p'), num))
        threading.Thread(target=ring, daemon=True).start()

    def sim_hangup(self):
        """Druga strana spustila (dolazni koji još zvoni ili razgovor)."""
        c = self.call
        if not c:
            return
        self.call = None
        if c['state'] == 'incoming':
            self._emit('\r\n+CLCC: 1,1,6,0,0,"%s",145\r\n\r\nNO CARRIER\r\n\r\nMISSED_CALL: %s %s\r\n' % (c['num'], time.strftime('%I:%M%p'), c['num']))
        else:
            self._emit('\r\n+CLCC: 1,%d,6,0,0,"%s",145\r\n\r\nVOICE CALL: END: 12\r\n\r\nNO CARRIER\r\n' % (0 if c['dir'] == 'out' else 1, c['num']))

    def sim_answer(self):
        c = self.call
        if c and c['dir'] == 'out' and c['state'] != 'active':
            c['state'] = 'active'
            self._emit(self._clcc(0) + '\r\nVOICE CALL: BEGIN\r\n')

    def sim_sms(self, num, text):
        self.idx += 1
        self.inbox.append((self.idx, num, text, time.time()))
        self._emit('\r\n+CMTI: "SM",%d\r\n' % self.idx)
