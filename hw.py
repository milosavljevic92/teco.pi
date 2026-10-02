"""Teco.Pi hardver na I2C magistrali (pinovi 3/5) i IR prijemnik (TSOP na GPIO17).

- RDA5807M: FM tjuner sa RDS-om (adrese 0x10 za redom čitanje/upis, 0x11 za pojedinačne registre)
- TPA2016D2: stereo pojačalo 2 x 2,8 W sa AGC-om (adresa 0x58)
- IR: kernel dekodira signal (dtoverlay=gpio-ir), mi čitamo kod tastera sa /dev/input/eventX

Svaki uređaj se prepoznaje sam; ako nije povezan, server radi kao i ranije (FM preko RTL-SDR, jačina u plejeru).
"""
import asyncio
import logging
import time

log = logging.getLogger('teco')

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:   # bez biblioteke nema I2C uređaja, ostalo radi
    SMBus = i2c_msg = None

I2C_BUS = 1


def open_bus():
    if SMBus is None:
        return None
    try:
        return SMBus(I2C_BUS)
    except OSError:
        return None   # I2C nije uključen (dtparam=i2c_arm=on)


# ---------------------------------------------------------------- FM tjuner

class RDA5807:
    SEQ, RAND = 0x10, 0x11
    FMIN, FMAX = 87.0, 108.0

    def __init__(self, bus):
        self.bus = bus
        # registri 0x02-0x07 (upisuju se uvek svi, redom, na adresu 0x10)
        self.r = [
            0xC00D,   # 02: izlaz uključen, bez utišavanja, RDS, novi demodulator, uključen
            0x0000,   # 03: kanal, opseg 87-108 MHz, korak 100 kHz
            0x0A00,   # 04: de-emfaza 50 µs (Evropa), meko utišavanje slabog signala
            0x888F,   # 05: prag pretrage 8, LNA, jačina 15 (najveća; jačinu menja Pi)
            0x0000,   # 06
            0x4202,   # 07: mekši prelaz stereo/mono pri slabom signalu
        ]
        self.f = None
        self.ps, self.rt = '', ''            # RDS: ime stanice i tekst (pesma)
        self._ps = [' '] * 8
        self._ps_seen = set()
        self._rt = [' '] * 64
        self._rt_ab = None
        self.rssi, self.stereo, self.tuned = 0, False, False

    @classmethod
    def detect(cls, bus):
        try:
            w, r = i2c_msg.write(cls.RAND, [0x00]), i2c_msg.read(cls.RAND, 2)
            bus.i2c_rdwr(w, r)
            hi, _ = list(r)
            return cls(bus) if hi == 0x58 else None   # CHIPID
        except OSError:
            return None

    def _write(self):
        data = []
        for v in self.r:
            data += [v >> 8, v & 0xFF]
        self.bus.i2c_rdwr(i2c_msg.write(self.SEQ, data))

    def _read(self):
        m = i2c_msg.read(self.SEQ, 12)   # 0x0A-0x0F
        self.bus.i2c_rdwr(m)
        b = list(m)
        return [(b[i] << 8) | b[i + 1] for i in range(0, 12, 2)]

    def power_on(self):
        self.r[0] = 0x0002        # meki reset
        self._write()
        time.sleep(0.05)
        self.r[0] = 0xC00D
        self._write()
        time.sleep(0.1)

    def power_off(self):
        self.r[0] = 0x0000
        self._write()
        self.f = None

    def tune(self, f):
        f = min(self.FMAX, max(self.FMIN, float(f)))
        ch = int(round((f - 87.0) * 10))
        self.r[1] = (ch << 6) | 0x0010   # TUNE
        self._write()
        self.r[1] &= ~0x0010
        self.f = round(87.0 + ch / 10, 1)
        self.ps, self.rt = '', ''
        self._ps, self._ps_seen, self._rt, self._rt_ab = [' '] * 8, set(), [' '] * 64, None
        return self.f

    def set_mute(self, mute):
        self.r[0] = (self.r[0] & ~0x4000) | (0 if mute else 0x4000)
        self._write()

    def poll(self):
        """Čita stanje i RDS; vraća True ako se nešto promenilo (za osvežavanje ekrana)."""
        a, b, ra, rb, rc, rd = self._read()
        changed = False
        rssi = (b >> 9) & 0x7F
        stereo = bool(a & 0x0400)
        tuned = bool(a & 0x4000)
        if abs(rssi - self.rssi) >= 3 or stereo != self.stereo or tuned != self.tuned:
            self.rssi, self.stereo, self.tuned = rssi, stereo, tuned
            changed = True
        blera, blerb = (b >> 2) & 3, b & 3
        if a & 0x8000 and blera <= 1 and blerb <= 1:   # nova RDS grupa bez većih grešaka
            changed |= self._rds(rb, rc, rd)
        return changed

    def _rds(self, b, c, d):
        grp, ver_b = (b >> 12) & 0xF, bool(b & 0x0800)
        ok = lambda ch: chr(ch) if 32 <= ch < 127 else ' '
        if grp == 0:   # 0A/0B: ime stanice, 2 slova po grupi
            i = b & 3
            self._ps[i * 2], self._ps[i * 2 + 1] = ok(d >> 8), ok(d & 0xFF)
            self._ps_seen.add(i)
            if len(self._ps_seen) == 4:
                ps = ''.join(self._ps).strip()
                self._ps_seen = set()
                if ps and ps != self.ps:
                    self.ps = ps
                    return True
        elif grp == 2:   # 2A/2B: tekst (npr. pesma)
            ab = bool(b & 0x10)
            if self._rt_ab is not None and ab != self._rt_ab:
                self._rt = [' '] * 64   # stanica je počela novi tekst
            self._rt_ab = ab
            i = b & 0xF
            chars = [d >> 8, d & 0xFF] if ver_b else [c >> 8, c & 0xFF, d >> 8, d & 0xFF]
            pos = i * len(chars)
            for k, ch in enumerate(chars):
                if ch == 0x0D:   # kraj teksta
                    self._rt[pos + k:] = [' '] * (64 - pos - k)
                    break
                if pos + k < 64:
                    self._rt[pos + k] = ok(ch)
            rt = ' '.join(''.join(self._rt).split())
            if rt != self.rt and len(rt) >= 3:
                self.rt = rt
                return True
        return False


# ---------------------------------------------------------------- pojačalo

class TPA2016:
    ADDR = 0x58
    GMIN, GMAX = -28, 24   # dB; gornja granica ispod 30 dB čuva male zvučnike

    def __init__(self, bus):
        self.bus = bus
        self.muted = False
        self.vol = None

    @classmethod
    def detect(cls, bus):
        try:
            bus.read_byte_data(cls.ADDR, 1)
            return cls(bus)
        except OSError:
            return None

    def _w(self, reg, val):
        self.bus.write_byte_data(self.ADDR, reg, val & 0xFF)

    def setup(self):
        self._w(1, 0xC3)   # oba zvučnika uključena, zaštita od kratkog spoja
        self._w(2, 0x05)   # AGC napad
        self._w(3, 0x0B)   # AGC otpuštanje
        self._w(4, 0x00)   # bez zadržavanja
        self._w(6, 0x3A)   # limiter uključen, šum-gejt nizak
        self._w(7, 0xC1)   # najveće pojačanje AGC-a 30 dB, kompresija 2:1 (glasno bez izobličenja)

    def set_volume(self, v):
        """v = 0-100 (kao klizač na stranici); 0 isključuje izlaz."""
        v = max(0, min(100, int(v)))
        self.vol = v
        g = round(self.GMIN + (self.GMAX - self.GMIN) * v / 100)
        self._w(5, g & 0x3F)   # fiksno pojačanje, 6-bitni broj sa znakom
        self._w(1, 0xC3 if v > 0 and not self.muted else 0x03)

    def set_mute(self, m):
        self.muted = bool(m)
        self._w(1, 0x03 if self.muted or not self.vol else 0xC3)


# ---------------------------------------------------------------- IR prijemnik

async def ir_codes():
    """Kodovi tastera sa daljinskog: (kod, ponavljanje). Čeka dok se prijemnik ne pojavi."""
    try:
        import evdev
    except ImportError:
        log.warning('python3-evdev nije instaliran: IR prijemnik ne radi')
        return
    while True:
        dev = None
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            if 'gpio_ir_recv' in d.name:
                dev = d
                break
            d.close()
        if not dev:
            await asyncio.sleep(10)
            continue
        log.info('IR prijemnik: %s', dev.path)
        last, last_t = None, 0.0
        try:
            async for ev in dev.async_read_loop():
                if ev.type == evdev.ecodes.EV_MSC and ev.code == evdev.ecodes.MSC_SCAN:
                    now = time.monotonic()
                    code = 'IR 0x%X' % ev.value
                    repeat = code == last and now - last_t < 0.25   # taster je držan
                    last, last_t = code, now
                    yield code, repeat
        except OSError:
            log.warning('IR prijemnik je nestao, tražim ponovo')
            await asyncio.sleep(5)
