"""Serijski terminal (Alati → Elektronika): USB-serijski adapteri (ttyUSB/ttyACM) i RS232 uređaji.
Čitanje ide kroz event loop (add_reader, bez niti i bez čekanja), podaci se šalju stranicama u paketima na ~25 ms
kao binarni WebSocket okviri: 1 bajt smer ('R' primljeno / 'T' poslato) + 8 bajtova vreme (double, s) + podaci.
"""
import asyncio
import glob
import os
import struct
import time

BAUDS = (300, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200, 230400, 250000, 460800, 500000, 921600, 1000000, 1500000, 2000000)
HIST = 256 * 1024     # koliko poslednjih bajtova dobije stranica koja se tek otvori
FLUSH = 0.025         # s: skupljanje pre slanja stranicama (manje okvira, i dalje „uživo“)


def ports():
    """[{'dev', 'n'}]: USB-serijski adapteri, sa imenom proizvođača/čipa gde postoji."""
    out, seen = [], set()
    byid = {os.path.realpath(p): os.path.basename(p) for p in glob.glob('/dev/serial/by-id/*')}
    for dev in sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')):
        if dev in seen:
            continue
        seen.add(dev)
        n = ''
        base = '/sys/class/tty/%s/device' % os.path.basename(dev)
        for up in ('/../product', '/../../product'):
            try:
                n = open(base + up).read().strip()
                break
            except OSError:
                pass
        if not n and dev in byid:
            n = byid[dev].replace('usb-', '').rsplit('-if', 1)[0].replace('_', ' ')
        out.append({'dev': dev, 'n': n or 'USB serijski'})
    return out


class Term:
    def __init__(self):
        self.ser = None
        self.cfg = {}
        self.clients = set()
        self.hist = []          # [(smer, vreme, bajtovi)]
        self.hist_n = 0
        self.pend = []          # još neposlato stranicama
        self.flush_h = None
        self.rx = self.tx = 0
        self.err = None

    # ---------- stanje
    def status(self):
        return {'open': self.ser is not None, 'cfg': self.cfg, 'rx': self.rx, 'tx': self.tx, 'err': self.err,
                'ports': ports(), 'bauds': BAUDS, 'lines': self.lines()}

    def lines(self):
        if self.ser is None:
            return {}
        try:
            return {'dtr': self.ser.dtr, 'rts': self.ser.rts, 'cts': self.ser.cts, 'dsr': self.ser.dsr, 'cd': self.ser.cd, 'ri': self.ser.ri}
        except OSError:
            return {}

    # ---------- port
    def open(self, dev, baud, fmt='8N1', dtr=True, rts=True):
        import serial
        self.close()
        if dev not in [p['dev'] for p in ports()]:
            raise ValueError('Port %s nije priključen.' % dev)
        baud = int(baud)
        if not 50 <= baud <= 4000000:
            raise ValueError('Neispravna brzina.')
        if len(fmt) != 3 or fmt[0] not in '5678' or fmt[1] not in 'NEOMS' or fmt[2] not in '12':
            raise ValueError('Neispravan format (npr. 8N1).')
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.bytesize, s.parity, s.stopbits, s.timeout = dev, baud, int(fmt[0]), fmt[1], int(fmt[2]), 0
            s.dtr, s.rts = bool(dtr), bool(rts)
            s.open()
        except (serial.SerialException, OSError) as e:
            raise ValueError(str(e).split(':')[-1].strip() or 'Port se ne otvara.')
        self.ser, self.err = s, None
        self.cfg = {'dev': dev, 'baud': baud, 'fmt': fmt}
        self.rx = self.tx = 0
        asyncio.get_running_loop().add_reader(s.fileno(), self._on_read)

    def close(self):
        if self.ser is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self.ser.fileno())
        except (OSError, ValueError, RuntimeError):
            pass
        try:
            self.ser.close()
        except OSError:
            pass
        self.ser = None

    def _on_read(self):
        try:
            data = os.read(self.ser.fileno(), 65536)
        except OSError as e:   # adapter izvučen
            self.err = 'Port je nestao (%s)' % (e.strerror or e)
            self.close()
            self._status_all()
            return
        if not data:
            self.err = 'Port je zatvoren'
            self.close()
            self._status_all()
            return
        self.rx += len(data)
        self._push(b'R', data)

    def write(self, data):
        if self.ser is None:
            raise ValueError('Port nije otvoren.')
        n = self.ser.write(data)
        self.tx += n
        self._push(b'T', data)

    def set_lines(self, dtr=None, rts=None, brk=None):
        if self.ser is None:
            return
        if dtr is not None:
            self.ser.dtr = bool(dtr)
        if rts is not None:
            self.ser.rts = bool(rts)
        if brk:
            self.ser.send_break(0.25)

    # ---------- slanje stranicama
    def _push(self, d, data):
        t = time.time()
        self.hist.append((d, t, data))
        self.hist_n += len(data)
        while self.hist_n > HIST and len(self.hist) > 1:
            self.hist_n -= len(self.hist.pop(0)[2])
        self.pend.append((d, t, data))
        if self.flush_h is None:
            self.flush_h = asyncio.get_running_loop().call_later(FLUSH, self._flush)

    @staticmethod
    def _frames(items):
        """Spaja uzastopne komade istog smera (vreme prvog) u okvire."""
        out, cur = [], None
        for d, t, data in items:
            if cur and cur[0] == d and len(cur[2]) < 16384:
                cur[2] += data
            else:
                cur = [d, t, bytearray(data)]
                out.append(cur)
        return [d + struct.pack('<d', t) + bytes(b) for d, t, b in out]

    def _flush(self):
        self.flush_h = None
        frames, self.pend = self._frames(self.pend), []
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            for f in frames:
                asyncio.ensure_future(self._send(ws, f))

    async def _send(self, ws, f):
        try:
            await ws.send_bytes(f)
        except (ConnectionError, RuntimeError):
            self.clients.discard(ws)

    def _status_all(self):
        import json
        msg = json.dumps({'st': self.status()})
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(msg))

    def history(self):
        return self._frames(self.hist)
