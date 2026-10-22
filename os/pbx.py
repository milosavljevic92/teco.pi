"""Teco.Pi telefonska centrala (Asterisk bez njegovog GUI-ja — podešava se sa Teco.Pi stranice):
- konfiguracija (lokali, krugovi zvonjenja, radno vreme, govorna pošta) se generiše u /etc/asterisk/teco/*.conf
- ARI veza (Stasis app 'teco'): SLIC lokal i GSM modem su kanali kojima upravlja Teco.Pi
- most za zvuk: Asterisk externalMedia (RTP, ulaw) <-> zvučna kartica (SLIC telefon / modem)."""
import asyncio
import audioop
import json
import random
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

TECO_DIR = Path('/etc/asterisk/teco')
SOUNDS = Path('/var/lib/asterisk/sounds/teco')
VM_DIR = Path('/var/spool/asterisk/voicemail/teco')
PROMPTS = {'welcome': 'Dobrodošlica (radno vreme)', 'out-worktime': 'Van radnog vremena', 'nobody': 'Niko nije dostupan'}
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
RING_SEC = 5   # jedno zvono = 1 s zvona + 4 s pauze


def gen_config(cfg, ari_pass, permit):
    """Podešavanje sa stranice -> {ime fajla: sadržaj}."""
    ext = cfg['ext']
    slic = cfg['slic_ext']
    # --- PJSIP: SIP lokali (svi osim SLIC-a), samo iz lokalnih mreža
    pj = ['; generiše Teco.Pi — ne menjati ručno', '[teco-udp]', 'type=transport', 'protocol=udp', 'bind=0.0.0.0:5060', '',
          '[teco-lan]', 'type=acl', 'deny=0.0.0.0/0.0.0.0'] + ['permit=%s' % p for p in permit] + ['']
    for n, e in sorted(ext.items()):
        if n == slic:
            continue
        pj += ['[%s]' % n, 'type=endpoint', 'context=teco-internal', 'disallow=all', 'allow=ulaw,alaw', 'auth=%s-auth' % n, 'aors=%s' % n,
               'callerid="%s" <%s>' % (e['name'].replace('"', ''), n), 'mailboxes=%s@teco' % n, 'direct_media=no', 'rtp_symmetric=yes',
               'force_rport=yes', 'rewrite_contact=yes', 'dtmf_mode=rfc4733', '',
               '[%s-auth]' % n, 'type=auth', 'auth_type=userpass', 'username=%s' % n, 'password=%s' % e['pass'], '',
               '[%s]' % n, 'type=aor', 'max_contacts=3', 'remove_existing=yes',
               'default_expiration=300', 'maximum_expiration=300', 'minimum_expiration=60', '']   # telefon koji nestane ispada posle ≤5 min; odmah dostupan po prijavi
    # --- plan biranja
    def dial_of(n):
        return 'Local/slic@teco-slic' if n == slic else 'PJSIP/%s' % n
    x = ['; generiše Teco.Pi — ne menjati ručno', '[teco-internal]']
    for n in sorted(ext):
        x += ['exten => %s,1,NoOp(lokal %s)' % (n, n), ' same => n,Dial(%s,%d,m(ringback))' % (dial_of(n), 30),
              ' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?end)', ' same => n,VoiceMail(%s@teco,s)' % n, ' same => n(end),Hangup()']
    x += ['exten => *97,1,VoiceMailMain(${CALLERID(num)}@teco)',          # preslušavanje svoje pošte
          'exten => _0XXX.,1,Stasis(teco,gsm-out,${EXTEN})',               # spolja preko modema (064…, 011…)
          'exten => _+X.,1,Stasis(teco,gsm-out,${EXTEN})', '',
          '[teco-slic]', 'exten => slic,1,Stasis(teco,slic,${IF($["${TECO_FROM}" != ""]?${TECO_FROM}:${CALLERID(num)})},${CALLERID(name)})', '',
          '[teco-gsm-in]',
          'exten => _[+0-9].,1,Set(CALLERID(all)="${EXTEN}" <${EXTEN}>)',   # Local/<broj>@teco-gsm-in: broj pozivaoca ide dalje (SLIC, SIP)
          ' same => n,Set(__TECO_FROM=${EXTEN})',                           # nasleđuje se kroz Local kanale (SLIC)
          ' same => n,Goto(s,1)',
          'exten => s,1,Answer()', ' same => n,Wait(0.5)']
    w = cfg['work']
    if w.get('on') and w.get('days'):
        days = '&'.join(DAYS[d] for d in sorted(w['days']))
        x += [' same => n,GotoIfTime(%s-%s,%s,*,*?work)' % (w['from'], w['to'], days),
              ' same => n,ExecIf($[${STAT(e,%s/out-worktime.wav)}]?Playback(%s/out-worktime))' % (SOUNDS, SOUNDS),
              ' same => n,VoiceMail(%s@teco,s)' % cfg['vm_box'], ' same => n,Hangup()']
    x += [' same => n(work),ExecIf($[${STAT(e,%s/welcome.wav)}]?Playback(%s/welcome))' % (SOUNDS, SOUNDS)]
    for i, r in enumerate(cfg['rounds'], 1):   # krug: zvone samo lokali koji su online (SIP prijavljen, SLIC uključen)
        x += [' same => n,NoOp(krug %d)' % i, ' same => n,Set(D=)', ' same => n,Set(DIALSTATUS=)']
        for n in r['m']:
            if n not in ext:
                continue
            if n == slic:
                x += [' same => n,ExecIf($["${GLOBAL(TECO_SLIC)}" = "1"]?Set(D=${D}&%s))' % dial_of(n)]
            else:
                x += [' same => n,ExecIf($["${DEVICE_STATE(PJSIP/%s)}" != "UNAVAILABLE" & "${DEVICE_STATE(PJSIP/%s)}" != "INVALID"]?Set(D=${D}&PJSIP/%s))' % (n, n, n)]
        x += [' same => n,ExecIf($["${D}" != ""]?Dial(${D:1},%d,m(ringback)))' % (max(1, int(r['rings'])) * RING_SEC),
              ' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?end)']
    x += [' same => n,ExecIf($[${STAT(e,%s/nobody.wav)}]?Playback(%s/nobody))' % (SOUNDS, SOUNDS),
          ' same => n,VoiceMail(%s@teco,s)' % cfg['vm_box'], ' same => n(end),Hangup()', '']
    # --- govorna pošta
    vm = ['; generiše Teco.Pi — ne menjati ručno', '[teco]', '%s => %s,Opšta pošta' % (cfg['vm_box'], cfg.get('vm_pin') or '0000')]
    for n, e in sorted(ext.items()):
        vm.append('%s => %s,%s' % (n, e.get('vmpin') or '0000', e['name'].replace(',', ' ')))
    ari = ['; generiše Teco.Pi', '[teco]', 'type=user', 'read_only=no', 'password=%s' % ari_pass]
    return {'pjsip.conf': '\n'.join(pj) + '\n', 'extensions.conf': '\n'.join(x) + '\n', 'voicemail.conf': '\n'.join(vm) + '\n', 'ari.conf': '\n'.join(ari) + '\n'}


def ringback_wav(path):
    """Ton povratnog poziva (425 Hz, 1 s / 4 s) za Dial m(ringback) — pozivalac čuje da zvoni."""
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 8000
    import math
    on = b''.join(struct.pack('<h', int(8000 * math.sin(2 * math.pi * 425 * i / rate))) for i in range(rate))
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(on + bytes(4 * rate * 2))


def voicemails():
    """Poruke govorne pošte: [{box, folder, id, from, t, dur, new}] (najnovije prve)."""
    out = []
    if not VM_DIR.exists():
        return out
    for txt in VM_DIR.glob('*/*/msg*.txt'):
        try:
            info = dict(l.split('=', 1) for l in txt.read_text(errors='replace').splitlines() if '=' in l and not l.startswith(';'))
        except OSError:
            continue
        out.append({'box': txt.parent.parent.name, 'folder': txt.parent.name, 'id': txt.stem, 'from': info.get('callerid', ''),
                    't': int(info.get('origtime', 0) or 0), 'dur': int(info.get('duration', 0) or 0), 'new': txt.parent.name == 'INBOX'})
    return sorted(out, key=lambda m: -m['t'])


class Media:
    """Most za zvuk: Asterisk externalMedia (RTP ulaw 8 kHz, 20 ms) <-> ALSA (aplay / arecord).
    Bez kartice (simulator): šalje tišinu, prima i odbacuje — tok poziva radi i bez hardvera."""

    def __init__(self, device):
        self.device = device
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.peer, self.alive = None, True
        self.play = self.rec = None
        if device:
            try:
                self.play = subprocess.Popen(['aplay', '-q', '-D', device, '-f', 'S16_LE', '-r', '8000', '-c', '1', '-t', 'raw'], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.rec = subprocess.Popen(['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', '8000', '-c', '1', '-t', 'raw'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                self.play = self.rec = None
        threading.Thread(target=self._rx, daemon=True, name='rtp-rx').start()
        threading.Thread(target=self._tx, daemon=True, name='rtp-tx').start()

    def _rx(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self.peer = addr
            if self.play and len(data) > 12:
                try:
                    self.play.stdin.write(audioop.ulaw2lin(data[12:], 2))
                except (OSError, ValueError):
                    self.play = None

    def _tx(self):
        seq, ts, ssrc = random.randint(0, 65535), random.randint(0, 1 << 30), random.randint(0, 1 << 30)
        silence = bytes(320)
        nxt = time.monotonic()
        while self.alive:
            pcm = None
            if self.rec:
                pcm = self.rec.stdout.read(320)
                if not pcm or len(pcm) < 320:
                    self.rec, pcm = None, None
            if pcm is None:   # bez kartice: tišina u ritmu od 20 ms
                nxt += 0.02
                time.sleep(max(0, nxt - time.monotonic()))
                pcm = silence
            if self.peer:
                hdr = struct.pack('!BBHII', 0x80, 0, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
                try:
                    self.sock.sendto(hdr + audioop.lin2ulaw(pcm, 2), self.peer)
                except OSError:
                    pass
            seq, ts = seq + 1, ts + 160

    def close(self):
        self.alive = False
        for p in (self.play, self.rec):
            if p:
                try:
                    p.kill()
                except OSError:
                    pass
        try:
            self.sock.close()
        except OSError:
            pass


class Ari:
    """ARI klijent: REST + WebSocket događaji za Stasis aplikaciju 'teco'."""

    def __init__(self, user, password, on_event, base='http://127.0.0.1:8088/ari'):
        import aiohttp
        self.aiohttp = aiohttp
        self.base, self.auth = base, aiohttp.BasicAuth(user, password)
        self.on_event = on_event
        self.ok = False
        self.session = None

    async def req(self, method, path, **params):
        if not self.session:
            self.session = self.aiohttp.ClientSession(auth=self.auth, timeout=self.aiohttp.ClientTimeout(total=10))
        params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None}
        async with self.session.request(method, self.base + path, params=params) as r:
            body = await r.text()
            if r.status >= 300:
                raise OSError('ARI %s %s: %s %s' % (method, path, r.status, body[:120]))
            return json.loads(body) if body.strip() else None

    async def run(self):
        while True:
            try:
                if not self.session:
                    self.session = self.aiohttp.ClientSession(auth=self.auth, timeout=self.aiohttp.ClientTimeout(total=10))
                async with self.session.ws_connect(self.base.replace('http', 'ws') + '/events', params={'app': 'teco', 'subscribeAll': 'false'},
                                                   heartbeat=30) as ws:
                    self.ok = True
                    self.on_event({'type': '_connected'})
                    async for msg in ws:
                        if msg.type == self.aiohttp.WSMsgType.TEXT:
                            try:
                                self.on_event(json.loads(msg.data))
                            except Exception:
                                import logging
                                logging.getLogger('teco').exception('ARI događaj')
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self.ok:
                self.ok = False
                self.on_event({'type': '_disconnected'})
            await asyncio.sleep(3)
