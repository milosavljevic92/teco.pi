"""PICkit 2 kao logički analizator: 3 kanala × 1024 uzorka, 5 kHz – 1 MHz, okidanje na nivo ili ivicu.
Preko libusb-a (python3-usb): Linux namerno ignoriše PICkit 2 kao HID uređaj (nema /dev/hidraw), pa se
koriste njegove interrupt krajnje tačke 0x01 (piše) i 0x81 (čita), paketi od 64 bajta.
Dozvola: /etc/udev/rules.d/60-teco-pickit2.rules (MODE 0666 za 04d8:0033).

Komande firmvera prema projektu pk2-la (GPLv2, reverzno inženjerstvo PICkit 2 „Logic Tool“-a):
  0xB8 početak snimanja: [0xB8, 1, enable, nivo, ivica, ponavljanja, prozor_lo, prozor_hi, brzina]
       bit kanala n (1-3) u enable/nivo/ivica = 1 << (n + 1); odgovor stiže kad okidanje uspe (0x04 0x40 = prekinuto dugmetom)
  0xB9 položaj u memoriji [0xB9, pomeraj, banka], 0xAC čita 64 bajta; banke 6 i 7 po 2 × 0x80 = 512 bajtova
  bajt uzoraka: kanal 1 bitovi 6 pa 2, kanal 2 bitovi 7 pa 3, kanal 3 bitovi 4 pa 0 (dva uzorka po bajtu)
Pinovi PICkit-a: 3 GND, 4 kanal 1 (PGD), 5 kanal 2 (PGC), 6 kanal 3 (AUX); pin 2 VDD na napon cilja (referenca ulaza).
"""
import os
import subprocess
import time

VID, PID = '04d8', '0033'
SETUP, SET_POS, READ, PAD, RUN_SCRIPT, UPLOAD = 0xB8, 0xB9, 0xAC, 0xAD, 0xA6, 0xAA
VPP_OFF, MCLR_GND_OFF, SET_ICSP_PINS, SET_AUX, ICSP_STATE, AUX_STATE = 0xFA, 0xF6, 0xF3, 0xCF, 0xDC, 0xCE
RATES = {'1MHz': (0x00, 1), '500kHz': (0x01, 2), '250kHz': (0x03, 4), '100kHz': (0x09, 10),
         '50kHz': (0x13, 20), '25kHz': (0x27, 40), '10kHz': (0x63, 100), '5kHz': (0xC7, 200)}   # bajt brzine, µs po uzorku
WINDOWS = {'start': (0xCC, 0x03), 'center': (0x0A, 0x00), 'end': (0x48, 0x02)}   # gde je okidanje u snimku
SAMPLES = 1024


class PK2Error(Exception):
    pass


RULE = 'SUBSYSTEM=="usb", ATTR{idVendor}=="04d8", ATTR{idProduct}=="0033", MODE="0666"\n'


def ensure_rule():
    """udev pravilo (dozvola za korisnika) — jednom; posle toga treba izvaditi/vratiti PICkit ili udevadm trigger."""
    p = '/etc/udev/rules.d/60-teco-pickit2.rules'
    if os.path.exists(p):
        return
    subprocess.run(['sudo', '-n', 'tee', p], input=RULE.encode(), capture_output=True, timeout=10)
    subprocess.run(['sudo', '-n', 'udevadm', 'control', '--reload'], capture_output=True, timeout=10)
    subprocess.run(['sudo', '-n', 'udevadm', 'trigger', '--subsystem-match=usb', '--attr-match=idVendor=' + VID], capture_output=True, timeout=10)
    time.sleep(1)


class PK2:
    def __init__(self):
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise PK2Error('Nedostaje python3-usb.')
        ensure_rule()
        self.usb = usb
        self.dev = usb.core.find(idVendor=int(VID, 16), idProduct=int(PID, 16))
        if self.dev is None:
            raise PK2Error('PICkit 2 nije priključen.')
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass   # već podešen (npr. posle pk2cmd-a)
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            raise PK2Error('PICkit 2 je zauzet ili nema dozvole (%s) — izvadi ga i vrati.' % e)

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, 0)
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def write(self, data):
        pkt = bytes(data) + bytes([PAD]) * (64 - len(data))
        self.dev.write(0x01, pkt, 1000)

    def read(self, timeout):
        try:
            return bytes(self.dev.read(0x81, 64, max(1, int(timeout * 1000))))
        except self.usb.core.USBError as e:   # isteklo vreme = nema odgovora još
            if 'timed out' in str(e).lower() or getattr(e, 'errno', None) == 110:
                return None
            raise PK2Error('USB greška: %s' % e)

    def flush(self):
        while self.read(0.05) is not None:
            pass

    def pins_input(self):
        """VPP isključen, MCLR pušten, ICSP i AUX pinovi kao ulazi."""
        self.write([RUN_SCRIPT, 6, VPP_OFF, MCLR_GND_OFF, SET_ICSP_PINS, 0x03, SET_AUX, 0x01])
        time.sleep(0.02)

    def pins(self):
        """Trenutni nivoi kanala 1-3 (0/1)."""
        self.write([RUN_SCRIPT, 2, ICSP_STATE, AUX_STATE, UPLOAD])
        r = self.read(2)
        if not r or len(r) < 3:
            raise PK2Error('PICkit 2 ne odgovara.')
        return [(r[1] >> 1) & 1, r[1] & 1, r[2] & 1]

    def capture(self, rate='1MHz', trig_ch=0, trig='rise', window='start', timeout=20, cancel=None):
        """Snimi 1024 uzorka po kanalu. trig_ch 0 = odmah (bez čekanja ivice); trig: rise / fall / high / low.
        Vraća dict: ch (3 liste 0/1), us (µs po uzorku), resp (prvi bajtovi odgovora — položaj okidanja)."""
        if rate not in RATES or window not in WINDOWS:
            raise PK2Error('Neispravna brzina ili prozor.')
        self.flush()
        self.pins_input()
        if trig_ch == 0:   # odmah: okidanje na trenutni nivo kanala 1 — ispunjeno odmah
            trig_ch, trig = 1, 'high' if self.pins()[0] else 'low'
        if trig_ch not in (1, 2, 3) or trig not in ('rise', 'fall', 'high', 'low'):
            raise PK2Error('Neispravno okidanje.')
        bit = 1 << (trig_ch + 1)
        en, lvl, edge = bit, bit if trig in ('rise', 'high') else 0, bit if trig in ('rise', 'fall') else 0
        rb, us = RATES[rate]
        wl, wh = WINDOWS[window]
        self.write([SETUP, 1, en, lvl, edge, 1, wl, wh, rb])
        end = time.time() + timeout
        resp = None
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise PK2Error('Prekinuto (PICkit čeka okidanje dok ne pritisneš njegovo dugme).')
            resp = self.read(0.25)
            if resp is not None:
                break
        if resp is None:
            raise PK2Error('Nema okidanja za %d s — PICkit i dalje čeka; pritisni dugme na njemu da prekine.' % timeout)
        if resp[0] == 0x04 and resp[1] == 0x40:
            raise PK2Error('Prekinuto dugmetom na PICkit-u.')
        raw = b''
        for bank, off in ((6, 0x00), (6, 0x80), (7, 0x00), (7, 0x80)):
            self.write([SET_POS, off, bank])
            for _ in range(2):
                self.write([READ])
                r = self.read(2)
                if r is None:
                    raise PK2Error('PICkit 2 nije poslao podatke.')
                raw += r[:64]
        ch = [[], [], []]
        for b in raw:
            ch[0] += [(b >> 6) & 1, (b >> 2) & 1]
            ch[1] += [(b >> 7) & 1, (b >> 3) & 1]
            ch[2] += [(b >> 4) & 1, b & 1]
        return {'ch': ch, 'us': us, 'rate': rate, 'trig_ch': trig_ch, 'trig': trig, 'window': window, 'resp': list(resp[:8]), 't': time.time()}


def uart_decode(v, us, baud, invert=False):
    """UART 8N1 iz uzoraka (lista 0/1): [(početni uzorak, kraj, bajt, greška_okvira)]."""
    n = len(v)
    spb = 1e6 / baud / us   # uzoraka po bitu
    if spb < 2:
        return []
    out, i = [], 1
    idle = 0 if invert else 1
    while i < n:
        if v[i - 1] == idle and v[i] != idle:   # početni bit
            mid = i + spb / 2
            if int(mid + 9 * spb) >= n:
                break
            if v[int(mid)] == idle:   # smetnja, ne početni bit
                i += 1
                continue
            byte = 0
            for k in range(8):
                bitv = v[int(mid + (k + 1) * spb)]
                if invert:
                    bitv ^= 1
                byte |= bitv << k
            stop = v[int(mid + 9 * spb)]
            out.append((i, int(i + 10 * spb), byte, (stop ^ invert) != 1))
            i = int(mid + 9 * spb)
        else:
            i += 1
    return out
