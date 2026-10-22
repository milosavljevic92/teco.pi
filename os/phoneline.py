"""Teco.Pi linija za obični telefon (SLIC KS0835F): zvono (RM + FR na GPIO), tonovi u slušalici
(slobodna linija, zauzeće) i prepoznavanje tonskog biranja (DTMF, Goertzel) preko zvučne kartice.
Impulsno biranje se prepoznaje u serveru iz ivica SHK (slušalica)."""
import re
import subprocess
import threading
import time

import numpy as np

RATE = 8000
DTMF_ROWS = (697, 770, 852, 941)
DTMF_COLS = (1209, 1336, 1477, 1633)
DTMF_KEYS = ('123A', '456B', '789C', '*0#D')


class Ringer:
    """Zvono: dok je RM uključen, FR menja polaritet na ring_hz (SLIC daje napon zvona); ritam po kadenci."""

    def __init__(self):
        self.stop_ev = threading.Event()
        self.th = None

    def start(self, rm_pin, fr_pin, hz, cadence, seconds=None):
        self.stop()
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self._run, args=(rm_pin, fr_pin, hz, cadence, seconds, self.stop_ev), daemon=True, name='slic-ring')
        self.th.start()

    def stop(self):
        self.stop_ev.set()
        if self.th:
            self.th.join(2)
        self.th = None

    @property
    def ringing(self):
        return bool(self.th and self.th.is_alive())

    @staticmethod
    def _run(rm_pin, fr_pin, hz, cadence, seconds, ev):
        from gpiozero import OutputDevice
        rm, fr = OutputDevice(rm_pin, initial_value=False), OutputDevice(fr_pin, initial_value=False)
        end = time.time() + seconds if seconds else None
        half = 0.5 / hz
        try:
            while not ev.is_set() and (end is None or time.time() < end):
                for on, off in cadence:
                    rm.on()
                    t1 = time.time() + on
                    while time.time() < t1 and not ev.is_set():
                        fr.toggle()
                        time.sleep(half)
                    rm.off()
                    fr.off()
                    if ev.wait(off) or (end and time.time() >= end):
                        break
        finally:
            rm.off()
            fr.off()
            rm.close()
            fr.close()


def alsa_device(kind, nth=0):
    """'usb' / 'jack' / 'hdmi' / 'i2s' -> plughw:N (nth-ta kartica te vrste: SLIC i modem mogu imati svaki svoju USB karticu) ili None."""
    try:
        cards = open('/proc/asound/cards').read()
    except OSError:
        return None
    found = [num for num, name in re.findall(r'^\s*(\d+) \[[^\]]*\]: (.+)$', cards, re.M)
             if (kind == 'usb' and 'usb' in name.lower()) or (kind == 'jack' and 'headphones' in name.lower()) or (kind == 'hdmi' and 'hdmi' in name.lower())
             or (kind == 'i2s' and not any(x in name.lower() for x in ('usb', 'headphones', 'hdmi')))]
    return 'plughw:%s' % found[nth] if len(found) > nth else None


class Tones:
    """Ton u slušalici: 'dial' (stalni), 'busy' (0,5 / 0,5 s), 'ringback' (1 / 4 s); jedan po jedan."""
    PAT = {'dial': [(1.0, 0)], 'busy': [(0.5, 0.5)], 'ringback': [(1.0, 4.0)]}

    def __init__(self):
        self.p, self.th, self.ev, self.kind = None, None, threading.Event(), None

    def play(self, kind, device, hz=425):
        if kind == self.kind:
            return
        self.stop()
        if not device or kind not in self.PAT:
            return
        self.kind = kind
        self.ev = threading.Event()
        try:
            self.p = subprocess.Popen(['aplay', '-q', '-D', device, '-f', 'S16_LE', '-r', str(RATE), '-c', '1', '-t', 'raw'],
                                      stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.p = None
            return
        self.th = threading.Thread(target=self._run, args=(self.p, self.PAT[kind], hz, self.ev), daemon=True, name='tone')
        self.th.start()

    def stop(self):
        self.kind = None
        self.ev.set()
        if self.p:
            try:
                self.p.kill()
            except OSError:
                pass
        self.p = None

    @staticmethod
    def _run(p, pat, hz, ev):
        t = np.arange(int(RATE * 0.1)) / RATE
        on = (np.sin(2 * np.pi * hz * t) * 9000).astype('<i2').tobytes()
        off = bytes(len(on))
        try:
            while not ev.is_set():
                for a, b in pat:
                    for _ in range(max(1, int(a * 10))):
                        if ev.is_set():
                            return
                        p.stdin.write(on)
                    for _ in range(int(b * 10)):
                        if ev.is_set():
                            return
                        p.stdin.write(off)
        except (OSError, ValueError):
            pass


def goertzel(x, f):
    w = 2 * np.pi * f / RATE
    c = 2 * np.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    return s1 * s1 + s2 * s2 - c * s1 * s2


class Dtmf:
    """Sluša zvučnu karticu i javlja pritisnut taster (cb(cifra)) jednom po pritisku."""

    def __init__(self):
        self.p, self.ev = None, threading.Event()

    def start(self, device, cb):
        self.stop()
        if not device:
            return False
        self.ev = threading.Event()
        try:
            self.p = subprocess.Popen(['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', str(RATE), '-c', '1', '-t', 'raw'],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.p = None
            return False
        threading.Thread(target=self._run, args=(self.p, cb, self.ev), daemon=True, name='dtmf').start()
        return True

    def stop(self):
        self.ev.set()
        if self.p:
            try:
                self.p.kill()
            except OSError:
                pass
        self.p = None

    @staticmethod
    def _run(p, cb, ev):
        n = 205   # ~25 ms
        last, cand, cnt = None, None, 0
        while not ev.is_set():
            b = p.stdout.read(n * 2)
            if not b or len(b) < n * 2:
                break
            x = np.frombuffer(b, '<i2').astype(np.float64)
            energy = float(np.dot(x, x)) + 1e-9
            if energy < n * 200 ** 2:   # tišina
                key = None
            else:
                rp = [goertzel(x, f) for f in DTMF_ROWS]
                cp = [goertzel(x, f) for f in DTMF_COLS]
                ri, ci = int(np.argmax(rp)), int(np.argmax(cp))
                rs, cs = sorted(rp), sorted(cp)
                ok = rp[ri] > 4 * rs[-2] and cp[ci] > 4 * cs[-2] and (rp[ri] + cp[ci]) > 0.3 * energy * n / 2
                key = DTMF_KEYS[ri][ci] if ok else None
            if key == cand:
                cnt += 1
            else:
                cand, cnt = key, 1
            if cnt == 2 and key != last:   # dva okvira zaredom = pritisak
                if key:
                    cb(key)
                last = key
            elif key is None and cnt >= 2:
                last = None
