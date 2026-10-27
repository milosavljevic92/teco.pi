"""Zajednički izlaz za alate na ekranu: slika ide kao sloj (overlay) PREKO plejera (mpv), pa sve ostalo
(YouTube, radio, TV) i dalje radi ispod. Slika se piše u /tmp (RAM) i mpv je učita komandom overlay-add.
"""
import json
import os
import socket
import time

MPV_SOCK = '/tmp/mpvsocket'
OVL_ID = 63   # najviši sloj: iznad slike konzole i sata


class Screen:
    def __init__(self, name):
        d = '/dev/shm' if os.path.isdir('/dev/shm') else '/tmp'   # /tmp je samo 64 MB, a slika na 1080p ima 8 MB
        self.files = ['%s/teco-tool-%s-%d.bgra' % (d, name, i) for i in (0, 1)]   # dva fajla: mpv nikad ne čita poluupisan
        self.cur = 0
        self.sock = None
        self.w = self.h = None

    def _cmd(self, *args):
        for _ in range(2):
            try:
                if self.sock is None:
                    self.sock = socket.socket(socket.AF_UNIX)
                    self.sock.settimeout(2)
                    self.sock.connect(MPV_SOCK)
                    self.sock.setblocking(False)
                self.sock.sendall((json.dumps({'command': list(args)}) + '\n').encode())
                try:   # odgovori i događaji se ne čitaju — samo se baca da se bafer ne napuni
                    while self.sock.recv(65536):
                        pass
                except (BlockingIOError, InterruptedError):
                    pass
                return True
            except OSError:
                self.sock = None
                time.sleep(0.3)
        return False

    def size(self):
        """Veličina ekrana plejera (osd-width/height); bez plejera: framebuffer."""
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2)
            s.connect(MPV_SOCK)
            out = {}
            for i, p in enumerate(('osd-width', 'osd-height'), 1):
                s.sendall((json.dumps({'command': ['get_property', p], 'request_id': i}) + '\n').encode())
            buf = b''
            t = time.time()
            while len(out) < 2 and time.time() - t < 2:
                buf += s.recv(65536)
                for line in buf.split(b'\n'):
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get('request_id') in (1, 2) and m.get('data'):
                        out[m['request_id']] = int(m['data'])
            s.close()
            if len(out) == 2:
                return self._fit(out[1], out[2])
        except OSError:
            pass
        with open('/sys/class/graphics/fb0/virtual_size') as f:
            return self._fit(*(int(x) for x in f.read().split(',')))

    def _fit(self, w, h):
        """Veliki ekran (TV 1080p): alat crta u pola rezolucije, a show() uvećava 2x — mpv sam ne ume da skalira
        sloj, a ceo kadar 1920x1080 je spor za crtanje na Pi-ju. Slika se centrira ako ne deli tačno."""
        self.k = max(1, h // 540)
        self.sw, self.sh = w, h
        return w // self.k, h // self.k

    def show(self, bgra, w, h):
        """bgra: bytes w*h*4 (B,G,R,A)."""
        k = getattr(self, 'k', 1)
        x = y = 0
        if k > 1:
            import numpy as np
            a = np.frombuffer(bgra, np.uint8).reshape(h, w, 4)
            bgra = a.repeat(k, 0).repeat(k, 1).tobytes()
            w, h = w * k, h * k
            x, y = (self.sw - w) // 2, (self.sh - h) // 2
        self.cur ^= 1
        f = self.files[self.cur]
        with open(f, 'wb') as fp:
            fp.write(bgra)
        self._cmd('overlay-add', OVL_ID, x, y, f, 0, 'bgra', w, h, w * 4)

    def close(self):
        self._cmd('overlay-remove', OVL_ID)
        for f in self.files:
            try:
                os.remove(f)
            except OSError:
                pass
