import subprocess
#!/usr/bin/env python3
"""Teco.Pi web server.

Daljinski i admin za MMC: upravlja mpv-om (YouTube lista i slajdšou), izvorima
AV1/AV2/AV3/FM, FM radiom preko RTL-SDR-a, relejima i prikazuje stanje sistema.
Stranica dobija stanje preko WebSocket-a (/ws), a slike se otpremaju na /api/upload.
"""
import asyncio
import glob
import hashlib
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import signal
import time
from pathlib import Path

from aiohttp import WSMsgType, web

import hw       # I2C tjuner i pojačalo, IR prijemnik
import splash   # Teco.Pi logo (pri pokretanju i kad nema šta da se prikaže)

BASE = Path(__file__).resolve().parent
STATIC = BASE / 'static'
DATA = BASE / 'data'
TV_FILE = DATA / 'tv_channels.json'
YT_COOKIES = DATA / 'yt-cookies.txt'   # kolačići YouTube naloga (kad YouTube traži    # [{'n', 'g', 'c' (kategorija), 'u'}], pravi se proverom strimova
IMAGES = BASE / 'images'
CAST_UNIT = 'teco-cast.service'   # korisnička usluga za cast (cast/cast.mjs)
ALARM_UNIT = 'teco-alarm.service'   # Paradox alarm (alarm/alarm_bridge.py, PAI)
ALARM_CFG = DATA / 'alarm.json'     # IP modul: adresa, port, lozinke (chmod 600, ne ide na stranicu)
ALARM_API = 'http://127.0.0.1:8098'
ALARM_CMDS = {'arm': 'Uključen (Regular)', 'arm_stay': 'Uključen (Stay)', 'arm_instant': 'Uključen (Instant)',
              'arm_force': 'Uključen (Force)', 'disarm': 'Isključen'}
PGM_CMDS = {'on': 'uključen', 'off': 'isključen', 'release': 'vraćen na automatski rad', 'pulse': 'uključen na 5 s'}
NTP_CONF = Path('/etc/systemd/timesyncd.conf.d/teco.conf')   # NTP serveri iz podešavanja mreže
SMB_CONF = Path('/etc/samba/smb.conf')   # Teco.Pi ga piše ceo (deljenje fajlova iz admina)
SHARE = BASE / 'share'                   # deljeni folder na SD kartici (\\tecopi\Teco)
USB_MNT = Path('/media/teco')            # USB diskovi: /media/teco/<ime>
USB_FS = {'vfat': 'vfat', 'exfat': 'exfat', 'ntfs': 'ntfs3', 'ext4': 'ext4', 'ext3': 'ext3', 'ext2': 'ext2'}
SMB_BIN = Path('/usr/sbin/smbd')
OPL_DIRS = ('APPS', 'ART', 'CD', 'CFG', 'CHT', 'DVD', 'LNG', 'POPS', 'THM', 'VMC')   # folderi koje Open PS2 Loader očekuje
GAME_EXT = {'.iso': None, '.zso': None, '.vcd': 'POPS'}   # None: CD (do 700 MB) ili DVD
CD_MAX = 700 * 2**20
USER = pwd.getpwuid(os.getuid()).pw_name   # Samba deli fajlove kao ovaj korisnik (i za prijavu sa Windows-a)
STATE_FILE = DATA / 'state.json'
MPV_SOCK = '/tmp/mpvsocket'
FM_SOCK = '/tmp/mpvfm'
ST_SOCK = '/tmp/mpvstream'
HELPER_SOCKS = (FM_SOCK, ST_SOCK)   # pomoćni mpv-ovi za zvuk (FM i internet radio)
SOURCES = ('av1', 'av2', 'av3', 'av4', 'fm', 'st', 'tv')
# av1 je YouTube (Pi); ulazi za konzole su interno av2-av4, a na ekranu se zovu AV1-AV3
AVS = ('av2', 'av3', 'av4')
AVL = {'av2': 'AV1', 'av3': 'AV2', 'av4': 'AV3'}
# TV kreće brže: najmanji kvalitet, samo poslednji segment uživo i kraće ispitivanje strima (~5 s umesto ~10 s)
TV_OPTS = 'hls-bitrate=min,demuxer-lavf-o=%19%live_start_index=-1,demuxer-lavf-analyzeduration=0.5,demuxer-lavf-probesize=500000'
SYS_TV = [   # besplatni kanali uživo, provereno sa Teco.Pi iz Srbije (septembar 2026)
    {'n': 'DW English', 'g': 'Vesti · Nemačka', 'u': 'https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8'},
    {'n': 'Al Jazeera', 'g': 'Vesti · Katar', 'u': 'https://live-hls-apps-aje-fa.getaj.net/AJE/index.m3u8'},
    {'n': 'France 24', 'g': 'Vesti · Francuska', 'u': 'https://live.france24.com/hls/live/2037218-b/F24_EN_HI_HLS/master_5000.m3u8'},
    {'n': 'TRT World', 'g': 'Vesti · Turska', 'u': 'https://tv-trtworld.medya.trt.com.tr/master.m3u8'},
    {'n': 'CNA', 'g': 'Vesti · Singapur', 'u': 'https://d2e1asnsl7br7b.cloudfront.net/7782e205e72f43aeb4a48ec97f66ebbe/index.m3u8'},
    {'n': 'Bloomberg', 'g': 'Biznis · SAD', 'u': 'https://www.bloomberg.com/media-manifest/streams/us.m3u8'},
    {'n': 'RTRS', 'g': 'Program · Banja Luka', 'u': 'https://parh.rtrs.tv/tv/live/playlist.m3u8'},
    {'n': 'TV5Monde Info', 'g': 'Vesti · Francuska', 'u': 'https://ott.tv5monde.com/Content/HLS/Live/channel(info)/index.m3u8'},
    {'n': 'Arirang', 'g': 'Program · Koreja', 'u': 'https://amdlive-ch01-ctnd-com.akamaized.net/arirang_1ch/smil:arirang_1ch.smil/playlist.m3u8'},
    {'n': 'Red Bull TV', 'g': 'Sport · Austrija', 'u': 'https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8'},
]
SYS_STREAMS = [   # svetske stanice, provereno da rade sa Teco.Pi (septembar 2026)
    {'n': 'Radio Paradise', 'g': 'Eclectic · SAD', 'u': 'http://stream.radioparadise.com/mp3-128'},
    {'n': 'KEXP Seattle', 'g': 'Indie · SAD', 'u': 'https://kexp-mp3-128.streamguys1.com/kexp128.mp3'},
    {'n': 'FIP', 'g': 'Eclectic · Pariz', 'u': 'https://icecast.radiofrance.fr/fip-midfi.mp3'},
    {'n': 'Classic FM', 'g': 'Klasika · London', 'u': 'https://media-ice.musicradio.com/ClassicFMMP3'},
    {'n': 'Capital FM', 'g': 'Hitovi · London', 'u': 'https://media-ssl.musicradio.com/CapitalMP3'},
    {'n': 'Rock Antenne', 'g': 'Rock · Nemačka', 'u': 'https://stream.rockantenne.de/rockantenne/stream/mp3'},
    {'n': 'SomaFM Groove Salad', 'g': 'Chill · SAD', 'u': 'https://ice1.somafm.com/groovesalad-128-mp3'},
    {'n': 'Radio Swiss Jazz', 'g': 'Jazz · Švajcarska', 'u': 'http://stream.srg-ssr.ch/m/rsj/mp3_128'},
    {'n': 'NTS Radio', 'g': 'Underground · London', 'u': 'https://stream-relay-geo.ntslive.net/stream'},
    {'n': 'Radio Nova', 'g': 'Groove · Pariz', 'u': 'http://novazz.ice.infomaniak.ch/novazz-128.mp3'},
    # rok: 80-e, 90-e, 2000-e, klasika (provereno sa Teco.Pi, septembar 2026)
    {'n': 'Rock Antenne 80er', 'g': 'Rok 80-ih · Nemačka', 'c': 'r80', 'u': 'https://stream.rockantenne.de/80er-rock/stream/mp3'},
    {'n': 'Bob 80s Rock', 'g': 'Rok 80-ih · Nemačka', 'c': 'r80', 'u': 'https://streams.radiobob.de/bob-80srock/mp3-192/streams.radiobob.de/'},
    {'n': 'SomaFM Underground 80s', 'g': 'Novi talas 80-ih · SAD', 'c': 'r80', 'u': 'https://ice1.somafm.com/u80s-128-mp3'},
    {'n': 'Rock Antenne 90er', 'g': 'Rok 90-ih · Nemačka', 'c': 'r90', 'u': 'https://stream.rockantenne.de/90er-rock/stream/mp3'},
    {'n': 'Bob 90s Rock', 'g': 'Rok 90-ih · Nemačka', 'c': 'r90', 'u': 'https://streams.radiobob.de/bob-90srock/mp3-192/streams.radiobob.de/'},
    {'n': 'Bob Grunge', 'g': 'Grunge 90-ih · Nemačka', 'c': 'r90', 'u': 'https://streams.radiobob.de/bob-grunge/mp3-192/streams.radiobob.de/'},
    {'n': 'Bob 2000er Rock', 'g': 'Rok 2000-ih · Nemačka', 'c': 'r00', 'u': 'https://streams.radiobob.de/bob-2000srock/mp3-192/streams.radiobob.de/'},
    {'n': 'Rock Antenne Classic', 'g': 'Klasični rok · Nemačka', 'c': 'rclassic', 'u': 'https://stream.rockantenne.de/classic-perlen/stream/mp3'},
    {'n': 'Bob Classic Rock', 'g': 'Klasični rok · Nemačka', 'c': 'rclassic', 'u': 'https://streams.radiobob.de/bob-classicrock/mp3-192/streams.radiobob.de/'},
    {'n': 'Radio Caroline', 'g': 'Klasični rok · UK', 'c': 'rclassic', 'u': 'http://sc6.radiocaroline.net:8040/stream'},
    {'n': 'Radio Bob', 'g': 'Rok · Nemačka', 'c': 'rock', 'u': 'https://streams.radiobob.de/bob-national/mp3-192/streams.radiobob.de/'},
    {'n': 'Radio Paradise Rock', 'g': 'Rok · SAD', 'c': 'rock', 'u': 'http://stream.radioparadise.com/rock-128'},
    {'n': 'Virgin Radio Italia', 'g': 'Rok · Italija', 'c': 'rock', 'u': 'https://icecast.unitedradio.it/Virgin.mp3'},
    {'n': 'Star FM Berlin', 'g': 'Rok · Berlin', 'c': 'rock', 'u': 'https://stream.starfm.de/berlin/mp3-192/'},
    {'n': 'Rock FM España', 'g': 'Rok · Španija', 'c': 'rock', 'u': 'https://rockfm-cope-rrcast.flumotion.com/cope/rockfm-low.mp3'},
    {'n': 'Rock Antenne Alternative', 'g': 'Alternativa · Nemačka', 'c': 'ralt', 'u': 'https://stream.rockantenne.de/alternative/stream/mp3'},
    {'n': 'Bob Alternative', 'g': 'Alternativa · Nemačka', 'c': 'ralt', 'u': 'https://streams.radiobob.de/bob-alternative/mp3-192/streams.radiobob.de/'},
    {'n': 'Bob Hard Rock', 'g': 'Hard rok · Nemačka', 'c': 'ralt', 'u': 'https://streams.radiobob.de/bob-hardrock/mp3-192/streams.radiobob.de/'},
    {'n': 'Rock Antenne Metal', 'g': 'Metal · Nemačka', 'c': 'ralt', 'u': 'https://stream.rockantenne.de/heavy-metal/stream/mp3'},
    {'n': 'Rock Antenne Soft Rock', 'g': 'Soft rok · Nemačka', 'c': 'rock', 'u': 'https://stream.rockantenne.de/soft-rock/stream/mp3'},
]
YTDLP = str(Path.home() / '.local/yt-dlp-venv/bin/yt-dlp')
YT_FMT = 'bestvideo[vcodec^=avc1][height<=480]+bestaudio/best[height<=480]'
PORT = int(os.environ.get('TECO_PORT', '80'))
FMIN, FMAX = 87.5, 108.0

log = logging.getLogger('teco')

DEFAULT_STATE = {
    'pin': None,                       # {'salt': hex, 'hash': hex}
    'names': {'av2': '', 'av3': '', 'av4': ''},   # prazno = ime konzole
    'cons': {'av2': 'ps1', 'av3': 'n64', 'av4': 'ps2'},
    'fm': {'f': 98.5, 'list': []},
    'stream': {'u': None, 'list': []},  # internet radio: moje stanice [{'n': ime, 'u': url}], u = poslednja puštena
    'tv': {'u': None, 'list': []},      # TV uživo: moji kanali, u = poslednji pušten
    'boot': 'last',                     # posle uključivanja: 'last' (kao pre gašenja) ili izvor (av1, fm, ...)
    'events': {},                      # popunjava se iz DEFAULT_EVENTS pri pokretanju
    'slide_sec': 10,
    'idle': 'slides',                 # šta je na ekranu kad nema videa: 'slides' ili 'clock'
    'relays': [
        {'n': 'AV1', 'pin': 22, 'on': False, 'role': 'av2'},
        {'n': 'AV2', 'pin': 23, 'on': False, 'role': 'av3'},
        {'n': 'AV3', 'pin': 24, 'on': False, 'role': 'av4'},
        {'n': 'Tuner', 'pin': 27, 'on': False, 'role': 'tuner'},
        {'n': 'Gašenje monitora', 'pin': 5, 'on': False, 'role': 'monitor'},   # monitor je na NC kontaktu
    ],
    'ir': [
        {'key': 'OK', 'code': 'NEC 0x20DF22DD', 'cmd': 'play'},
        {'key': 'Play', 'code': 'NEC 0x20DFA25D', 'cmd': 'play'},
        {'key': 'Stop', 'code': 'NEC 0x20DF8D72', 'cmd': 'stop'},
        {'key': 'Desno', 'code': 'NEC 0x20DF609F', 'cmd': 'fwd'},
        {'key': 'Levo', 'code': 'NEC 0x20DFE01F', 'cmd': 'back'},
        {'key': 'CH +', 'code': 'NEC 0x20DF00FF', 'cmd': 'next'},
        {'key': 'CH −', 'code': 'NEC 0x20DF807F', 'cmd': 'prev'},
        {'key': 'Vol +', 'code': 'NEC 0x20DF40BF', 'cmd': 'volup'},
        {'key': 'Vol −', 'code': 'NEC 0x20DFC03F', 'cmd': 'voldn'},
        {'key': 'Mute', 'code': 'NEC 0x20DF906F', 'cmd': 'mute'},
        {'key': '1', 'code': 'NEC 0x20DF8877', 'cmd': 'av1'},
        {'key': '2', 'code': 'NEC 0x20DF48B7', 'cmd': 'av2'},
        {'key': '3', 'code': 'NEC 0x20DFC837', 'cmd': 'av3'},
        {'key': '4', 'code': 'NEC 0x20DF28D7', 'cmd': 'av4'},
        {'key': '5', 'code': 'NEC 0x20DFA857', 'cmd': 'fm'},
        {'key': '6', 'code': 'NEC 0x20DF6897', 'cmd': 'st'},
        {'key': '7', 'code': 'NEC 0x20DFE817', 'cmd': 'tv'},
        {'key': 'Power', 'code': 'NEC 0x20DF10EF', 'cmd': 'standby'},
    ],
}
# brojevi na virtuelnom daljinskom: 1 YT, 2-4 AV1-AV3, 5 FM, 6 internet radio, 7 TV (i za postojeća podešavanja)
IR_NUM_KEYS = [('4', 'NEC 0x20DF28D7', 'av4'), ('5', 'NEC 0x20DFA857', 'fm'), ('6', 'NEC 0x20DF6897', 'st'), ('7', 'NEC 0x20DFE817', 'tv')]
IR_KEY8 = 'NEC 0x20DF18E7'   # LG taster 8 (ranije RND); 9 je 0x20DF9867 — za sada bez komande

EVENTS_DIR = BASE / 'events'
AUDIO_EXT = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.opus', '.aac')
# Događaji (zvono, interfon): slika preko ekrana + zvuk. Zvuk je bilo koji audio fajl u events/<id>/.
DEFAULT_EVENTS = {
    'zvono': {'n': 'Zvono na vratima', 'on': True, 'sec': 15, 'pause': True},
    'interfon': {'n': 'Interfon', 'on': True, 'sec': 20, 'pause': True},
    'pozar': {'n': 'Požarni alarm', 'on': True, 'sec': 30, 'pause': True},
    'voda': {'n': 'Curenje vode', 'on': True, 'sec': 30, 'pause': True},
}
EVENT_SUB = {'zvono': 'Pozvonio je neko', 'interfon': 'Pozvonio je neko', 'pozar': 'Proveri odmah!', 'voda': 'Proveri odmah!'}
ALARMS = {'pozar', 'voda'}   # zvuk se ponavlja dok je slika na ekranu i prekidaju zvono/interfon
EVENT_LOOK = {   # boja i ikonica (viewBox 0 0 100 100)
    'zvono': ('#fbbf24', '<path d="M50 14c-3 0-5 2-5 5v3c-12 3-20 13-20 26v16l-7 9v3h64v-3l-7-9V48c0-13-8-23-20-26v-3c0-3-2-5-5-5z" fill="currentColor"/>'
                         '<path d="M40 80a10 10 0 0 0 20 0z" fill="currentColor"/>'
                         '<path d="M16 30c3-8 8-14 14-18M84 30c-3-8-8-14-14-18" stroke="currentColor" stroke-width="5" fill="none" stroke-linecap="round"/>'),
    'interfon': ('#2dd4bf', '<rect x="26" y="10" width="48" height="80" rx="9" fill="none" stroke="currentColor" stroke-width="5"/>'
                            '<g fill="currentColor">' + ''.join('<circle cx="%d" cy="%d" r="2.6"/>' % (x, y) for y in (26, 33, 40) for x in (40, 47, 54, 61)) + '</g>'
                            '<rect x="36" y="52" width="28" height="16" rx="4" fill="currentColor" opacity=".35"/>'
                            '<circle cx="50" cy="78" r="5" fill="currentColor"/>'),
    'pozar': ('#f87171', '<path d="M52 6c3 16 26 26 26 54a28 28 0 0 1-56 0c0-14 7-22 13-29 1 10 6 15 11 16-4-16 1-30 6-41z" fill="currentColor"/>'
                         '<path d="M50 50c4 8 12 12 12 22a12 12 0 0 1-24 0c0-8 6-12 12-22z" fill="#fde68a"/>'),
    'voda': ('#60a5fa', '<path d="M50 8C40 26 26 40 26 56a24 24 0 0 0 48 0C74 40 60 26 50 8z" fill="currentColor"/>'
                        '<path d="M38 58a13 13 0 0 0 12 13" stroke="#0b1018" stroke-opacity=".45" stroke-width="5" fill="none" stroke-linecap="round"/>'
                        '<path d="M10 92c7-5 13-5 20 0s13 5 20 0 13-5 20 0 13 5 20 0" stroke="currentColor" stroke-width="4" fill="none" stroke-linecap="round"/>'),
}

ADMIN_CMDS = {'reboot', 'boot', 'theme','radio_clock', 'yt_cookies_del', 'wifi_scan', 'wifi_connect', 'wifi_forget', 'tv_add', 'tv_del', 'ev_set', 'ev_test', 'ev_sound_del', 'q_move', 'idle', 'img_del', 'slide_sec',
              'bt_scan', 'bt_pair', 'bt_connect', 'bt_disconnect', 'bt_remove', 'pin_req', 'cast', 'smb', 'smb_usb', 'smb_install', 'smb_eject', 'smb_pw', 'smb_ls', 'smb_dir', 'smb_opl', 'game_del', 'net_set', 'dns_set', 'ntp_set', 'alarm_cfg', 'relay', 'ir_save', 'ir_del',
              'cons', 'name', 'fm_save', 'fm_del', 'st_add', 'st_del', 'set_pin'}


# ---------------------------------------------------------------- stanje na disku

def load_state():
    st = json.loads(json.dumps(DEFAULT_STATE))
    try:
        st.update(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return st


def save_state(st):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE_FILE)


SLIDE_W, SLIDE_H = 720, 576      # rezolucija izlaza (PAL)
SCREEN_ASPECT = 16 / 9           # oblik ekrana monitora; mpv razvlači 720x576 na ceo ekran (keepaspect=no)


def slide_image(im):
    """Cela slika na ekranu, bez sečenja i razvlačenja; prazno polje = zamućena ista slika."""
    from PIL import ImageFilter, ImageEnhance, ImageOps, Image
    vw, vh = round(SLIDE_H * SCREEN_ASPECT), SLIDE_H          # platno u kvadratnim pikselima, kao na ekranu
    bg = ImageOps.fit(im, (vw // 4, vh // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
    bg = ImageEnhance.Brightness(bg.resize((vw, vh), Image.BILINEAR)).enhance(0.45)
    fg = im.copy()
    fg.thumbnail((vw, vh), Image.LANCZOS)
    bg.paste(fg, ((vw - fg.width) // 2, (vh - fg.height) // 2))
    return bg.resize((SLIDE_W, SLIDE_H), Image.LANCZOS)          # stisne se u 720x576, ekran ga vrati u pravi oblik


def pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac('sha256', pin.encode(), bytes.fromhex(salt), 100_000).hex()


class JscWorker:
    """Stalno upaljen Node (jsc_worker.js) umesto novog Node-a za svaki YouTube video.
    yt-dlp šalje ceo program (rešavač + plejer ~4 MB + zadaci); rešavač i plejer se šalju samo prvi put,
    posle samo zadaci. Poziva se iz yt-dlp niti (ytpool), zato brava; ako nešto ne valja, yt-dlp radi po starom."""
    MARK = 'console.log(JSON.stringify(jsc('

    def __init__(self):
        import threading
        self.lock, self.p, self.n = threading.Lock(), None, 0
        self.heads, self.players = set(), set()

    def _start(self):
        self.p = subprocess.Popen(['node', str(BASE / 'jsc_worker.js')], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
        self.heads.clear()
        self.players.clear()

    def run(self, stdin):
        import threading
        i = stdin.rfind(self.MARK)
        tail = stdin[i + len(self.MARK):].rstrip()
        data = json.loads(tail[:tail.rfind(')));')])
        field = 'preprocessed_player' if data.get('type') == 'preprocessed' else 'player'
        player = data.pop(field)
        head = stdin[:i]
        key = hashlib.sha1(head.encode()).hexdigest()
        pkey = hashlib.sha1(player.encode()).hexdigest()
        with self.lock:
            for _ in range(3):
                if not self.p or self.p.poll() is not None:
                    self._start()
                self.n += 1
                msg = {'id': self.n, 'key': key, 'pkey': pkey, 'data': data}
                if key not in self.heads:
                    msg['head'] = head
                if pkey not in self.players:
                    msg['player'] = player
                p = self.p
                dog = threading.Timer(40, p.kill)   # zaglavljen Node: ubij ga, sledeći poziv pali novi
                dog.start()
                try:
                    p.stdin.write(json.dumps(msg) + '\n')
                    p.stdin.flush()
                    line = p.stdout.readline()
                except (OSError, ValueError):
                    line = ''
                finally:
                    dog.cancel()
                if not line:
                    self.p = None
                    continue
                r = json.loads(line)
                if r.get('err') in ('need_head', 'need_player'):   # Node čuva samo poslednja 2: pošalji ponovo
                    self.heads.discard(key)
                    self.players.discard(pkey)
                    continue
                if 'err' in r:
                    raise RuntimeError(r['err'])
                self.heads.add(key)
                self.players.add(pkey)
                return r['out']
        raise RuntimeError('jsc_worker ne odgovara')


# ---------------------------------------------------------------- mpv IPC

class Mpv:
    """Trajna veza sa mpv-om preko JSON IPC-a; prati svojstva i šalje komande."""
    PROPS = ['pause', 'time-pos', 'duration', 'media-title', 'volume', 'mute',
             'playlist', 'playlist-pos', 'idle-active', 'path']

    def __init__(self, sock, on_change):
        self.sock = sock
        self.on_change = on_change
        self.props = {}
        self.writer = None
        self.rid = 0
        self.pending = {}
        self.connected = False

    async def run(self):
        while True:
            try:
                reader, self.writer = await asyncio.open_unix_connection(self.sock, limit=1 << 20)
                self.connected = True
                log.info('mpv povezan')
                for i, p in enumerate(self.PROPS, 1):
                    await self._send({'command': ['observe_property', i, p]})
                self.on_change('connected')
                async for line in reader:
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    rid = m.get('request_id')
                    if rid in self.pending:
                        fut = self.pending.pop(rid)
                        if not fut.done():
                            fut.set_result(m)
                    elif m.get('event') == 'property-change':
                        self.props[m['name']] = m.get('data')
                        self.on_change(m['name'])
                    elif m.get('event'):
                        self.on_change('event:' + m['event'])
            except (OSError, ConnectionError):
                pass
            if self.connected:
                log.warning('mpv veza prekinuta')
            self.connected = False
            for fut in self.pending.values():
                fut.cancel()
            self.pending.clear()
            await asyncio.sleep(1)

    async def _send(self, obj):
        self.writer.write((json.dumps(obj) + '\n').encode())
        await self.writer.drain()

    async def cmd(self, *args, timeout=5):
        if not self.connected:
            return None
        self.rid += 1
        rid = self.rid
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self._send({'command': list(args), 'request_id': rid})
            m = await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError, OSError, ConnectionError):
            self.pending.pop(rid, None)
            return None
        return m.get('data', True) if m.get('error') == 'success' else None


async def mpv_oneshot(sock, *args):
    """Jedna komanda za pomoćni mpv (FM), bez trajne veze."""
    try:
        r, w = await asyncio.open_unix_connection(sock)
        w.write((json.dumps({'command': list(args)}) + '\n').encode())
        await w.drain()
        w.close()
    except (OSError, ConnectionError):
        pass


async def mpv_query(sock, prop):
    """Čita jedno svojstvo pomoćnog mpv-a (npr. naslov pesme sa internet radija)."""
    try:
        r, w = await asyncio.open_unix_connection(sock)
        w.write((json.dumps({'command': ['get_property', prop], 'request_id': 1}) + '\n').encode())
        await w.drain()
        try:
            while True:
                line = await asyncio.wait_for(r.readline(), 2)
                if not line:
                    return None
                m = json.loads(line)
                if m.get('request_id') == 1:
                    return m.get('data') if m.get('error') == 'success' else None
        finally:
            w.close()
    except (OSError, ConnectionError, ValueError, asyncio.TimeoutError):
        return None


# ---------------------------------------------------------------- glavna logika

class Teco:
    def __init__(self):
        self.st = load_state()
        self.clients = set()
        self.tokens = set()
        self.meta = {}          # filename u mpv-u -> {'t': naslov, 'd': trajanje, 'url': youtube link}
        try:                    # čuva se na disku, da naslovi na listi prežive restart servera
            self.meta = json.loads((DATA / 'meta.json').read_text())
        except (OSError, ValueError):
            pass
        self.mode = 'idle'      # 'yt' | 'slides' | 'idle'
        self.source = 'av1'
        self.fm_proc = None
        self.fm_err = ''
        self.st_proc = None     # internet radio (pomoćni mpv)
        self.st_err = ''
        self.st_title = ''      # naslov pesme koji šalje stanica (icy-title)
        from concurrent.futures import ThreadPoolExecutor
        self.ytpool = ThreadPoolExecutor(max_workers=2)
        self.ytdlp_ok = False   # postaje True kad se yt_dlp modul učita (vidi warm_ytdlp)
        self.pending = []       # linkovi koji se pripremaju: {'id', 'url', 't', 'err'}
        self.pid = 0
        self.rcache = {}        # ID videa -> pripremljeni linkovi (važe dok ne istekne 'expire')
        self.sys = {}
        self.cpu_hist, self.room_hist, self.out_hist = [], [], []
        self.cpu_prev = None
        self.dht_last = None
        self.audio, self.bt, self.net = [], {}, {}
        self.wifi = {'list': [], 'scanning': False, 'busy': None}
        self.sink_full = set()   # izlazi zvuka kojima je jačina već podignuta na 100%
        self.i2c, self.i2c_lock = None, asyncio.Lock()
        self.tuner = self.amp = None   # hw.RDA5807 / hw.TPA2016 kad su povezani
        self.fm_task = None
        self.ir_learn_until = 0        # dok se u adminu snima taster, daljinski ne izvršava komande
        self._ir_first = self._ir_rep = 0.0
        self.cast_proc = None
        self.cast_sender = None
        self.ev_now = None      # događaj koji je trenutno na ekranu
        for eid, ev in DEFAULT_EVENTS.items():   # novi događaji dobijaju podrazumevana podešavanja
            self.st.setdefault('events', {}).setdefault(eid, dict(ev))
        self.st.setdefault('stream', {'u': None, 'list': []})
        self.st.setdefault('tv', {'u': None, 'list': []})
        for k in AVS:   # novi AV ulaz dobija podrazumevana podešavanja
            self.st['names'].setdefault(k, '')
            self.st['cons'].setdefault(k, DEFAULT_STATE['cons'][k])
        if not self.st.get('ir_v2'):   # brojevi 4-7 dobijaju novi raspored (AV3 je dodat)
            for key, code, cmd in IR_NUM_KEYS:
                self.st['ir'] = [x for x in self.st['ir'] if x['code'] != code]
                self.st['ir'].append({'key': key, 'code': code, 'cmd': cmd})
            self.st['ir_v2'] = True
        if not self.st.get('relays_v2'):   # releji po planu: AV1-AV3, tjuner, gašenje monitora
            self.st['relays'] = json.loads(json.dumps(DEFAULT_STATE['relays']))
            self.st['relays_v2'] = True
        for x in self.st['ir']:   # Power na virtuelnom daljinskom = Stand by (r0 je sada AV1 rele)
            if x['code'] == 'NEC 0x20DF10EF' and x['cmd'] == 'r0':
                x['cmd'] = 'standby'
        if not self.st.get('ir_v9'):   # daljinski 1-9 (3×3): RND je sklonjen, njegov kod je LG taster 8 — ostaje slobodan
            self.st['ir'] = [x for x in self.st['ir'] if not (x['code'] == IR_KEY8 and x['cmd'] == 'random')]
            self.st['ir_v9'] = True
        self.yt_snap = None     # YouTube lista sačuvana dok je na ekranu TV (vraća se na AV1)
        self.booted = False     # postaje True kad boot_restore vrati stanje od pre gašenja
        self.logo_on = False    # Teco.Pi logo je na ekranu (overlay 0)
        self.tv_err = ''
        self.tv_loading = False
        self.clock_key = None   # šta je trenutno nacrtano na satu (None = sat nije na ekranu)
        self.clock_raw = None
        self.dirty = asyncio.Event()
        self.mpv = Mpv(MPV_SOCK, self._mpv_changed)
        self.gpio = []
        self._init_relays()

    # ---------- releji
    def _init_relays(self):
        try:
            from gpiozero import OutputDevice
        except ImportError:
            OutputDevice = None
        for r in self.st['relays']:
            dev = None
            if OutputDevice:
                try:
                    dev = OutputDevice(r['pin'], initial_value=bool(r['on']))
                except Exception as e:  # pin zauzet ili nema GPIO-a
                    log.warning('rele na GPIO%s nije dostupan: %s', r['pin'], e)
            self.gpio.append(dev)

    SRC_ROLES = ('av2', 'av3', 'av4', 'tuner')   # releji ulaza: uvek najviše jedan uključen

    def _relay_hw(self, i, on):
        self.st['relays'][i]['on'] = bool(on)
        if self.gpio[i]:
            self.gpio[i].value = bool(on)

    def relay_set(self, i, on):
        """Uključuje/isključuje rele uz zaštitu: samo jedan AV ulaz ili tjuner istovremeno,
        a gašenje monitora samo kad nijedan ulaz nije uključen. Vraća poruku ako nije dozvoljeno."""
        rl = self.st['relays']
        if not 0 <= i < len(rl):
            return None
        role = rl[i].get('role')
        if on and role == 'monitor' and any(r['on'] for r in rl if r.get('role') in self.SRC_ROLES):
            return 'Monitor se gasi samo kad su AV ulazi i tjuner isključeni.'
        if on and role in self.SRC_ROLES:
            for k, r in enumerate(rl):
                if k != i and r['on'] and (r.get('role') in self.SRC_ROLES or r.get('role') == 'monitor'):
                    self._relay_hw(k, False)   # drugi ulaz se isključuje, monitor se pali
        self._relay_hw(i, on)
        save_state(self.st)
        return None

    # ---------- mpv događaji
    def _mpv_changed(self, name):
        if name == 'connected':
            # posle restarta servera izvor je AV1: skloni sliku konzole ako je ostala na ekranu
            asyncio.get_running_loop().create_task(self.hide_console())
        if name == 'idle-active' and self.mpv.props.get('idle-active') and self.mode == 'yt':
            asyncio.get_running_loop().create_task(self.start_slides())
        if getattr(self, 'yt_loading', None) and self.mode == 'yt' and name == 'event:playback-restart':
            asyncio.get_running_loop().create_task(self.yt_loaded())   # video je krenuo: skloni sliku „Učitavam“
        if self.mode == 'tv':
            if name == 'event:playback-restart' and self.tv_loading:   # prva slika novog kanala
                self.tv_loading = False   # kanal je krenuo: skloni sliku „Učitavam“
                asyncio.get_running_loop().create_task(self.hide_console())
            elif name == 'idle-active' and self.mpv.props.get('idle-active'):
                self.tv_loading = False
                self.tv_err = 'Kanal trenutno ne radi.'
                asyncio.get_running_loop().create_task(self.show_tv())
        if name == 'path' and self.mode == 'yt':
            m = self.meta.get(self.mpv.props.get('path') or '')
            if m and m.get('url'):
                self._remember(m)
        if name == 'playlist' and self.mode == 'idle':
            # nešto je pušteno mimo servera (npr. skripta yt): prepoznaj režim po sadržaju
            pl = self.mpv.props.get('playlist') or []
            if pl:
                self.mode = 'slides' if pl[0].get('filename', '').startswith(str(IMAGES)) else 'yt'
        self.mark()

    def mark(self):
        self.dirty.set()

    def _save_meta(self, keep=None):
        """Zadrži samo stavke koje su još na listi u mpv-u (i upravo dodatu), pa snimi."""
        live = {e.get('filename') for e in self.mpv.props.get('playlist') or []}
        live.add(keep)
        self.meta = {k: v for k, v in self.meta.items() if k in live}
        tmp = DATA / 'meta.tmp'
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False))
        os.replace(tmp, DATA / 'meta.json')

    def _remember(self, m):
        """Istorija puštanja: poslednjih 30 videa, najnoviji prvi, bez duplikata."""
        h = [x for x in self.st.get('history', []) if x['url'] != m['url']]
        h.insert(0, {'t': m['t'], 'url': m['url'], 'd': m.get('d') or 0, 'at': int(time.time()), 'src': m.get('src', 'link')})
        self.st['history'] = h[:30]
        save_state(self.st)

    # ---------- YouTube
    def yt_cookie_state(self):
        """'ok' = prijava važi, 'expired' = YouTube je odjavio tu sesiju (nema SID kolačića), None = nema fajla.
        yt-dlp posle svakog videa upisuje nazad šta je YouTube vratio, pa se odjava vidi ovde."""
        try:
            mt = YT_COOKIES.stat().st_mtime
        except OSError:
            return None
        c = getattr(self, '_ytck', None)
        if c and c[0] == mt:
            return c[1]
        names = {l.split('\t')[5] for l in YT_COOKIES.read_text(errors='replace').splitlines() if l.count('\t') >= 6 and not l.startswith('# ')}
        st = 'ok' if names & {'SID', '__Secure-1PSID', 'LOGIN_INFO'} else 'expired'
        self._ytck = (mt, st)
        return st

    @staticmethod
    def _extract(url):
        """yt-dlp u samom procesu servera (bez pokretanja novog programa): ~2 s umesto ~5 s."""
        import yt_dlp
        opts = {'quiet': True, 'no_warnings': True, 'format': YT_FMT, 'noplaylist': True,
                'extractor_args': {'youtube': {'skip': ['hls']}}}
        if YT_COOKIES.exists():
            opts['cookiefile'] = str(YT_COOKIES)
        opts['js_runtimes'] = {'node': {}}   # YouTube zadaci (n-challenge) se rešavaju u Node-u (>= 20, /usr/local/bin)

        def get(o):
            with yt_dlp.YoutubeDL(o) as y:
                return y.extract_info(url, download=False)
        try:
            try:
                info = get(opts)
            except yt_dlp.utils.DownloadError as e:
                if 'format is not available' not in str(e):
                    raise
                # YouTube ponekad privremeno ne da avc1 formate: uzmi bilo šta do 480p
                opts['format'] = 'bv*[height<=480]+ba/b[height<=480]/b'
                info = get(opts)
        except yt_dlp.utils.DownloadError as e:
            if 'not a bot' in str(e):   # YouTube blokira IP/nalog: kolačići istekli ili ih nema
                raise RuntimeError('YouTube traži prijavu („nisi bot“). Ubaci nove YouTube kolačiće u Sistem → YouTube kolačići.') from None
            raise RuntimeError(str(e).replace('ERROR: ', '')) from None
        if info.get('entries'):
            info = info['entries'][0]
        rf = info.get('requested_formats')
        urls = [f['url'] for f in rf] if rf else [info['url']]
        return info.get('title') or url, float(info.get('duration') or 0), urls

    async def resolve(self, url):
        """Pripremljeni linkovi važe ~6 h (parametar expire), pa se pamte: ponovno puštanje kreće odmah."""
        key = self._vid(url)
        hit = self.rcache.get(key)
        if hit and hit['exp'] - time.time() > 900:
            return hit['t'], hit['d'], hit['urls']
        t0 = time.monotonic()
        if self.ytdlp_ok:
            title, dur, urls = await asyncio.get_running_loop().run_in_executor(self.ytpool, self._extract, url)
        else:
            title, dur, urls = await self._resolve_cli(url)
        log.info('YouTube %s pripremljen za %.1f s', key, time.monotonic() - t0)
        m = re.search(r'[?&]expire=(\d+)', urls[0])
        self.rcache[key] = {'t': title, 'd': dur, 'urls': urls, 'exp': int(m.group(1)) if m else time.time() + 3600}
        for k in [k for k, v in self.rcache.items() if v['exp'] < time.time()]:
            del self.rcache[k]
        return title, dur, urls

    @staticmethod
    def _vid(url):
        """ID videa iz različitih oblika YouTube linka, da isti video ima isti ključ."""
        m = re.search(r'(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})', url)
        return m.group(1) if m else url

    async def _resolve_cli(self, url):
        proc = await asyncio.create_subprocess_exec(
            YTDLP, '-f', YT_FMT, '--no-playlist', '--print', 'title', '--print', 'duration', '-g', url,
            *(['--cookies', str(YT_COOKIES)] if YT_COOKIES.exists() else []), '--js-runtimes', 'node',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 90)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError('YouTube nije odgovorio na vreme')
        lines = [l for l in out.decode(errors='replace').splitlines() if l.strip()]
        if proc.returncode != 0 or len(lines) < 3:
            msg = err.decode(errors='replace').strip().splitlines()
            raise RuntimeError(msg[-1] if msg else 'yt-dlp nije uspeo')
        title, dur, urls = lines[0], lines[1], lines[2:]
        try:
            dur = float(dur)
        except ValueError:
            dur = 0
        return title, dur, urls

    @staticmethod
    def _q(v):  # mpv lista opcija: %duzina%vrednost, da zarezi u URL-u ne smetaju
        return '%%%d%%%s' % (len(v.encode()), v)

    async def _oembed_title(self, entry):
        """Brz naslov za stavku koja se još priprema (yt-dlp traje nekoliko sekundi)."""
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get('https://www.youtube.com/oembed', params={'url': entry['url'], 'format': 'json'}) as r:
                    if r.status == 200:
                        entry['t'] = (await r.json()).get('title')
                        self.mark()
        except Exception:
            pass

    async def warm_ytdlp(self):
        """Učita yt_dlp iz venv-a unapred, da prvi link ne čeka ~1 s na import."""
        import sys
        for sp in glob.glob(str(Path(YTDLP).parent.parent / 'lib/python3*/site-packages')):
            if sp not in sys.path:
                sys.path.append(sp)
        try:
            await asyncio.get_running_loop().run_in_executor(self.ytpool, __import__, 'yt_dlp')
            self.ytdlp_ok = True
            log.info('yt_dlp učitan u server')
        except ImportError as e:
            log.warning('yt_dlp modul nije dostupan, koristim program yt-dlp: %s', e)
            return
        # YouTube zadatak (n-challenge) Node na Pi 3 rešava ~10 s, najviše na obradu plejera (base.js).
        # yt-dlp ume da čuva već obrađen plejer na disku, ali je to isključeno (fajlovi su ~2 MB):
        # uključujemo ga i sami brišemo stare, pa video kreće za ~5 s umesto ~15 s.
        try:
            from yt_dlp.extractor.youtube.jsc._builtin import ejs
            ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
            d = Path.home() / '.cache/yt-dlp' / ejs.EJSBaseJCP._CACHE_SECTION
            old = sorted(d.glob('*'), key=lambda p: p.stat().st_mtime)[:-3] if d.exists() else []
            for p in old:
                p.unlink()
        except (ImportError, AttributeError, OSError) as e:
            log.warning('keš YouTube plejera: %s', e)
        try:   # samo ono što plejer koristi: dugački videi imaju ~180 automatskih titlova × 7 formata i
            # ~19 sinhronizovanih audio jezika, a obrada svega toga na Pi 3 traje ~18 s (2 h video: 20 s umesto 2 s).
            # Pre obrade se iz YouTube odgovora skidaju titlovi, sličice za premotavanje i sl., video iznad 480p
            # i audio koji nije originalni. Ako posle toga ne ostane i slika i zvuk, ostaje sve kao što je stiglo.
            from yt_dlp.extractor.youtube._video import YoutubeIE
            epr = YoutubeIE._extract_player_responses

            def want(f):
                mt = f.get('mimeType') or ''
                if mt.startswith('video/'):
                    return (f.get('height') or 0) <= 480
                if mt.startswith('audio/'):
                    at = f.get('audioTrack')
                    return not at or bool(at.get('audioIsDefault'))
                return False

            def lean(ie, *a, **k):
                prs, url = epr(ie, *a, **k)
                for pr in prs:
                    for key in ('captions', 'storyboards', 'endscreen', 'cards', 'annotations', 'heartbeatParams'):
                        pr.pop(key, None)
                    sd = pr.get('streamingData') or {}
                    keep = [f for f in sd.get('adaptiveFormats') or [] if want(f)]
                    kinds = {(f.get('mimeType') or '')[:6] for f in keep}
                    if {'video/', 'audio/'} <= kinds:
                        sd['adaptiveFormats'] = keep
                return prs, url
            YoutubeIE._extract_player_responses = lean
        except (ImportError, AttributeError) as e:
            log.warning('lakši YouTube odgovor: %s', e)
        try:   # isti zadaci, ali u stalno upaljenom Node-u (~3 s brže po videu)
            from yt_dlp.extractor.youtube.jsc._builtin import node as jnode
            worker, orig = JscWorker(), jnode.NodeJCP._run_js_runtime

            def fast(prov, stdin):
                try:
                    return worker.run(stdin)
                except Exception as e:
                    log.warning('jsc_worker: %s (koristim običan Node)', e)
                    return orig(prov, stdin)
            jnode.NodeJCP._run_js_runtime = fast
        except (ImportError, AttributeError) as e:
            log.warning('jsc_worker: %s', e)
        # zagrevanje: ako se plejer promenio (npr. posle noćnog ažuriranja), prvi video ne čeka obradu
        await asyncio.sleep(90)
        h = [x['url'] for x in self.st.get('history', []) if x.get('url')]
        if h and not self.pending:
            try:
                await self.resolve(h[0])
            except RuntimeError:
                pass

    async def add_youtube(self, url, now=False, start=0, remember=True, src=None):
        """src = odakle je video došao: 'cast' (YouTube aplikacija) ili 'link' (nalepljen/poslat link)."""
        if src is None:   # vraćanje liste, istorija, nasumično: isto poreklo kao ranije
            src = next((x.get('src') for x in self.st.get('history', []) if x.get('url') == url), None) or 'link'
        self.pid += 1
        entry = {'id': self.pid, 'url': url, 't': None, 'err': None}
        self.pending.append(entry)
        self.mark()
        asyncio.get_running_loop().create_task(self._oembed_title(entry))
        try:
            title, dur, urls = await self.resolve(url)
        except RuntimeError as e:
            entry['err'] = str(e)
            self.mark()
            asyncio.get_running_loop().call_later(8, lambda: (self.pending.remove(entry) if entry in self.pending else None, self.mark()))
            raise
        self.pending.remove(entry)
        self.mark()
        if self.mode == 'tv':
            if not now:   # TV je na ekranu: video čeka na YouTube listi dok se ne vratiš na AV1
                self.yt_snap = self.yt_snap or {'urls': [], 'pos': 0}
                self.yt_snap['urls'].append(url)
                return title
            await self.set_source('av1')   # vraća sačuvanu listu, pa ovaj video ide odmah
        opts = 'force-media-title=' + self._q(title)
        if len(urls) > 1:
            opts += ',audio-file=' + self._q(urls[1])
        if start and start > 2:
            opts += ',start=%d' % int(start)   # nastavi od mesta gde je gledano (pregledač/cast)
        self.meta[urls[0]] = {'t': title, 'd': dur, 'url': url, 'src': src}
        self._save_meta(urls[0])
        if remember:
            self._remember(self.meta[urls[0]])   # u istoriji odmah, da se uvek može ponovo pustiti
        if self.mode != 'yt':
            await self.stop_slides()
            await self.hide_clock()
            await self.mpv.cmd('loadfile', urls[0], 'replace', opts)
            self.mode = 'yt'
            # posle Stop/pauze mpv zadržava pauzu, pa novi video ne bi krenuo
            await self.mpv.cmd('set_property', 'pause', self.source != 'av1')
        else:
            await self.mpv.cmd('loadfile', urls[0], 'append-play', opts)
            if now:  # premesti odmah iza trenutnog i pusti ga
                n = int(await self.mpv.cmd('get_property', 'playlist-count') or 1)
                cur = int(await self.mpv.cmd('get_property', 'playlist-pos') or 0)
                if n - 1 != cur + 1:
                    await self.mpv.cmd('playlist-move', n - 1, cur + 1)
                await self.mpv.cmd('set_property', 'playlist-pos', cur + 1)
                if self.source == 'av1':
                    await self.mpv.cmd('set_property', 'pause', False)
        self.mark()
        return title

    def queue(self):
        if self.mode != 'yt':
            return []
        out = []
        pl = self.mpv.props.get('playlist') or []
        cur = next((i for i, e in enumerate(pl) if e.get('current')), 0)
        for i, e in enumerate(pl):
            if i < cur:
                continue  # već pušteni su u istoriji
            m = self.meta.get(e.get('filename'), {})
            title = m.get('t') or (self.mpv.props.get('media-title') if e.get('current') else 'YouTube video')
            out.append({'i': i, 't': title, 'd': m.get('d') or 0, 'cur': bool(e.get('current')), 'src': m.get('src', 'link')})
        return out

    # ---------- slajdšou
    def images(self):
        return sorted(os.path.basename(p) for p in glob.glob(str(IMAGES / '*.jpg')))

    async def start_slides(self):
        imgs = self.images()
        if not imgs or self.source != 'av1' or self.st.get('idle') == 'clock':
            self.mode = 'idle'
            await self.mpv.cmd('playlist-clear')
            self.mark()
            return
        await self.mpv.cmd('set_property', 'image-display-duration', self.st['slide_sec'])
        await self.mpv.cmd('set_property', 'loop-playlist', 'inf')
        await self.mpv.cmd('loadfile', str(IMAGES / imgs[0]), 'replace')
        for name in imgs[1:]:
            await self.mpv.cmd('loadfile', str(IMAGES / name), 'append')
        await self.mpv.cmd('set_property', 'pause', False)
        self.mode = 'slides'
        self.mark()

    async def stop_slides(self):
        if self.mode == 'slides':
            await self.mpv.cmd('set_property', 'loop-playlist', 'no')
            await self.mpv.cmd('playlist-clear')
            await self.mpv.cmd('stop')
        self.mode = 'idle'

    async def refresh_slides(self):
        if self.mode in ('slides', 'idle') and self.source == 'av1':
            await self.stop_slides()
            await self.start_slides()

    # ---------- slika konzole na ekranu (mpv overlay, YouTube ostaje pauziran ispod)
    async def _overlay(self, parts, build, oid=0, keep=False, show=True):
        """Iscrta SVG preko celog ekrana kao mpv overlay (slika se kešira u /tmp).
        keep=True: slika koja se ne menja (konzole, logo) čuva se trajno u data/ovl, da je sat,
        radio i TV ne izbace iz keša u /tmp — prelaz na AV je tada uvek odmah.

        build(vw, h) crta u „virtuelnoj“ širini koja odgovara obliku ekrana (16:9), a
        rsvg to stisne u stvarnu rezoluciju izlaza; ekran ga vrati u pravi oblik."""
        w = int(await self.mpv.cmd('get_property', 'osd-width') or 720)
        h = int(await self.mpv.cmd('get_property', 'osd-height') or 576)
        vw = round(h * SCREEN_ASPECT)
        key = hashlib.md5(json.dumps(parts + [w, h, 7], ensure_ascii=False).encode()).hexdigest()[:12]
        if keep:
            (DATA / 'ovl').mkdir(exist_ok=True)
        raw = (DATA / 'ovl' / ('%s.bgra' % key)) if keep else Path('/tmp/teco-ovl-%s.bgra' % key)
        if not raw.exists():
            svg_path = raw.with_suffix('.svg')
            svg_path.write_text(build(vw, h))
            png = raw.with_suffix('.png')
            # crtamo u punoj (16:9) širini; sužavanje radi PIL, jer rsvg pri nejednakom
            # skaliranju pogrešno centrira tekst (~45 px ulevo)
            p = await asyncio.create_subprocess_exec('rsvg-convert', '-w', str(vw), '-h', str(h), '-o', str(png), str(svg_path))
            await p.wait()

            def to_bgra():
                from PIL import Image
                im = Image.open(png).convert('RGBA').resize((w, h), Image.LANCZOS)
                raw.write_bytes(im.tobytes('raw', 'BGRA'))
            try:
                await asyncio.get_running_loop().run_in_executor(None, to_bgra)
            except Exception as e:
                log.warning('slika za ekran nije napravljena: %s', e)
                return None
            finally:
                for tmp in (png, svg_path):   # međukoraci više ne trebaju
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        if not show:   # samo pripremi sliku unapred (npr. konzole pri pokretanju), bez prikaza
            return raw
        await self.mpv.cmd('overlay-add', oid, 0, 0, str(raw), 0, 'bgra', w, h, w * 4)
        if not keep:
            self._overlay_prune(raw)
        return raw

    @staticmethod
    def _ovl_prune_keep(n=12):
        """data/ovl: stare slike konzola (posle promene imena) se brišu, ostaje poslednjih n."""
        try:
            files = sorted((DATA / 'ovl').glob('*.bgra'), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[n:]:
                p.unlink()
        except OSError:
            pass

    @staticmethod
    def _overlay_prune(keep, n=12):
        """/tmp je u RAM-u (64 MB), a jedna slika ima ~1,6 MB: čuvaj samo poslednjih n."""
        try:
            os.utime(keep)
            files = sorted(Path('/tmp').glob('teco-ovl-*.bgra'), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[n:]:
                p.unlink()
        except OSError:
            pass

    # ---------- sat kad nema videa (overlay 1, iznad slajdova, ispod ničega)
    DAYS = ['ponedeljak', 'utorak', 'sreda', 'četvrtak', 'petak', 'subota', 'nedelja']
    MONTHS = ['januar', 'februar', 'mart', 'april', 'maj', 'jun', 'jul', 'avgust', 'septembar', 'oktobar', 'novembar', 'decembar']

    async def show_clock(self):
        now = time.localtime()
        tin, tout, hum = self.sys.get('room'), self.sys.get('out'), self.sys.get('hum')
        fmt_t = lambda t: '%.1f°' % t if t is not None else '—'
        hm = time.strftime('%H:%M', now)
        date = '%s, %d. %s' % (self.DAYS[now.tm_wday], now.tm_mday, self.MONTHS[now.tm_mon - 1])

        def build(w, h):
            def block(x, label, val, color):
                return (f'<text x="{x:.0f}" y="{h * 0.74:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.032:.0f}" letter-spacing="3" fill="{color}">{label}</text>'
                        f'<text x="{x:.0f}" y="{h * 0.86:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.1:.0f}" fill="#e8eef6">{val}</text>')
            # samo merenja koja postoje: bez spoljnog merenja nema ni bloka „Napolje”
            both = tin is not None and tout is not None
            xin = w * (0.3 if both else 0.5)
            parts = []
            if both:
                parts.append(f'<line x1="{w * 0.5:.0f}" y1="{h * 0.68:.0f}" x2="{w * 0.5:.0f}" y2="{h * 0.88:.0f}" stroke="#233044" stroke-width="2"/>')
            if tin is not None:
                parts.append(block(xin, 'UNUTRA', fmt_t(tin), '#2dd4bf'))
                if hum is not None:
                    parts.append(f'<text x="{xin:.0f}" y="{h * 0.93:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.036:.0f}" fill="#9aa4b3">vlažnost {hum}%</text>')
            if tout is not None:
                parts.append(block(w * (0.7 if both else 0.5), 'NAPOLJE', fmt_t(tout), '#fbbf24'))
            temps = ''.join(parts)
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="g" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="#2dd4bf" stop-opacity=".22"/><stop offset="1" stop-color="#2dd4bf" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#g)"/>
<text x="{w / 2}" y="{h * 0.46:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.3:.0f}" fill="#ffffff">{hm}</text>
<text x="{w / 2}" y="{h * 0.56:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.045:.0f}" fill="#9aa4b3">{date}</text>
{temps}
</svg>'''
        old = self.clock_raw
        self.clock_raw = await self._overlay(['clock', hm, date, fmt_t(tin), fmt_t(tout), hum], build, oid=1)
        self.clock_key = (hm, fmt_t(tin), fmt_t(tout), hum)
        if old and old != self.clock_raw:  # stari minut više ne treba (mpv ga je već pročitao)
            for ext in ('.bgra', '.png', '.svg'):
                try:
                    old.with_suffix(ext).unlink()
                except OSError:
                    pass

    async def hide_clock(self):
        if self.clock_key:
            await self.mpv.cmd('overlay-remove', 1)
        self.clock_key = None

    def clock_wanted(self):
        if not self.mpv.connected:
            return False
        if self.source in ('st', 'fm'):   # radio: sat na ekranu na svakih 15 min (:00, :15, ...), N sekundi
            n = int(self.st.get('radio_clock', 15) or 0)
            lt = time.localtime()
            return n > 0 and lt.tm_min % 15 == 0 and lt.tm_sec < n
        # dok se stanje posle uključivanja ne vrati, na ekranu je logo (ne sat)
        return self.booted and self.st.get('idle') == 'clock' and self.source == 'av1' and self.mode == 'idle'

    async def clock_loop(self):
        while True:
            try:
                if self.clock_wanted():
                    t = self.sys.get('room'), self.sys.get('out')
                    key = (time.strftime('%H:%M'), *('%.1f°' % x if x is not None else '—' for x in t), self.sys.get('hum'))
                    if key != self.clock_key:
                        await self.show_clock()
                elif self.clock_key:
                    await self.hide_clock()
                # logo: YouTube bez videa, slika i sata (i pri pokretanju), umesto prazne pozadine
                want = self.mpv.connected and self.source == 'av1' and self.mode == 'idle' and not self.clock_wanted() \
                    and not getattr(self, 'yt_loading', None)   # dok se video učitava, tu je YouTube slika
                if want and not self.logo_on:
                    await self._overlay(['logo', 1], splash.svg, keep=True)
                    self.logo_on = True
                    if self.source in AVS:   # izvor je promenjen dok se logo crtao: konzola ide preko njega
                        await self.show_console(self.source)
                elif not want and self.logo_on:
                    if self.source == 'av1':
                        await self.mpv.cmd('overlay-remove', 0)
                    self.logo_on = False
            except Exception as e:
                log.warning('sat: %s', e)
            await asyncio.sleep(2)

    @staticmethod
    def _esc(t):
        return str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    async def show_fm(self):
        f = self.st['fm']['f']
        p = next((x for x in self.st['fm']['list'] if abs(x['f'] - f) < 0.05), None)
        tn = self.tuner if self.fm_task else None
        name = p['n'] if p else (tn.ps if tn and tn.ps else 'Ručno podešeno')
        status = self.fm_err or (tn.rt if tn and tn.rt else (('Stereo' if tn.stereo else 'Mono') if tn and tn.tuned else 'Prijem…'))
        err = bool(self.fm_err)

        def build(w, h):
            x0, x1, y = w * 0.12, w * 0.88, h * 0.8
            pos = lambda fr: x0 + (fr - FMIN) / (FMAX - FMIN) * (x1 - x0)
            ticks = []
            for i in range(0, 41):
                fr = 88 + i * 0.5
                if fr > FMAX:
                    break
                big = fr % 2 == 0
                ticks.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%s"/>'
                             % (pos(fr), y, pos(fr), y - (h * 0.035 if big else h * 0.018), '#c7ced8' if big else '#5f6b7b', 2 if big else 1))
                if fr % 4 == 0:
                    ticks.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-family="DejaVu Sans Mono" font-size="%d" fill="#8b9bb1">%d</text>'
                                 % (pos(fr), y - h * 0.05, h * 0.03, fr))
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="g" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="#f87171" stop-opacity=".30"/><stop offset="1" stop-color="#f87171" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#g)"/>
<text x="{w / 2}" y="{h * 0.17:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.038:.0f}" letter-spacing="4" fill="#f87171">FM RADIO</text>
<text x="{w / 2}" y="{h * 0.43:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.22:.0f}" fill="#ffffff">{f:.1f}<tspan font-size="{h * 0.06:.0f}" fill="#8b9bb1" dx="{w * 0.015:.0f}">MHz</tspan></text>
<text x="{w / 2}" y="{h * 0.55:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.065:.0f}" fill="#e8eef6">{self._esc(name)}</text>
<text x="{w / 2}" y="{h * 0.63:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.034:.0f}" fill="{'#f87171' if err else '#9aa4b3'}">{self._esc(status[:60])}</text>
<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="#8b9bb1" stroke-width="2"/>
{''.join(ticks)}
<line x1="{pos(f):.1f}" y1="{y - h * 0.09:.1f}" x2="{pos(f):.1f}" y2="{y + h * 0.02:.1f}" stroke="#ef4444" stroke-width="3"/>
</svg>'''
        await self._overlay(['fm', f, name, status], build)

    async def prerender_consoles(self):
        """Slike sve tri konzole unapred (pri pokretanju i posle promene konzole/imena):
        prvi prelaz sa YouTube-a na AV je tada odmah, a ne posle ~1-2 s crtanja."""
        for _ in range(60):
            if self.mpv.connected:
                break
            await asyncio.sleep(0.5)
        for s in AVS:
            try:
                await self.show_console(s, show=False)
            except Exception as e:
                log.warning('slika konzole %s: %s', s, e)

    async def show_console(self, s, show=True):
        try:
            cons = json.loads((STATIC / 'consoles.json').read_text())
        except (OSError, ValueError):
            return
        c = cons.get(self.st['cons'].get(s)) or cons['other']
        name = self.st['names'].get(s) or c['n']
        color = {'av2': '#fbbf24', 'av3': '#a78bfa', 'av4': '#34d399'}[s]

        def build(w, h):
            esc = self._esc
            ch = int(h * 0.46)                       # veličina prema visini, da tekst uvek stane ispod
            cw = int(ch * 260 / 150)
            if cw > w * 0.8:
                cw = int(w * 0.8)
                ch = int(cw * 150 / 260)
            cx, cy = (w - cw) // 2, int(h * 0.1)
            inner = c['svg'].replace('<svg viewBox="0 0 260 150"', '<svg x="%d" y="%d" width="%d" height="%d" viewBox="0 0 260 150"' % (cx, cy, cw, ch), 1)
            ty = cy + ch + int(h * 0.1)
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="bgGlow" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="{color}" stop-opacity=".35"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#bgGlow)"/>
<ellipse cx="{w // 2}" cy="{cy + ch * 0.78:.0f}" rx="{cw * 0.4:.0f}" ry="{h * 0.02:.0f}" fill="#000" fill-opacity=".5"/>
{inner}
<text x="{w // 2}" y="{ty}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.038:.0f}" letter-spacing="3" fill="{color}">{AVL.get(s, s.upper())}</text>
<text x="{w // 2}" y="{ty + h * 0.095:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.075:.0f}" fill="#ffffff">{esc(name)}</text>
<text x="{w // 2}" y="{ty + h * 0.165:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.036:.0f}" fill="#9aa4b3">Čekam signal…</text>
</svg>'''
        await self._overlay(['cons', s, c['svg'], name], build, keep=True, show=show)

    async def hide_console(self):
        self.logo_on = False
        await self.mpv.cmd('overlay-remove', 0)

    async def show_yt(self, title=None):
        """Prelazak na YT dok se video učitava: slika u stilu ostalih izvora, umesto crnog ekrana.
        Sklanja se kad krene prva slika videa (_mpv_changed) ili posle 30 s."""
        color = '#ff0033'
        line = 'Učitavam video…'
        title = (title or 'YouTube').strip()

        def build(w, h):
            esc = self._esc
            cx, cy, s = w / 2, h * 0.3, h * 0.26
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="g" cx="50%" cy="20%" r="80%"><stop offset="0" stop-color="{color}" stop-opacity=".28"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#g)"/>
<rect x="{cx - s * 0.72:.0f}" y="{cy - s * 0.5:.0f}" width="{s * 1.44:.0f}" height="{s:.0f}" rx="{s * 0.26:.0f}" fill="{color}"/>
<path d="M{cx - s * 0.16:.0f} {cy - s * 0.24:.0f}v{s * 0.48:.0f}l{s * 0.4:.0f} -{s * 0.24:.0f}z" fill="#fff"/>
<text x="{cx}" y="{h * 0.6:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.036:.0f}" letter-spacing="4" fill="{color}">YOUTUBE</text>
<text x="{cx}" y="{h * 0.72:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.07:.0f}" fill="#ffffff">{esc(title[:32] + ('…' if len(title) > 32 else ''))}</text>
<text x="{cx}" y="{h * 0.82:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.04:.0f}" fill="#c7ced8">{line}</text>
</svg>'''
        self.yt_loading = time.monotonic()
        await self._overlay(['yt', title], build)

        async def failsafe(started):
            await asyncio.sleep(30)
            if getattr(self, 'yt_loading', None) == started:
                await self.yt_loaded()
        asyncio.get_running_loop().create_task(failsafe(self.yt_loading))

    async def yt_loaded(self):
        if getattr(self, 'yt_loading', None):
            self.yt_loading = None
            if self.source == 'av1':
                await self.hide_console()

    # ---------- izvori
    async def set_source(self, s, title=None):
        """Prebacivanja idu jedno po jedno: npr. cast stigne dok se FM još pali, pa bi FM
        završio posle YouTube-a i preuzeo ekran i zvuk.
        title: prelazak na YT zbog novog videa (cast, link) — do prve slike prikazuje se slika „Učitavam“."""
        if s not in SOURCES:
            return
        lock = self.__dict__.setdefault('_src_lock', asyncio.Lock())
        me = asyncio.current_task()
        if getattr(self, '_src_owner', None) is me:   # poziv iznutra (isti zadatak): bez čekanja, da se ne zaglavi
            return await self._set_source(s, title)
        async with lock:
            self._src_owner = me
            try:
                await self._set_source(s, title)
            finally:
                self._src_owner = None

    def _hist_title(self, url):
        return next((x.get('t') for x in self.st.get('history', []) if x.get('url') == url), None)

    async def _set_source(self, s, title=None):
        prev, self.source = self.source, s
        log.info('izvor: %s -> %s', prev, s)
        if s == 'av1' and prev != 'av1':
            snap = self.yt_snap if self.mode != 'yt' else None
            if title or (snap and snap.get('urls')):   # video se tek učitava: YouTube slika umesto prethodnog izvora
                await self.show_yt(title or self._hist_title(snap['urls'][0]))
        if prev == 'fm' and s != 'fm':
            await self.fm_stop()
        if prev == 'st' and s != 'st':
            await self.stream_stop()
        if prev == 'tv' and s != 'tv':
            await self.tv_stop()
        self.st['last_source'] = s
        if s in AVS:
            self.st['last_av'] = s   # AV dugme na stranici pamti poslednji ulaz
        for k in AVS:   # AV releji: uključen samo ulaz koji je izabran
            self.relay_role(k, s == k)
        save_state(self.st)
        self.mark()   # stranica odmah vidi novi izvor (slika za ekran se ume crtati i par sekundi)
        if s == 'av1':
            if self.yt_snap and self.mode != 'yt':   # povratak sa TV-a: vrati YouTube listu gde je stala
                snap, self.yt_snap = self.yt_snap, None
                await self.yt_restore(snap)
            elif self.mode == 'yt':
                await self.mpv.cmd('set_property', 'pause', False)
            elif self.mode == 'idle':
                await self.start_slides()
            else:
                await self.mpv.cmd('set_property', 'pause', False)
        elif s == 'tv':
            await self.tv_start()
        else:
            await self.mpv.cmd('set_property', 'pause', True)
            if s == 'fm':
                await self.fm_start()
            elif s == 'st':
                await self.stream_start()
        if s != 'av1':
            await self.hide_clock()  # sat je overlay 1 i bio bi iznad slike konzole/radija
        if s in AVS:
            await self.show_console(s)
        elif s == 'av1' and not getattr(self, 'yt_loading', None):
            await self.hide_console()
        if s != 'av1':
            self.yt_loading = None
        self.mark()

    # ---------- YouTube lista: pamćenje i vraćanje (TV, ponovno pokretanje)
    def yt_snapshot(self):
        """Linkovi od trenutnog videa do kraja liste i mesto gde je video stao."""
        if self.mode != 'yt':
            return None
        pl = self.mpv.props.get('playlist') or []
        cur = next((i for i, e in enumerate(pl) if e.get('current')), 0)
        urls = [m['url'] for m in (self.meta.get(e.get('filename')) or {} for e in pl[cur:]) if m.get('url')]
        if not urls:
            return None
        return {'urls': urls[:6], 'pos': int(self.mpv.props.get('time-pos') or 0), 'paused': bool(self.mpv.props.get('pause'))}

    async def yt_restore(self, snap):
        urls = list(dict.fromkeys(snap.get('urls') or []))   # bez duplikata
        if not urls:
            return
        try:
            await self.add_youtube(urls[0], start=snap.get('pos', 0), remember=False)
        except RuntimeError as e:
            log.warning('vraćanje YouTube liste: %s', e)

        async def rest():   # ostatak liste u pozadini, da prvi video krene odmah
            for u in urls[1:]:
                try:
                    await self.add_youtube(u, remember=False)
                except RuntimeError:
                    pass
        asyncio.get_running_loop().create_task(rest())

    async def boot_restore(self):
        try:
            await self._boot_restore()
        except Exception as e:
            log.warning('vraćanje stanja posle uključivanja: %s', e)
        self.booted = True

    async def _boot_restore(self):
        """Posle uključivanja: izvor, jačina i YouTube lista kao pre gašenja (ili izvor iz podešavanja)."""
        for _ in range(60):
            if self.mpv.connected:
                break
            await asyncio.sleep(0.5)
        for _ in range(20):
            if self.ytdlp_ok:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        b = self.st.get('boot', 'last')
        target = self.st.get('last_source', 'av1') if b == 'last' else b
        if target not in SOURCES:
            target = 'av1'
        if self.st.get('last_vol') is not None:
            await self.set_volume(self.st['last_vol'])
        snap = self.st.get('last_yt') if b == 'last' else None
        if snap and (self.mpv.props.get('playlist') or []):
            snap = None   # plejer nije restartovan (samo server): lista je već tu, ne dodaj je ponovo
        if snap and snap.get('urls'):
            if target == 'av1':
                await self.yt_restore(snap)
                if snap.get('paused'):
                    await self.mpv.cmd('set_property', 'pause', True)
            else:
                self.yt_snap = snap
        if target != 'av1':
            await self.set_source(target)
        self.booted = True
        self.mark()

    def remember_state(self):
        """Poziva se na 10 s: pamti YouTube listu i jačinu za sledeće uključivanje."""
        if not self.booted:   # dok se stanje posle uključivanja ne vrati, ne prepisuj ga praznim
            return
        snap = self.yt_snapshot() if self.mode == 'yt' else self.yt_snap
        vol = self.cur_vol()
        changed = False
        if snap != self.st.get('last_yt'):
            self.st['last_yt'] = snap
            changed = True
        if vol is not None and int(vol) != self.st.get('last_vol'):
            self.st['last_vol'] = int(vol)
            changed = True
        if changed:
            save_state(self.st)

    # ---------- TV: kanali uživo (glavni mpv; YouTube lista se pamti i vraća)
    def tv_sys(self):
        """Lista kanala iz data/tv_channels.json (proverena sa Teco.Pi); ako je nema, ugrađenih 10."""
        try:
            mt = TV_FILE.stat().st_mtime
            if mt != getattr(self, '_tv_mt', None):
                self._tv_sys, self._tv_mt = json.loads(TV_FILE.read_text()), mt
            return self._tv_sys
        except (OSError, ValueError):
            return SYS_TV

    def tv_all(self):
        return self.tv_sys() + [dict(x, c='my') for x in self.st['tv']['list']]

    def tv_seq(self):
        """Redosled za sledeći/prethodni kanal: omiljeni ako ih ima, inače svi."""
        fav = set(self.st['tv'].get('fav') or [])
        lst = self.tv_all()
        return [x for x in lst if x['u'] in fav] or lst

    async def tv_step(self, d):
        seq = self.tv_seq()
        if not seq:
            return
        u = self.st['tv'].get('u')
        i = next((k for k, x in enumerate(seq) if x['u'] == u), -1)
        await self.tv_play_url(seq[(i + d) % len(seq)]['u'])

    async def tv_play_url(self, u):
        if not any(x['u'] == u for x in self.tv_all()):
            return
        self.st['tv']['u'] = u
        save_state(self.st)
        if self.source != 'tv':
            await self.set_source('tv')
        else:
            await self.tv_start()
        self.mark()

    def tv_cur(self):
        lst = self.tv_all()
        if not lst:
            return None
        u = self.st['tv'].get('u')
        i = next((k for k, x in enumerate(lst) if x['u'] == u), 0)
        return i, lst[i]

    async def tv_start(self):
        if self.mode == 'yt':
            self.yt_snap = self.yt_snapshot() or self.yt_snap
        await self.stop_slides()
        await self.hide_clock()
        self.tv_err = ''
        cur = self.tv_cur()
        if not cur:
            self.tv_err = 'Nema kanala.'
            await self.show_tv()
            return
        self.mode = 'tv'
        self.tv_loading = True
        asyncio.get_running_loop().create_task(self.show_tv())   # crtanje slike ne sme da odloži kanal
        await self.mpv.cmd('set_property', 'loop-playlist', 'no')
        await self.mpv.cmd('loadfile', cur[1]['u'], 'replace', TV_OPTS + ',force-media-title=' + self._q(cur[1]['n']))
        await self.mpv.cmd('set_property', 'pause', False)
        self.mark()

    async def tv_stop(self):
        if self.mode == 'tv':
            self.mode = 'idle'
            self.tv_loading = False
            await self.mpv.cmd('stop')
        await self.hide_console()

    async def show_tv(self):
        cur = self.tv_cur()
        name = cur[1]['n'] if cur else 'TV'
        seq = self.tv_seq()
        k = next((j for j, x in enumerate(seq) if cur and x['u'] == cur[1]['u']), None)
        pos = '%d / %d' % (k + 1, len(seq)) if k is not None else ''
        line = self.tv_err or 'Učitavam kanal…'
        err = bool(self.tv_err)
        color = '#a78bfa'

        def build(w, h):
            esc = self._esc
            cx, cy, s = w / 2, h * 0.3, h * 0.3
            x0, y0 = cx - s * 0.7, cy - s * 0.42
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="g" cx="50%" cy="20%" r="80%"><stop offset="0" stop-color="{color}" stop-opacity=".3"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#g)"/>
<path d="M{cx - s * 0.22:.0f} {y0 - s * 0.28:.0f}L{cx:.0f} {y0 - s * 0.04:.0f}L{cx + s * 0.22:.0f} {y0 - s * 0.28:.0f}" fill="none" stroke="{color}" stroke-width="{h * 0.01:.0f}" stroke-linecap="round" stroke-linejoin="round"/>
<rect x="{x0:.0f}" y="{y0:.0f}" width="{s * 1.4:.0f}" height="{s * 0.9:.0f}" rx="{s * 0.08:.0f}" fill="{color}" fill-opacity=".14" stroke="{color}" stroke-width="{h * 0.01:.0f}"/>
<path d="M{cx - s * 0.12:.0f} {cy - s * 0.18:.0f}v{s * 0.36:.0f}l{s * 0.3:.0f} -{s * 0.18:.0f}z" fill="{color}"/>
<text x="{cx}" y="{h * 0.6:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.036:.0f}" letter-spacing="4" fill="{color}">TV UŽIVO{('  ·  ' + pos) if pos else ''}</text>
<text x="{cx}" y="{h * 0.72:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.085:.0f}" fill="#ffffff">{esc(name[:26])}</text>
<text x="{cx}" y="{h * 0.82:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.04:.0f}" fill="{'#f87171' if err else '#c7ced8'}">{esc(line[:52])}</text>
</svg>'''
        await self._overlay(['tv', name, pos, line], build)
        if self.mode == 'tv' and not self.tv_loading and not self.tv_err:
            await self.hide_console()   # kanal je krenuo dok se slika crtala

    # ---------- I2C hardver (hw.py): FM tjuner RDA5807M, pojačalo TPA2016D2
    async def _i2c(self, fn, *args):
        """I2C je spor i blokira: izvršava se van glavne petlje, jedan po jedan."""
        async with self.i2c_lock:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def hw_detect(self):
        if self.i2c is None:
            self.i2c = hw.open_bus()
        if self.i2c is None:
            return
        if not self.tuner:
            self.tuner = await self._i2c(hw.RDA5807.detect, self.i2c)
            if self.tuner:
                log.info('FM tjuner RDA5807M pronađen')
        if not getattr(self, 'keys_addr', None):   # pločica sa tasterima (PCF8574, 0x20-0x27 ili 0x38-0x3F)
            def find_keys(bus):
                for a in list(range(0x20, 0x28)) + list(range(0x38, 0x40)):
                    try:
                        bus.read_byte(a)
                        return a
                    except OSError:
                        pass
            self.keys_addr = await self._i2c(find_keys, self.i2c)
        if not self.amp:
            self.amp = await self._i2c(hw.TPA2016.detect, self.i2c)
            if self.amp:
                log.info('pojačalo TPA2016D2 pronađeno')
                try:
                    await self._i2c(self.amp.setup)
                except OSError as e:
                    log.warning('pojačalo: %s', e)
                self._amp_on = None   # amp_sync podesi jačinu
                await self.amp_sync()

    def relay_role(self, role, on):
        """Rele po nameni (npr. 'tuner' = GPIO27 ili ime koje sadrži 'tun')."""
        for i, r in enumerate(self.st['relays']):
            if r.get('role') == role:
                if r['on'] != on:
                    self.relay_set(i, on)
                return

    # ---------- jačina: preko pojačala (zvučnici) ili u plejeru (Bluetooth, bez pojačala)
    def amp_active(self):
        if not self.amp:
            return False
        d = next((s for s in self.audio if s['def']), None)
        return bool(d and not d['bt'])

    def cur_vol(self):
        return int(self.st.get('amp_vol', 60)) if self.amp_active() else int(self.mpv.props.get('volume') or 70)

    def cur_mute(self):
        return self.amp.muted if self.amp_active() else bool(self.mpv.props.get('mute'))

    def play_vol(self):
        """Jačina za plejere: pun signal kad jačinu drži pojačalo (manje šuma sa jack-a)."""
        return 100 if self.amp_active() else int(self.mpv.props.get('volume') or 70)

    async def amp_sync(self):
        """Poziva se kad se promeni izlaz zvuka: prebaci jačinu između plejera i pojačala."""
        on = self.amp_active()
        if on == getattr(self, '_amp_on', None):
            return
        was, self._amp_on = getattr(self, '_amp_on', None), on
        if on:
            if was is False:
                self.st['amp_vol'] = int(self.mpv.props.get('volume') or 60)
            await self.mpv.cmd('set_property', 'volume', 100)
            for sock in HELPER_SOCKS:
                await mpv_oneshot(sock, 'set_property', 'volume', 100)
            try:
                await self._i2c(self.amp.set_volume, self.st.get('amp_vol', 60))
            except OSError as e:
                log.warning('pojačalo: %s', e)
        elif was and self.amp:
            v = int(self.st.get('amp_vol', 60))
            await self.mpv.cmd('set_property', 'volume', v)
            for sock in HELPER_SOCKS:
                await mpv_oneshot(sock, 'set_property', 'volume', v)
        self.mark()

    # ---------- FM radio: RDA5807M ako je povezan, inače RTL-SDR (rtl_fm -> pomoćni mpv)
    async def _tuner_loop(self):
        """Signal i RDS sa tjunera ~12 puta u sekundi dok je FM izabran."""
        while self.source == 'fm' and self.tuner:
            try:
                if await self._i2c(self.tuner.poll):
                    await self.show_fm()
                    self.mark()
            except OSError:
                pass
            await asyncio.sleep(0.08)

    async def fm_start(self):
        await self.fm_stop()
        f = self.st['fm']['f']
        if self.tuner:
            self.fm_err = ''
            try:
                await self._i2c(self.tuner.power_on)
                await self._i2c(self.tuner.tune, f)
                self.relay_role('tuner', True)   # zvuk tjunera ide u PCM1808
                self.fm_task = asyncio.get_running_loop().create_task(self._tuner_loop())
            except OSError as e:
                self.fm_err = 'FM tjuner ne odgovara (%s)' % e
            await self.show_fm()
            return
        vol = self.play_vol()
        cmd = (f'exec rtl_fm -f {f:.1f}M -M wbfm -s 200000 -r 48000 -E deemp -g 40 - 2>/tmp/teco-rtlfm.log'
               f' | mpv --no-config --ao=pipewire --really-quiet --input-ipc-server={FM_SOCK} --volume={vol}'
               ' --demuxer=rawaudio --demuxer-rawaudio-rate=48000 --demuxer-rawaudio-channels=1'
               ' --demuxer-rawaudio-format=s16le -')
        self.fm_err = ''
        self.fm_proc = await asyncio.create_subprocess_shell(cmd, start_new_session=True)
        asyncio.get_running_loop().create_task(self._fm_watch(self.fm_proc))
        await self.show_fm()

    async def _fm_watch(self, proc):
        await proc.wait()
        if proc is self.fm_proc and self.source == 'fm':
            try:
                tail = Path('/tmp/teco-rtlfm.log').read_text(errors='replace').strip().splitlines()
            except OSError:
                tail = []
            self.fm_err = 'SDR prijemnik ne radi' + (': ' + tail[-1] if tail else '')
            self.fm_proc = None
            await self.show_fm()
            self.mark()

    async def fm_stop(self):
        t, self.fm_task = self.fm_task, None
        if t:
            t.cancel()
            if self.tuner:
                try:
                    await self._i2c(self.tuner.power_off)
                except OSError:
                    pass
                self.relay_role('tuner', False)
        p, self.fm_proc = self.fm_proc, None
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                os.killpg(p.pid, signal.SIGKILL)

    async def fm_tune(self, f):
        f = round(min(FMAX, max(FMIN, float(f))), 1)
        self.st['fm']['f'] = f
        save_state(self.st)
        if self.source == 'fm':
            if self.tuner and self.fm_task:   # tjuner menja stanicu odmah, bez ponovnog pokretanja
                try:
                    await self._i2c(self.tuner.tune, f)
                except OSError as e:
                    self.fm_err = 'FM tjuner ne odgovara (%s)' % e
                await self.show_fm()
            else:
                await self.fm_start()
        self.mark()

    async def fm_seek(self, d):
        fs = sorted(p['f'] for p in self.st['fm']['list'])
        if not fs:
            return
        cur = self.st['fm']['f']
        if d > 0:
            nx = next((x for x in fs if x > cur + 0.05), fs[0])
        else:
            nx = next((x for x in reversed(fs) if x < cur - 0.05), fs[-1])
        await self.fm_tune(nx)

    # ---------- Stream: internet radio (pomoćni mpv, YouTube lista ostaje netaknuta)
    def stream_all(self):
        """Sistemske stanice pa moje (moje su uvek omiljene)."""
        return SYS_STREAMS + [dict(x, c='my') for x in self.st['stream']['list']]

    def stream_seq(self):
        """Redosled za sledeća/prethodna: omiljene i moje stanice, a ako ih nema, sve."""
        fav = set(self.st['stream'].get('fav') or [])
        lst = self.stream_all()
        return [x for x in lst if x['u'] in fav or x.get('c') == 'my'] or lst

    async def stream_step(self, d):
        seq = self.stream_seq()
        if not seq:
            return
        u = self.st['stream'].get('u')
        i = next((k for k, x in enumerate(seq) if x['u'] == u), -1)
        u = seq[(i + d) % len(seq)]['u']
        await self.stream_play(next(k for k, x in enumerate(self.stream_all()) if x['u'] == u))

    def stream_cur(self):
        lst = self.stream_all()
        if not lst:
            return None
        u = self.st['stream'].get('u')
        i = next((k for k, x in enumerate(lst) if x['u'] == u), 0)
        return i, lst[i]

    async def stream_start(self):
        await self.stream_stop()
        self.st_err, self.st_title = '', ''
        cur = self.stream_cur()
        if not cur:
            self.st_err = 'Nema sačuvanih stanica. Dodaj link u Admin režimu.'
            await self.show_stream()
            return
        vol = self.play_vol()
        self.st_proc = await asyncio.create_subprocess_exec(
            'mpv', '--no-config', '--no-video', '--ao=pipewire', '--really-quiet', '--input-ipc-server=' + ST_SOCK,
            '--volume=%d' % vol, '--mute=%s' % ('yes' if self.mpv.props.get('mute') else 'no'),
            '--network-timeout=15', '--demuxer-max-bytes=4MiB', '--cache-secs=10', cur[1]['u'],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            env=dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid()))
        asyncio.get_running_loop().create_task(self._stream_watch(self.st_proc))
        await self.show_stream()

    async def _stream_watch(self, proc):
        """Prati naslov pesme koji šalje stanica i javlja ako se stream prekine."""
        while proc.returncode is None and proc is self.st_proc:
            try:
                await asyncio.wait_for(proc.wait(), 4)
                break
            except asyncio.TimeoutError:
                pass
            md = await mpv_query(ST_SOCK, 'metadata') or {}
            t = next((v for k, v in md.items() if k.lower() == 'icy-title'), '') or ''
            if t != self.st_title and proc is self.st_proc:
                self.st_title = t.strip()
                await self.show_stream()
                self.mark()
        if proc is self.st_proc and self.source == 'st':
            self.st_proc = None
            self.st_err = 'Stream ne radi. Proveri link ili internet.'
            await self.show_stream()
            self.mark()

    async def stream_stop(self):
        p, self.st_proc = self.st_proc, None
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                p.kill()

    async def stream_play(self, i):
        """i je redni broj u zajedničkoj listi (svetske + moje)."""
        lst = self.stream_all()
        if not lst:
            return
        self.st['stream']['u'] = lst[int(i) % len(lst)]['u']
        save_state(self.st)
        if self.source != 'st':
            await self.set_source('st')
        else:
            await self.stream_start()
        self.mark()

    async def show_stream(self):
        cur = self.stream_cur()
        name = cur[1]['n'] if cur else 'Radio'
        seq = self.stream_seq()
        k = next((j for j, x in enumerate(seq) if cur and x['u'] == cur[1]['u']), None)
        pos = '%d / %d' % (k + 1, len(seq)) if k is not None else ''
        line = self.st_err or self.st_title or (cur[1].get('g') if cur else '') or 'Uživo'
        err = bool(self.st_err)
        color = '#38bdf8'

        def build(w, h):
            esc = self._esc
            cx, cy, r = w / 2, h * 0.3, h * 0.095
            waves = ''.join(
                '<path d="M{:.0f} {:.0f}a{:.0f} {:.0f} 0 0 {} 0 {:.0f}" fill="none" stroke="{}" stroke-width="{:.0f}" stroke-linecap="round" stroke-opacity="{}"/>'
                .format(cx + sx * r * k, cy - r * k * 0.95, r * k, r * k, 1 if sx > 0 else 0, r * k * 1.9, color, h * 0.012, op)
                for k, op in ((1.4, .9), (1.8, .55), (2.2, .3)) for sx in (1, -1))
            return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="g" cx="50%" cy="20%" r="80%"><stop offset="0" stop-color="{color}" stop-opacity=".32"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#g)"/>
{waves}
<circle cx="{cx}" cy="{cy:.0f}" r="{r:.0f}" fill="{color}" fill-opacity=".16" stroke="{color}" stroke-width="{h * 0.008:.0f}"/>
<circle cx="{cx}" cy="{cy:.0f}" r="{r * 0.38:.0f}" fill="{color}"/>
<text x="{cx}" y="{h * 0.6:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.036:.0f}" letter-spacing="4" fill="{color}">INTERNET RADIO{('  ·  ' + pos) if pos else ''}</text>
<text x="{cx}" y="{h * 0.72:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.085:.0f}" fill="#ffffff">{esc(name[:26])}</text>
<text x="{cx}" y="{h * 0.82:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.04:.0f}" fill="{'#f87171' if err else '#c7ced8'}">{esc(line[:52])}</text>
</svg>'''
        await self._overlay(['st', name, pos, line], build)

    # ---------- izlaz zvuka (PipeWire) i Bluetooth (bluetoothctl)
    @staticmethod
    async def _run(*args, timeout=30):
        env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return ''
        return out.decode(errors='replace')

    async def audio_refresh(self):
        """Lista izlaza iz `wpctl status` (lagano, bez pw-dump)."""
        out = await self._run('wpctl', 'status', timeout=5)
        sinks, in_sinks = [], False
        for line in out.splitlines():
            body = line.replace('│', ' ').replace('├', ' ').replace('└', ' ').replace('─', ' ').strip()
            if body == 'Video':
                break                      # dalje su video uređaji
            if body.startswith('Sinks:'):
                in_sinks = True
                continue
            if in_sinks and body.endswith(':'):
                in_sinks = False           # počela je sledeća sekcija
                continue
            m = re.match(r'(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[vol:\s*([\d.]+).*\])?$', body) if in_sinks else None
            if m:
                name = m.group(3).strip()
                pretty = 'Speaker' if name.startswith('Built-in Audio') else name
                sinks.append({'id': int(m.group(2)), 'n': pretty, 'def': m.group(1) == '*', 'bt': not name.startswith('Built-in')})
                # jačinu menja samo plejer: izlaz (npr. nove BT slušalice, WirePlumber im da 60%) ide na 100%,
                # jednom po uređaju, da pun zvuk sa stranice ili telefona (Cast) bude zaista pun
                if name not in self.sink_full and m.group(4) and float(m.group(4)) < 1.0:
                    await self._run('wpctl', 'set-volume', m.group(2), '1.0', timeout=5)
                self.sink_full.add(name)
        self.audio = sinks
        await self.amp_sync()
        self.mark()

    async def set_output(self, sid):
        await self._run('wpctl', 'set-default', str(int(sid)), timeout=5)
        await asyncio.sleep(0.5)
        await self.audio_refresh()

    async def bt_refresh(self):
        out = await self._run('bluetoothctl', 'devices', timeout=8)
        devs = []
        for line in out.splitlines():
            m = re.match(r'Device ([0-9A-F:]{17}) (.+)', line.strip())
            if not m or re.fullmatch(r'[0-9A-F]{2}(-[0-9A-F]{2}){5}', m.group(2)):
                continue  # uređaji bez imena (samo adresa) nisu zanimljivi
            info = await self._run('bluetoothctl', 'info', m.group(1), timeout=5)
            flag = lambda k: re.search(r'%s: yes' % k, info) is not None
            icon = re.search(r'Icon: (\S+)', info)
            devs.append({'mac': m.group(1), 'n': m.group(2), 'paired': flag('Paired'), 'conn': flag('Connected'),
                         'audio': bool(icon and icon.group(1).startswith('audio'))})
        devs.sort(key=lambda d: (not d['conn'], not d['paired'], not d['audio'], d['n']))
        power = 'Powered: yes' in await self._run('bluetoothctl', 'show', timeout=5)
        self.bt = {'power': power, 'scan': self.bt.get('scan', False), 'devs': devs[:20]}
        self.mark()

    async def bt_cmd(self, c, mac=None):
        if mac and not re.fullmatch(r'[0-9A-F:]{17}', mac):
            return {'err': 'Neispravna adresa uređaja.'}
        await self._run('bluetoothctl', 'power', 'on', timeout=8)
        await self._run('bluetoothctl', 'pairable', 'on', timeout=8)
        if c == 'bt_scan':
            self.bt['scan'] = True
            self.mark()
            await self._run('bluetoothctl', '--timeout', '12', 'scan', 'on', timeout=20)
            self.bt['scan'] = False
            await self.bt_refresh()
            return {'ok': 'Pretraga završena'}
        if c == 'bt_pair':
            # agent „bez unosa“ potvrđuje uparivanje bez PIN-a; neke slušalice (Jabra) vrate grešku
            # pri pair, a upare se tek pri connect, pa se connect radi uvek
            await self._run('bluetoothctl', '--agent', 'NoInputNoOutput', '--timeout', '25', 'pair', mac, timeout=35)
            await self._run('bluetoothctl', 'trust', mac, timeout=8)
            c = 'bt_connect'
        if c == 'bt_connect':
            out = await self._run('bluetoothctl', '--agent', 'NoInputNoOutput', '--timeout', '15', 'connect', mac, timeout=25)
            await asyncio.sleep(2)
            await self.bt_refresh()
            await self.audio_refresh()
            bt_sink = next((s for s in self.audio if s['bt']), None)
            if bt_sink:
                await self.set_output(bt_sink['id'])   # zvuk odmah prebaci na slušalice
                return {'ok': 'Zvuk ide na ' + bt_sink['n']}
            return {'err': 'Povezano, ali slušalice nisu ponudile zvuk.'} if 'successful' in out else {'err': 'Povezivanje nije uspelo.'}
        if c == 'bt_disconnect':
            await self._run('bluetoothctl', 'disconnect', mac, timeout=10)
        if c == 'bt_remove':
            await self._run('bluetoothctl', 'remove', mac, timeout=10)
        await asyncio.sleep(1)
        await self.bt_refresh()
        await self.audio_refresh()
        return {}

    # ---------- cast iz YouTube aplikacije / Chrome-a (Node program cast/cast.mjs)
    async def cast_set(self, on):
        self.st['cast'] = bool(on)
        save_state(self.st)
        if on:
            await self.cast_start()
        else:
            await self.cast_stop()
        self.mark()

    # Cast radi kao posebna korisnička usluga (teco-cast.service, kao teco-player): restart servera
    # ga ne prekida, pa telefon ostaje povezan; systemd ga sam ponovo pokreće ako padne.
    async def cast_refresh(self):
        self.cast_run = (await self._run('systemctl', '--user', 'is-active', CAST_UNIT, timeout=5)).strip() == 'active'
        if not self.cast_run:
            self.cast_sender = None
        self.mark()

    async def cast_start(self):
        await self._run('systemctl', '--user', 'start', CAST_UNIT, timeout=15)
        await self.cast_refresh()

    async def cast_stop(self):
        await self._run('systemctl', '--user', 'stop', CAST_UNIT, timeout=15)
        self.cast_sender = None
        await self.cast_refresh()

    # ---------- deljenje fajlova (Samba): folder na SD kartici + USB diskovi (npr. ISO igrice za PS2 / OPL)
    @staticmethod
    async def _rc(*args, inp=None, timeout=30):
        """Kao _run, ali vraća i izlazni kod (i može da pošalje ulaz)."""
        p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if inp is not None else asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(inp.encode() if inp is not None else None), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return 1, 'isteklo je vreme'
        return p.returncode, out.decode(errors='replace').strip()

    def smb_cfg(self):
        return self.st.setdefault('smb', {'on': False, 'usb': True})

    async def usb_scan(self):
        """USB particije sa fajl sistemom (lsblk). id = UUID (ili uređaj), n = ime deljenja."""
        _, out = await self._rc('lsblk', '-J', '-b', '-o', 'NAME,PATH,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINT,TRAN', timeout=10)
        try:
            devs = json.loads(out).get('blockdevices') or []
        except ValueError:
            return getattr(self, 'usb', [])
        parts, names = [], {'teco'}
        for d in devs:
            if d.get('tran') != 'usb':
                continue
            for p in d.get('children') or [d]:   # disk bez particija (ceo disk formatiran)
                if not p.get('fstype'):
                    continue
                n = re.sub(r'[^A-Za-z0-9_-]', '', (p.get('label') or '').replace(' ', '_'))[:20] or 'USB'
                base, i = n, 2
                while n.lower() in names:
                    n, i = '%s%d' % (base, i), i + 1
                names.add(n.lower())
                parts.append({'id': p.get('uuid') or p['path'], 'dev': p['path'], 'fs': p['fstype'], 'n': n,
                              'label': p.get('label') or '', 'size': int(p.get('size') or 0), 'mnt': p.get('mountpoint')})
        return parts

    async def usb_mount(self, p):
        path = USB_MNT / p['n']
        opts = 'noatime'
        if p['fs'] in ('vfat', 'exfat', 'ntfs'):   # bez Linux vlasnika: fajlovi pripadaju Teco.Pi korisniku
            opts += ',uid=%d,gid=%d,umask=002' % (os.getuid(), os.getgid()) + (',utf8' if p['fs'] == 'vfat' else '')
        await self._rc('sudo', '-n', 'mkdir', '-p', str(path), timeout=5)
        rc, out = await self._rc('sudo', '-n', 'mount', '-t', USB_FS[p['fs']], '-o', opts, p['dev'], str(path), timeout=30)
        if rc:   # npr. NTFS koji nije pravilno izbačen: bar za čitanje
            rc, out2 = await self._rc('sudo', '-n', 'mount', '-t', USB_FS[p['fs']], '-o', opts + ',ro', p['dev'], str(path), timeout=30)
            if rc:
                await self._rc('sudo', '-n', 'rmdir', str(path), timeout=5)
                return out.splitlines()[-1][:120] if out else 'montiranje nije uspelo'
            log.info('USB %s montiran samo za čitanje: %s', p['dev'], out)
        log.info('USB %s (%s) montiran na %s', p['dev'], p['fs'], path)
        p['mnt'] = str(path)
        return None

    async def usb_umount(self, path, lazy=False):
        rc, out = await self._rc('sudo', '-n', 'umount', *(['-l'] if lazy else []), path, timeout=30)
        if not rc:
            await self._rc('sudo', '-n', 'rmdir', path, timeout=5)
        return None if not rc else (out.splitlines()[-1][:120] if out else 'disk je zauzet')

    @staticmethod
    def teco_mounts():
        """{mesto: uređaj} za sve što je montirano u /media/teco."""
        m = {}
        try:
            for line in Path('/proc/mounts').read_text().splitlines():
                dev, mnt = line.split()[:2]
                mnt = mnt.replace('\\040', ' ')
                if mnt.startswith(str(USB_MNT) + '/'):
                    m[mnt] = dev
        except OSError:
            pass
        return m

    async def smb_refresh(self, force=False):
        """Na 10 s: USB diskovi (montira nove, skida izvađene), smb.conf i smbd prema podešavanju."""
        if not SMB_BIN.exists() or getattr(self, 'smb_installing', False):
            return
        if not hasattr(self, 'smb_lock'):
            self.smb_lock = asyncio.Lock()
        async with self.smb_lock:   # petlja i komande iz admina ne smeju da montiraju isto u isto vreme
            await self._smb_refresh(force)

    async def _smb_refresh(self, force):
        cfg = self.smb_cfg()
        auto = cfg['on'] and cfg.get('usb', True)
        parts = await self.usb_scan()
        ids = {p['id'] for p in parts}
        self.usb_skip = {k: v for k, v in getattr(self, 'usb_skip', {}).items() if k in ids}   # izvađen disk: zaboravi grešku/izbacivanje
        devs = {p['dev'] for p in parts}
        for mnt, dev in self.teco_mounts().items():
            if dev not in devs or not auto:   # disk izvađen bez „Izbaci“ ili je deljenje isključeno
                await self.usb_umount(mnt, lazy=dev not in devs)
                for p in parts:
                    if p['mnt'] == mnt:
                        p['mnt'] = None
        if auto:
            for p in parts:
                if not p['mnt'] and p['id'] not in self.usb_skip:
                    if p['fs'] not in USB_FS:
                        self.usb_skip[p['id']] = 'format %s nije podržan' % p['fs']
                    else:
                        err = await self.usb_mount(p)
                        if err:
                            self.usb_skip[p['id']] = err
        self.usb = parts
        shares = [{'n': 'Teco', 'path': str(SHARE), 'usb': False, 'fs': 'sd'}]
        SHARE.mkdir(exist_ok=True)
        dirs = cfg.setdefault('dirs', {})   # {UUID diska: folder na disku koji se deli}, prazno = ceo disk
        for p in parts:
            if p['mnt'] and auto:
                rel = dirs.get(p['id'], '')
                path = Path(p['mnt']) / rel
                if rel and not path.is_dir():   # folder obrisan ili preimenovan: deli ceo disk
                    path, rel = Path(p['mnt']), ''
                n = p['n']
                if rel:   # deljenje se zove kao folder (npr. ps2share), pa OPL ne treba prepodešavati
                    n = re.sub(r'[^A-Za-z0-9_-]', '', Path(rel).name.replace(' ', '_'))[:20] or n
                base_n, i = n, 2
                while n.lower() in {s['n'].lower() for s in shares}:
                    n, i = '%s%d' % (base_n, i), i + 1
                shares.append({'n': n, 'path': str(path), 'dir': rel, 'usb': True, 'dev': p['dev'], 'fs': p['fs'], 'label': p['label']})
        for s in shares:
            try:
                du = shutil.disk_usage(s['path'])
                s['size'], s['free'] = du.total, du.free
            except OSError:
                pass
            s['opl'] = os.path.isdir(os.path.join(s['path'], 'DVD'))
            s['games'] = self.share_games(s['path'])
        self.smb_shares = shares
        conf = self.smb_conf_text(shares)
        if force or conf != getattr(self, 'smb_conf', None):
            try:
                same = SMB_CONF.read_text() == conf
            except OSError:
                same = False
            if not same:
                p = await asyncio.create_subprocess_exec('sudo', '-n', 'tee', str(SMB_CONF), stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.communicate(conf.encode())
                if not p.returncode and cfg['on'] and getattr(self, 'smb_run', False):
                    await self._rc('sudo', '-n', 'systemctl', 'reload', 'smbd', timeout=15)
            self.smb_conf = conf
        run = (await self._run('systemctl', 'is-active', 'smbd', timeout=5)).strip() == 'active'
        if cfg['on'] and not run:
            await self._rc('sudo', '-n', 'systemctl', 'enable', '--now', 'smbd', timeout=30)
        elif not cfg['on'] and (run or force):
            await self._rc('sudo', '-n', 'systemctl', 'disable', '--now', 'smbd', 'nmbd', timeout=30)
        self.smb_run = (await self._run('systemctl', 'is-active', 'smbd', timeout=5)).strip() == 'active'
        self.mark()

    @staticmethod
    def smb_conf_text(shares):
        g = ['# Teco.Pi: ovaj fajl pravi server.py (Admin > Deljenje fajlova); ručne izmene se gube.',
             '[global]',
             '   workgroup = WORKGROUP', '   server string = Teco.Pi', '   netbios name = TECOPI',
             '   server role = standalone server', '   map to guest = Bad User', '   guest account = ' + USER,
             '   server min protocol = NT1', '   ntlm auth = ntlmv1-permitted',   # PS2 Open PS2 Loader zna samo SMB1
             '   disable netbios = yes', '   smb ports = 445',
             '   load printers = no', '   printing = bsd', '   printcap name = /dev/null', '   disable spoolss = yes',
             '   logging = systemd', '   log level = 1', '   strict sync = no', '   use sendfile = yes']
        for s in shares:
            g += ['', '[%s]' % s['n'], '   path = ' + s['path'], '   guest ok = yes', '   read only = no',
                  '   force user = ' + USER, '   create mask = 0664', '   directory mask = 0775']
        return '\n'.join(g) + '\n'

    def smb_snapshot(self):
        cfg = self.smb_cfg()
        shown = {s.get('dev') for s in getattr(self, 'smb_shares', [])}
        skip = getattr(self, 'usb_skip', {})
        return {'inst': SMB_BIN.exists(), 'installing': bool(getattr(self, 'smb_installing', False)),
                'err': getattr(self, 'smb_err', None), 'on': cfg['on'], 'usb': cfg.get('usb', True), 'pw': bool(cfg.get('pw')),
                'user': USER, 'run': bool(getattr(self, 'smb_run', False)),
                'shares': getattr(self, 'smb_shares', []) if cfg['on'] else [],
                'disks': [{'dev': p['dev'], 'n': p['label'] or p['n'], 'fs': p['fs'], 'size': p['size'],
                           'msg': skip.get(p['id']) or ('' if cfg['on'] and cfg.get('usb', True) else 'nije deljen')}
                          for p in getattr(self, 'usb', []) if p['dev'] not in shown]}

    async def smb_set(self, c, v):
        cfg = self.smb_cfg()
        cfg['on' if c == 'smb' else 'usb'] = v
        save_state(self.st)
        if not SMB_BIN.exists():
            return {'err': 'Samba nije instalirana.'}
        await self.smb_refresh(force=True)
        if c == 'smb_usb':
            return {'ok': 'USB diskovi se dele' if v else 'USB diskovi se više ne dele'}
        if v and not self.smb_run:
            return {'err': 'Samba nije pokrenuta (journalctl -u smbd).'}
        return {'ok': 'Deljenje fajlova je uključeno' if v else 'Deljenje fajlova je isključeno'}

    async def smb_install(self):
        if getattr(self, 'smb_installing', False):
            return {'ok': 'Instalacija je već u toku…'}
        self.smb_installing, self.smb_err = True, None
        self.mark()

        async def job():
            try:
                await self._rc('sudo', '-n', 'apt-get', 'update', timeout=600)
                rc, out = await self._rc('sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
                                         '--no-install-recommends', 'samba', timeout=1800)
                if rc:
                    self.smb_err = 'Instalacija nije uspela: ' + (out.splitlines() or [''])[-1][:160]
                    log.warning('samba: %s', out[-2000:])
                else:   # paket sam pokreće smbd/nmbd sa svojim podešavanjem: gasi dok se ne uključi u adminu
                    await self._rc('sudo', '-n', 'systemctl', 'disable', '--now', 'smbd', 'nmbd', 'samba-ad-dc', timeout=60)
            finally:
                self.smb_installing = False
            await self.smb_refresh(force=True)
        asyncio.get_running_loop().create_task(job())
        return {'ok': 'Instaliram Samba, traje par minuta…'}

    async def smb_password(self, pw):
        """Lozinka za prijavu sa Windows-a (Windows 10/11 ne puštaju gosta)."""
        pw = str(pw or '')
        if not 4 <= len(pw) <= 64:
            return {'err': 'Lozinka treba da ima 4 do 64 znaka.'}
        rc, out = await self._rc('sudo', '-n', 'smbpasswd', '-s', '-a', USER, inp='%s\n%s\n' % (pw, pw), timeout=15)
        if rc:
            return {'err': 'Lozinka nije sačuvana: ' + out[:120]}
        self.smb_cfg()['pw'] = True
        save_state(self.st)
        self.mark()
        return {'ok': 'Lozinka sačuvana. Korisnik: ' + USER}

    async def usb_eject(self, dev):
        p = next((p for p in getattr(self, 'usb', []) if p['dev'] == dev), None)
        if not p or not p['mnt']:
            return {'err': 'Disk nije priključen.'}
        async with self.smb_lock:
            self.usb_skip[p['id']] = 'izbačen · izvadi i ponovo priključi za deljenje'
            for s in self.smb_shares:
                if s.get('dev') == dev:
                    await self._rc('sudo', '-n', 'smbcontrol', 'smbd', 'close-share', s['n'], timeout=10)
            await self._rc('sync', timeout=60)
            err = await self.usb_umount(p['mnt'])
            if err:
                self.usb_skip.pop(p['id'], None)
                return {'err': 'Disk je zauzet, pokušaj ponovo: ' + err}
            await self._smb_refresh(False)
        return {'ok': 'Disk %s je izbačen, možeš da ga izvadiš.' % (p['label'] or p['n'])}

    @staticmethod
    def share_games(path):
        """Igrice u OPL folderima (DVD, CD, POPS) jednog deljenja."""
        games = []
        for d in ('DVD', 'CD', 'POPS'):
            try:
                with os.scandir(os.path.join(path, d)) as it:
                    for e in it:
                        if e.is_file() and not e.name.startswith('.') and os.path.splitext(e.name)[1].lower() in GAME_EXT:
                            games.append({'f': d + '/' + e.name, 'size': e.stat().st_size})
            except OSError:
                pass
        return sorted(games, key=lambda g: g['f'].lower())

    def share_base(self, name):
        s = next((s for s in getattr(self, 'smb_shares', []) if s['n'] == name), None)
        return (Path(s['path']), s) if s and self.smb_cfg()['on'] else (None, None)

    @staticmethod
    def inside(base, rel):
        """base/rel samo ako ne izlazi iz base (bez ../)."""
        base = base.resolve()
        p = (base / str(rel or '').strip('/')).resolve()
        return p if p == base or base in p.parents else None

    async def smb_ls(self, a):
        """Folderi na USB disku (za izbor šta se deli)."""
        p = next((p for p in getattr(self, 'usb', []) if p['dev'] == a.get('dev') and p['mnt']), None)
        if not p:
            return {'err': 'Disk nije priključen.'}
        d = self.inside(Path(p['mnt']), a.get('path'))
        if not d or not d.is_dir():
            return {'err': 'Folder ne postoji.'}
        skip = {'System Volume Information', '$RECYCLE.BIN', 'lost+found'}
        try:
            dirs = sorted((e.name for e in os.scandir(d) if e.is_dir() and not e.name.startswith('.') and e.name not in skip), key=str.lower)
        except OSError as e:
            return {'err': 'Ne mogu da pročitam folder: %s' % e.strerror}
        root = Path(p['mnt']).resolve()
        return {'path': '' if d == root else str(d.relative_to(root)), 'dirs': dirs[:300]}

    async def smb_dir(self, a):
        p = next((p for p in getattr(self, 'usb', []) if p['dev'] == a.get('dev') and p['mnt']), None)
        if not p:
            return {'err': 'Disk nije priključen.'}
        d = self.inside(Path(p['mnt']), a.get('path'))
        if not d or not d.is_dir():
            return {'err': 'Folder ne postoji.'}
        rel = '' if d == Path(p['mnt']).resolve() else str(d.relative_to(Path(p['mnt']).resolve()))
        self.smb_cfg().setdefault('dirs', {})[p['id']] = rel
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': '%s deli %s' % (p['n'], rel or 'ceo disk')}

    async def smb_opl(self, a):
        base, _ = self.share_base(a.get('share'))
        if not base:
            return {'err': 'Deljenje nije pronađeno.'}
        try:
            for d in OPL_DIRS:
                (base / d).mkdir(exist_ok=True)
        except OSError as e:
            return {'err': 'Folderi nisu napravljeni: %s' % e.strerror}
        await self.smb_refresh()
        return {'ok': 'OPL folderi su napravljeni u %s' % a.get('share')}

    async def game_del(self, a):
        base, _ = self.share_base(a.get('share'))
        f = self.inside(base, a.get('f')) if base else None
        if not f or not f.is_file() or f.parent.name not in ('DVD', 'CD', 'POPS'):
            return {'err': 'Igrica nije pronađena.'}
        f.unlink()
        await self.smb_refresh()
        return {'ok': 'Obrisano: ' + f.name}

    # ---------- Paradox alarm (proof of concept): stanje sa mosta, uključi/isključi particije
    def alarm_cfg(self):
        try:
            return json.loads(ALARM_CFG.read_text())
        except (OSError, ValueError):
            return {}

    async def alarm_cfg_set(self, a):
        """Admin: podešavanje IP modula. Prazna lozinka = zadrži staru. Lozinke se nikad ne šalju nazad."""
        c = self.alarm_cfg()
        host = str(a.get('host') or '').strip()
        if a.get('enabled') and not re.fullmatch(r'[A-Za-z0-9.\-]{1,253}', host):
            return {'err': 'Upiši IP adresu IP150 modula (npr. 172.10.10.50).'}
        try:
            port = int(a.get('port') or 10000)
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            return {'err': 'Port nije ispravan (obično 10000).'}
        c.update(host=host, port=port, enabled=bool(a.get('enabled')), name=str(a.get('name') or '').strip()[:40])
        for k in ('ip_password', 'pc_password'):
            if a.get(k):
                c[k] = str(a[k])[:32]
        ALARM_CFG.write_text(json.dumps(c))
        os.chmod(ALARM_CFG, 0o600)
        await self._run('systemctl', '--user', 'restart' if c['enabled'] else 'stop', ALARM_UNIT, timeout=20)
        self.alarm = {}
        self.mark()
        return {'ok': 'Alarm: podešavanje sačuvano' + (', povezujem se…' if c['enabled'] else ' (isključen).')}

    async def alarm_post(self, path, body):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
                async with s.post(ALARM_API + path, json=body) as r:
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {'err': 'Alarm servis ne odgovara.'}

    async def alarm_extra(self, c, a):
        """Monitor zone (dnevnik), brisanje memorije alarma, brisanje dnevnika."""
        if c == 'alarm_monitor':
            cfg = self.alarm_cfg()
            mon = {int(x) for x in cfg.get('monitor', [])}
            z = int(a.get('zone'))
            (mon.add if a.get('on') else mon.discard)(z)
            cfg['monitor'] = sorted(mon)
            ALARM_CFG.write_text(json.dumps(cfg))
            os.chmod(ALARM_CFG, 0o600)
            self.mark()
            return {'ok': 'Zona se ' + ('prati: događaji se čuvaju' if a.get('on') else 'više ne prati')}
        if c == 'alarm_mem_clear':
            r = await self.alarm_post('/zone', {'p': 'all', 'cmd': 'clear_alarm_memory'})
            return {'ok': 'Memorija alarma je obrisana'} if r.get('ok') else {'err': r.get('err') or 'Nije uspelo.'}
        if c == 'alarm_ev_clear':
            r = await self.alarm_post('/events/clear', {})
            return {'ok': 'Dnevnik događaja je obrisan'} if r.get('ok') else {'err': r.get('err') or 'Nije uspelo.'}

    async def alarm_do(self, a):
        """Particija (uključi u režimu Regular/Stay/Instant/Force, isključi) ili PGM izlaz (a['pgm'])."""
        cmd = str(a.get('cmd') or '')
        pgm = a.get('pgm') is not None
        if cmd not in (PGM_CMDS if pgm else ALARM_CMDS):
            return {'err': 'Nepoznata komanda.'}
        import aiohttp
        path, target = ('/pgm', str(a.get('pgm'))) if pgm else ('/partition', str(a.get('p') or 'all'))

        async def post(c):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(ALARM_API + path, json={'p': target, 'cmd': c}) as r:
                    return await r.json()
        try:
            res = await post('on' if cmd == 'pulse' else cmd)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {'err': 'Alarm servis ne odgovara.'}
        if cmd == 'pulse' and res.get('ok'):
            # „aktiviraj na 5 s“: isključenje radi server (i ako se stranica zatvori)
            async def later():
                await asyncio.sleep(5)
                try:
                    r2 = await post('off')
                    log.info('alarm pgm: %s off posle 5 s -> %s', target, r2)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.warning('alarm pgm: %s off nije uspeo: %s', target, e)
            asyncio.get_running_loop().create_task(later())
        log.info('alarm %s: %s %s -> %s', path[1:], target, cmd, res)
        self.alarm_poll_now = True
        if not res.get('ok'):
            return {'err': res.get('err') or 'Komanda nije uspela.'}
        return {'ok': ('PGM ' + target + ': ' + PGM_CMDS[cmd]) if pgm else 'Alarm: ' + ALARM_CMDS[cmd]}

    async def alarm_loop(self):
        """Dugo čekanje na most (/wait?v=): čim se na centrali nešto promeni (zona, particija, smetnja,
        događaj), most odmah odgovori novim stanjem i stranica ga dobija za delić sekunde."""
        import aiohttp
        self.alarm = {}
        v = -1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            while True:
                if not self.alarm_cfg().get('enabled'):
                    if self.alarm:
                        self.alarm = {}
                        self.mark()
                    v = -1
                    await asyncio.sleep(2)
                    continue
                try:
                    async with s.get(ALARM_API + '/wait', params={'v': v}) as r:
                        st = await r.json()
                    v = st.pop('v', -1)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    st = {'run': 'down', 'err': 'Alarm servis se pokreće…', 'partitions': [], 'zones': []}
                    v = -1
                    await asyncio.sleep(2)
                if st != self.alarm:
                    self.alarm = st
                    self.mark()

    def alarm_snapshot(self):
        c = self.alarm_cfg()
        st = self.alarm or {}
        zk = ('id', 'key', 'label', 'open', 'bypassed', 'alarm', 'tamper', 'fire', 'low_battery', 'generated_alarm', 'was_in_alarm',
              'supervision_trouble', 'in_tx_delay', 'shutdown', 'fire_alarm')
        pk = ('id', 'key', 'label', 'arm', 'arm_stay', 'arm_away', 'arm_sleep', 'arm_no_entry', 'alarm', 'current_state', 'target_state', 'ready',
              'ready_status', 'exit_delay', 'entry_delay', 'fire_alarm', 'audible_alarm', 'silent_alarm', 'alarm_in_memory',
              'trouble', 'zone_bypassed', 'panic_alarm', 'bell')
        sysd = st.get('system') or {}
        tr = sysd.get('troubles') or {}
        parts = st.get('partitions', [])
        return {
            'id': 'a1',   # za sada jedan alarm; lista (alarms) je spremna za više
            'name': c.get('name') or (parts[0].get('label') if parts else '') or 'Alarm',
            'on': bool(c.get('enabled')),
            'cfg': {'host': c.get('host', ''), 'port': c.get('port', 10000), 'name': c.get('name', ''),
                    'ip_pw': bool(c.get('ip_password')), 'pc_pw': bool(c.get('pc_password'))},
            'troubles': sorted(k for k, v in tr.items() if v is True and not k.startswith('_')),
            'power': {k: sysd.get('power', {}).get(k) for k in ('vdc', 'battery', 'dc') if isinstance(sysd.get('power', {}).get(k), (int, float))},
            'events': (st.get('events') or [])[-60:],
            'monitor': [int(x) for x in c.get('monitor', [])],
            'run': st.get('run'), 'err': st.get('err'), 'panel': st.get('panel'),
            'parts': [{k: p.get(k) for k in pk if k in p} for p in st.get('partitions', [])],
            'zones': [{k: z.get(k) for k in zk if k in z} for z in st.get('zones', [])],
            'pgms': [{k: g.get(k) for k in ('id', 'key', 'label', 'on', 'disabled') if k in g} for g in st.get('pgms', [])],
        }

    async def cast_api(self, a):
        """Komande od cast programa (samo sa ovog uređaja)."""
        c = a.get('a')
        if c not in ('status', 'play'):   # play ima svoj zapis
            log.info('cast: %s %s', c, {k: v for k, v in a.items() if k != 'a'} or '')
        if c == 'status':
            p = self.mpv.props
            cur = (self.meta.get(p.get('path') or '') or {}).get('url')
            vid = self._vid(cur) if cur and self.mode == 'yt' and not p.get('idle-active') else None
            hold = getattr(self, 'cast_hold', None)
            loading = bool(self.pending)
            if hold:   # video sa telefona se još učitava: ne javljaj da je završen ili zamenjen
                if vid == hold[0] and p.get('time-pos') is not None:
                    self.cast_hold = None
                elif time.time() < hold[1]:
                    vid, loading = hold[0], True
                else:
                    self.cast_hold = None
            return {'vid': vid, 'pos': p.get('time-pos') or 0, 'dur': p.get('duration') or 0, 'paused': bool(p.get('pause')),
                    'vol': self.cur_vol(), 'mute': self.cur_mute(), 'loading': loading}
        if c == 'play':
            vid = str(a.get('id', ''))
            if not re.fullmatch(r'[\w-]{11}', vid):
                return {'ok': False}
            log.info('cast pusti: %s od %s s', vid, a.get('pos'))
            self.cast_seq = getattr(self, 'cast_seq', 0) + 1
            seq = self.cast_seq
            self.cast_hold = (vid, time.time() + 25)
            url = 'https://www.youtube.com/watch?v=' + vid
            try:
                title = (await self.resolve(url))[0]   # priprema (keš); ako za to vreme stigne novi zahtev, ovaj je zastareo
            except RuntimeError:
                if seq == self.cast_seq:
                    self.cast_hold = None
                raise
            if seq != self.cast_seq:   # npr. reklama pa pravi video: pušta se samo poslednji
                log.info('cast: %s preskočen, stigao je noviji zahtev', vid)
                return {'ok': True}
            if self.source != 'av1':
                await self.set_source('av1', title)
            try:
                await self.add_youtube(url, True, float(a.get('pos') or 0), src='cast')
            except RuntimeError:
                self.cast_hold = None
                raise
            await self.mpv.cmd('set_property', 'pause', False)
        elif c == 'pause':
            await self.mpv.cmd('set_property', 'pause', True)
        elif c == 'resume':
            await self.mpv.cmd('set_property', 'pause', False)
        elif c == 'stop':
            await self.mpv.cmd('set_property', 'pause', True)
        elif c == 'seek':
            await self.mpv.cmd('seek', float(a.get('pos') or 0), 'absolute')
        elif c == 'volume':
            log.info('cast jačina: %s', a)
            await self.set_volume(a.get('level', 70))   # ovo i skida utišavanje
            if a.get('muted'):
                await self.mpv.cmd('set_property', 'mute', True)
        elif c == 'sender':
            self.cast_sender = str(a.get('name') or 'Telefon')[:40] if a.get('on') else None
        self.mark()
        return {'ok': True}

    # ---------- događaji: zvono i interfon
    def _event_svg(self, eid, w, h, when):
        ev = self.st['events'][eid]
        color, icon = EVENT_LOOK.get(eid, ('#2dd4bf', ''))
        esc = self._esc
        s = int(h * 0.36)
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="eg" cx="50%" cy="38%" r="60%"><stop offset="0" stop-color="{color}" stop-opacity=".35"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#eg)"/>
<circle cx="{w / 2}" cy="{h * 0.36:.0f}" r="{s * 0.72:.0f}" fill="{color}" fill-opacity=".14" stroke="{color}" stroke-opacity=".5" stroke-width="3"/>
<svg x="{w / 2 - s / 2:.0f}" y="{h * 0.36 - s / 2:.0f}" width="{s}" height="{s}" viewBox="0 0 100 100" color="{color}">{icon}</svg>
<text x="{w / 2}" y="{h * 0.76:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.085:.0f}" fill="#ffffff">{esc(ev['n'])}</text>
<text x="{w / 2}" y="{h * 0.86:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.042:.0f}" fill="#9aa4b3">{esc(when)}</text>
</svg>'''

    @staticmethod
    def _alarm_wav(path, eid):
        """Ugrađen zvuk alarma (bez fajla sa strane): požar = tri oštra pištanja (kao detektor dima),
        voda = dvotonska sirena. Ponavlja se dok je alarm na ekranu."""
        import math
        import struct
        import wave
        rate, out = 22050, bytearray()

        def tone(f, sec, vol=0.55):
            n = int(rate * sec)
            for i in range(n):
                env = min(1, i / 200, (n - i) / 200)   # bez pucketanja na ivicama
                out.extend(struct.pack('<h', int(32767 * vol * env * math.sin(2 * math.pi * f * i / rate))))

        if eid == 'pozar':
            for _ in range(3):
                tone(3100, 0.5)
                tone(0, 0.5)
            tone(0, 1.0)
        else:
            for _ in range(4):
                tone(880, 0.35)
                tone(660, 0.35)
            tone(0, 0.6)
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(out))

    def event_sound(self, eid):
        d = EVENTS_DIR / eid
        files = sorted(p for p in d.glob('*') if p.suffix.lower() in AUDIO_EXT) if d.exists() else []
        return files[0] if files else None

    async def events_assets(self):
        """Folder i slika (image.png, za web pregled) za svaki događaj; slika se osvežava kad se promeni naziv."""
        for eid, ev in self.st['events'].items():
            d = EVENTS_DIR / eid
            d.mkdir(parents=True, exist_ok=True)
            if eid in ALARMS and not (d / 'alarm.wav').exists():
                self._alarm_wav(d / 'alarm.wav', eid)
            png, stamp = d / 'image.png', d / '.image-name'
            if png.exists() and stamp.exists() and stamp.read_text() == ev['n']:
                continue
            svg = d / '.image.svg'
            svg.write_text(self._event_svg(eid, 1024, 576, EVENT_SUB.get(eid, '')))
            p = await asyncio.create_subprocess_exec('rsvg-convert', '-w', '1024', '-h', '576', '-o', str(png), str(svg))
            await p.wait()
            stamp.write_text(ev['n'])

    async def fire_event(self, eid):
        """Pokazuje sliku preko ekrana, pušta zvuk i posle ev['sec'] sekundi vraća sve kako je bilo."""
        ev = self.st['events'].get(eid)
        if not ev or not ev.get('on'):
            return {'err': 'Događaj je isključen.'}
        if self.ev_now and not (eid in ALARMS and self.ev_now['id'] not in ALARMS):
            return {'err': 'Već je prikazan jedan događaj.'}
        if self.ev_now:   # alarm ima prednost nad zvonom/interfonom
            await self.ev_task_end()
        when = EVENT_SUB.get(eid, '') + ' · ' + time.strftime('%H:%M')
        self.ev_now = {'id': eid, 'n': ev['n'], 'at': time.time(), 'sec': ev['sec']}
        self.mark()
        resume = ev.get('pause') and self.source == 'av1' and self.mode == 'yt' and not self.mpv.props.get('pause')
        if resume:
            await self.mpv.cmd('set_property', 'pause', True)
        await self._overlay(['ev', eid, ev['n'], when], lambda w, h: self._event_svg(eid, w, h, when), oid=2)
        snd, proc = self.event_sound(eid), None
        if snd:
            vol = int(self.mpv.props.get('volume') or 70)
            proc = await asyncio.create_subprocess_exec(
                'mpv', '--no-config', '--no-video', '--ao=pipewire', '--really-quiet', '--volume=%d' % max(vol, 60), *(['--loop-file=inf'] if eid in ALARMS else []), str(snd),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid()))

        async def cleanup():
            if proc and proc.returncode is None:
                proc.terminate()
            await self.mpv.cmd('overlay-remove', 2)
            if resume:
                await self.mpv.cmd('set_property', 'pause', False)
            self.ev_now = None
            self.mark()

        async def finish():
            try:
                await asyncio.sleep(max(1, int(ev['sec'])))
            except asyncio.CancelledError:
                pass
            await cleanup()
        self.ev_task = asyncio.get_running_loop().create_task(finish())
        return {'ok': ev['n'] + (' · pušta se zvuk ' + snd.name if snd else ' · bez zvuka (dodaj fajl u folder)')}

    async def ev_task_end(self):
        t = getattr(self, 'ev_task', None)
        if t and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    def events_snapshot(self):
        out = []
        for eid, ev in self.st['events'].items():
            snd = self.event_sound(eid)
            out.append({'id': eid, 'n': ev['n'], 'on': ev.get('on', True), 'sec': ev['sec'], 'pause': ev.get('pause', True),
                        'sound': snd.name if snd else None, 'dir': str(EVENTS_DIR / eid), 'alarm': eid in ALARMS})
        return out

    # ---------- mreža
    async def net_refresh(self):
        import socket
        net = {'host': socket.gethostname(), 'ifs': [], 'gw': None, 'ssid': None, 'signal': None}
        try:
            for i in json.loads(await self._run('ip', '-j', 'addr', timeout=5)):
                if i.get('ifname') == 'lo':
                    continue
                v4 = [a['local'] + '/' + str(a['prefixlen']) for a in i.get('addr_info', []) if a.get('family') == 'inet']
                net['ifs'].append({'n': i['ifname'], 'up': i.get('operstate') == 'UP', 'ip': v4, 'mac': i.get('address')})
            r = json.loads(await self._run('ip', '-j', 'route', 'show', 'default', timeout=5))
            net['gw'] = r[0].get('gateway') if r else None
        except (ValueError, KeyError, IndexError):
            pass
        # podešavanje kablovske mreže (NetworkManager): DHCP ili statička adresa
        con = await self._eth_con()
        if con:
            out = await self._run('nmcli', '-t', '-f', 'ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv4.ignore-auto-dns', 'con', 'show', con, timeout=5)
            cfg = dict(l.split(':', 1) for l in out.splitlines() if ':' in l)
            net['eth'] = {'con': con, 'mode': 'static' if cfg.get('ipv4.method') == 'manual' else 'dhcp',
                          'ip': cfg.get('ipv4.addresses', ''), 'gw': cfg.get('ipv4.gateway', '').replace('--', '')}
            dns = cfg.get('ipv4.dns', '').replace('--', '').replace(',', ' ').split()
            net['dns'] = {'mode': 'manual' if cfg.get('ipv4.ignore-auto-dns') == 'yes' or (net['eth']['mode'] == 'static' and dns) else 'auto',
                          'servers': dns}
        try:   # DNS koji se stvarno koristi (NetworkManager ga upisuje ovde)
            net.setdefault('dns', {'mode': 'auto', 'servers': []})['active'] = re.findall(r'^nameserver\s+(\S+)', Path('/etc/resolv.conf').read_text(), re.M)
        except OSError:
            pass
        # sat: NTP (systemd-timesyncd)
        sync = (await self._run('timedatectl', 'show', '-p', 'NTPSynchronized', '--value', timeout=5)).strip()
        ts = dict(l.split('=', 1) for l in (await self._run('timedatectl', 'show-timesync', '-p', 'ServerName', '-p', 'ServerAddress', timeout=5)).splitlines() if '=' in l)
        try:
            m = re.search(r'^NTP=(.*)$', NTP_CONF.read_text(), re.M)
            cfg_ntp = m.group(1).split() if m else []
        except OSError:
            cfg_ntp = []
        net['ntp'] = {'sync': sync == 'yes', 'server': ts.get('ServerName') or '', 'addr': ts.get('ServerAddress') or '', 'servers': cfg_ntp}
        wifi = await self._run('nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL', 'dev', 'wifi', timeout=5)
        for line in wifi.splitlines():
            if line.startswith('yes:'):
                _, net['ssid'], net['signal'] = line.split(':', 2)
        self.net = net
        self.mark()

    async def _eth_con(self):
        out = await self._run('nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', timeout=5)
        for line in out.splitlines():
            name, _, typ = line.rpartition(':')
            if typ == '802-3-ethernet':
                return name.replace('\\:', ':')
        return None

    # ---------- Wi-Fi (nmcli): traženje mreža, povezivanje, brisanje sačuvanih
    @staticmethod
    def _nm_split(line):
        """nmcli -t deli polja sa ':' a ':' u vrednosti piše kao '\\:'."""
        return [p.replace('\x00', ':') for p in line.replace('\\:', '\x00').split(':')]

    async def wifi_scan(self):
        self.wifi['scanning'] = True
        self.mark()
        try:
            out = await self._run('nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', '--rescan', 'yes', timeout=30)
            saved = await self._wifi_saved()
            best = {}
            for line in out.splitlines():
                f = self._nm_split(line)
                if len(f) < 4 or not f[1]:
                    continue   # skrivene mreže (bez imena) se ne prikazuju
                sig = int(f[2]) if f[2].isdigit() else 0
                cur = best.get(f[1])
                if not cur or sig > cur['sig'] or f[0] == '*':
                    best[f[1]] = {'ssid': f[1], 'sig': sig, 'lock': f[3] not in ('', '--'),
                                  'on': f[0] == '*' or bool(cur and cur['on']), 'saved': f[1] in saved}
            self.wifi['list'] = sorted(best.values(), key=lambda x: (not x['on'], -x['sig']))[:20]
        finally:
            self.wifi['scanning'] = False
            self.mark()

    async def _wifi_saved(self):
        out = await self._run('nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', timeout=5)
        return {self._nm_split(l)[0] for l in out.splitlines() if l.endswith(':802-11-wireless')}

    async def wifi_connect(self, ssid, pw):
        ssid = str(ssid or '')[:64]
        if not ssid:
            return {'err': 'Izaberi mrežu.'}
        self.wifi['busy'] = ssid
        self.mark()
        try:
            if not pw and ssid in await self._wifi_saved():
                out = await self._run('nmcli', 'con', 'up', 'id', ssid, timeout=45)
            else:
                args = ['nmcli', 'dev', 'wifi', 'connect', ssid, 'ifname', 'wlan0'] + (['password', str(pw)[:128]] if pw else [])
                out = await self._run(*args, timeout=45)
            if 'successfully' not in out:
                if pw and ssid in await self._wifi_saved():   # pogrešna lozinka: ne čuvaj neispravan profil
                    await self._run('nmcli', 'con', 'delete', 'id', ssid, timeout=10)
                msg = out.strip().splitlines()[-1] if out.strip() else ''
                return {'err': 'Nije povezano' + (': pogrešna lozinka' if 'Secrets were required' in out or 'password' in out.lower() else (': ' + msg[:100] if msg else '.'))}
            await self._run('nmcli', 'con', 'mod', 'id', ssid, 'ipv6.method', 'disabled', timeout=10)
            return {'ok': 'Povezano na ' + ssid}
        finally:
            self.wifi['busy'] = None
            await self.net_refresh()
            await self.wifi_scan()

    async def wifi_forget(self, ssid):
        await self._run('nmcli', 'con', 'delete', 'id', str(ssid), timeout=10)
        await self.net_refresh()
        await self.wifi_scan()
        return {'ok': 'Mreža %s je zaboravljena' % ssid}

    async def net_set(self, a):
        import ipaddress
        con = await self._eth_con()
        if not con:
            return {'err': 'Kablovska veza nije pronađena.'}
        if a.get('mode') == 'static':
            try:
                iface = ipaddress.IPv4Interface(str(a.get('ip', '')).strip())
                if iface.network.prefixlen == 32:
                    iface = ipaddress.IPv4Interface('%s/24' % iface.ip)
                gw = ipaddress.IPv4Address(str(a.get('gw', '')).strip())
            except ValueError:
                return {'err': 'Proveri adrese: npr. IP 172.10.10.135/24, gateway 172.10.10.1.'}
            if gw not in iface.network:
                return {'err': 'Gateway mora biti u istoj mreži kao IP adresa.'}
            args = ['ipv4.method', 'manual', 'ipv4.addresses', str(iface), 'ipv4.gateway', str(gw)]
            if not (self.net.get('dns') or {}).get('servers'):   # ručna adresa bez DNS-a: ruter je DNS
                args += ['ipv4.dns', str(gw)]
            msg = 'Statička adresa %s' % iface.ip
        else:   # DNS se podešava posebno (dns_set), ovde se ne dira
            args = ['ipv4.method', 'auto', 'ipv4.addresses', '', 'ipv4.gateway', '']
            if (self.net.get('dns') or {}).get('mode') != 'manual':
                args += ['ipv4.dns', '']
            msg = 'DHCP'
        out = await self._run('nmcli', 'con', 'mod', con, *args, timeout=10)
        if 'Error' in out:
            return {'err': 'Nije sačuvano: ' + out.strip()[:120]}

        async def reconnect():  # posle odgovora stranici, da poruka stigne pre promene adrese
            await asyncio.sleep(1.5)
            await self._run('nmcli', 'con', 'up', con, timeout=30)
            await self.net_refresh()
        asyncio.get_running_loop().create_task(reconnect())
        return {'ok': msg + ' sačuvana. Ako se stranica ne vrati, otvori http://tecopi.local'}

    async def dns_set(self, a):
        """DNS za kabl i sve Wi-Fi mreže: od rutera (DHCP) ili ručno (npr. 1.1.1.1 8.8.8.8).
        Primenjuje se bez prekida veze (nmcli device reapply)."""
        import ipaddress
        manual = a.get('mode') == 'manual'
        try:
            dns = [str(ipaddress.IPv4Address(x)) for x in re.split(r'[,\s]+', str(a.get('servers') or '')) if x][:3]
        except ValueError:
            return {'err': 'DNS adresa nije ispravna (npr. 1.1.1.1).'}
        if manual and not dns:
            return {'err': 'Upiši bar jedan DNS server.'}
        out = await self._run('nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show', timeout=5)
        cons = []
        for line in out.splitlines():
            parts = re.split(r'(?<!\\):', line)
            if len(parts) == 3 and parts[1] in ('802-3-ethernet', '802-11-wireless'):
                cons.append((parts[0].replace('\\:', ':'), parts[2]))
        if not cons:
            return {'err': 'Nema mrežnih veza za podešavanje.'}
        for con, _ in cons:
            m = await self._run('nmcli', '-g', 'ipv4.method,ipv4.gateway', 'con', 'show', con, timeout=5)
            method, _, gw = m.strip().partition(':')
            if manual:
                args = ['ipv4.ignore-auto-dns', 'yes', 'ipv4.dns', ' '.join(dns)]
            else:   # od rutera; ručna IP adresa nema DHCP, pa tada DNS = gateway
                args = ['ipv4.ignore-auto-dns', 'no', 'ipv4.dns', gw.strip() if method == 'manual' else '']
            r = await self._run('nmcli', 'con', 'mod', con, *args, timeout=10)
            if 'Error' in r:
                return {'err': 'Nije sačuvano: ' + r.strip()[:120]}
        for _, dev in cons:
            if dev:
                await self._run('nmcli', 'dev', 'reapply', dev, timeout=15)
        await self.net_refresh()
        return {'ok': 'DNS: ' + (' · '.join(dns) if manual else 'od rutera')}

    async def ntp_set(self, a):
        """NTP serveri za sat (systemd-timesyncd). Prazno = podrazumevani (debian.pool.ntp.org)."""
        srv = [x for x in re.split(r'[,\s]+', str(a.get('servers') or '')) if x][:4]
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', x) for x in srv):
            return {'err': 'NTP server nije ispravan (npr. rs.pool.ntp.org ili 172.10.10.1).'}
        conf = '[Time]\n' + ('NTP=%s\n' % ' '.join(srv) if srv else '')
        await self._run('sudo', '-n', 'mkdir', '-p', str(NTP_CONF.parent), timeout=5)
        p = await asyncio.create_subprocess_exec('sudo', '-n', 'tee', str(NTP_CONF), stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate(conf.encode())
        if p.returncode:
            return {'err': 'Nije sačuvano: ' + err.decode(errors='replace').strip()[:120]}
        await self._run('sudo', '-n', 'systemctl', 'restart', 'systemd-timesyncd', timeout=15)

        async def later():   # sinhronizacija traje par sekundi
            await asyncio.sleep(6)
            await self.net_refresh()
        asyncio.get_running_loop().create_task(later())
        return {'ok': 'NTP: ' + (' · '.join(srv) if srv else 'podrazumevani serveri') + '. Sinhronizujem sat…'}

    async def slow_loop(self):
        """Stvari koje se retko menjaju: mreža na 30 s, zvuk i Bluetooth na 10 s."""
        n = 0
        while True:
            try:
                self.remember_state()
                await self.audio_refresh()
                await self.smb_refresh()
                if n % 3 == 0:
                    await self.bt_refresh()
                    await self.net_refresh()
                    await self.cast_refresh()
                if n % 6 == 0 and not (self.tuner and self.amp and getattr(self, 'keys_addr', None)):
                    await self.hw_detect()   # tjuner/pojačalo povezani naknadno
            except Exception as e:
                log.warning('osvežavanje zvuka/mreže: %s', e)
            n += 1
            await asyncio.sleep(10)

    # ---------- jačina zvuka
    async def set_volume(self, v):
        v = max(0, min(100, int(v)))
        if self.amp_active():   # zvučnici: jačinu drži pojačalo, plejeri daju pun signal
            self.st['amp_vol'] = v
            try:
                await self._i2c(self.amp.set_volume, v)
                await self._i2c(self.amp.set_mute, False)
            except OSError as e:
                log.warning('pojačalo: %s', e)
            self.mark()
            v = 100
        await self.mpv.cmd('set_property', 'volume', v)
        await self.mpv.cmd('set_property', 'mute', False)
        for sock in HELPER_SOCKS:
            await mpv_oneshot(sock, 'set_property', 'volume', v)
            await mpv_oneshot(sock, 'set_property', 'mute', False)

    async def toggle_mute(self):
        if self.amp_active():   # pojačalo se utiša (bez šuštanja u tišini)
            try:
                await self._i2c(self.amp.set_mute, not self.amp.muted)
            except OSError as e:
                log.warning('pojačalo: %s', e)
            self.mark()
            return
        m = not bool(self.mpv.props.get('mute'))
        await self.mpv.cmd('set_property', 'mute', m)
        for sock in HELPER_SOCKS:
            await mpv_oneshot(sock, 'set_property', 'mute', m)

    # ---------- nasumično: stanica, kanal ili ceo izvor
    RAND_MEMORY = {'st': 12, 'tv': 25, 'yt': 10, 'av': 1, 'src': 2, 'all': 2, 'fm': 3}   # koliko poslednjih izbora se ne ponavlja

    def _rand(self, kind, items, key=lambda x: x, exclude=()):
        """Nasumičan izbor bez skorašnjih ponavljanja: pamti poslednje izbore po kategoriji
        i bira samo među ostalima (kad su sve skoro birane, najstariji se ponovo dozvoljavaju)."""
        import random
        rng = random.SystemRandom()
        recent = self.__dict__.setdefault('rand_recent', {}).setdefault(kind, [])
        pool = [x for x in items if key(x) not in exclude] or list(items)
        n = min(self.RAND_MEMORY.get(kind, 5), max(0, len(pool) - 1))
        fresh = [x for x in pool if key(x) not in recent[-n:]] if n else pool
        pick = rng.choice(fresh or pool)
        recent.append(key(pick))
        recent[:] = recent[-50:]
        return pick

    async def random_pick(self, what=None):
        what = what or {'st': 'st', 'tv': 'tv', 'av1': 'yt'}.get(self.source, 'av' if self.source in AVS else 'src')
        if what == 'all':   # RND na daljinskom: nasumičan izvor I nasumičan sadržaj u njemu (bez AV ulaza)
            kinds = ['st', 'tv'] + (['yt'] if any(x.get('url') for x in self.st.get('history', [])) else []) \
                + (['fm'] if self.st['fm'].get('list') else [])
            cur = {'av1': 'yt'}.get(self.source, self.source)
            what = self._rand('all', kinds, exclude=(cur,))
            if what == 'fm':
                p = self._rand('fm', self.st['fm']['list'], key=lambda v: v['f'], exclude=(self.st['fm']['f'],))
                await self.fm_tune(p['f'])
                if self.source != 'fm':
                    await self.set_source('fm')
                return {'ok': 'Nasumično: FM · ' + p['n']}
        if what == 'src':
            what = self._rand('src', [s for s in SOURCES if s not in AVS], exclude=(self.source,))   # bez AV ulaza
        elif what == 'av':   # nasumičan AV ulaz (AV1-AV3)
            what = self._rand('av', list(AVS), exclude=(self.source,))
        if what == 'yt':   # nasumičan video iz istorije gledanja
            cur = (self.meta.get(self.mpv.props.get('path') or '') or {}).get('url')
            h = [x for x in self.st.get('history', []) if x.get('url')]
            if not h or (len(h) == 1 and h[0]['url'] == cur):
                return {'err': 'Istorija gledanja je prazna.'}
            x = self._rand('yt', h, key=lambda v: v['url'], exclude=(cur,))
            try:
                await self.add_youtube(x['url'], True)
            except RuntimeError as e:
                return {'err': 'Video nije pušten: %s' % e}
            if self.source != 'av1':
                await self.set_source('av1')
            return {'ok': 'Nasumično: ' + x['t']}
        if what == 'st':
            lst = self.stream_all()
            u = self._rand('st', [x['u'] for x in lst], exclude=(self.st['stream'].get('u'),))
            pick = next(i for i, x in enumerate(lst) if x['u'] == u)
            await self.stream_play(pick)
            return {'ok': 'Nasumično: Radio · ' + lst[pick]['n']}
        if what == 'tv':
            lst = self.tv_all()
            x = self._rand('tv', lst, key=lambda v: v['u'], exclude=(self.st['tv'].get('u'),))
            await self.tv_play_url(x['u'])
            return {'ok': 'Nasumično: TV · ' + x['n']}
        await self.set_source(what)
        return {'ok': 'Nasumično: ' + {'av1': 'YT', 'fm': 'FM radio'}.get(what, AVL.get(what, what))}

    # ---------- Stand by: sve staje, na ekranu slike ili sat (YouTube lista se pamti za dugme YT)
    async def standby(self):
        if self.mode == 'yt':
            self.yt_snap = self.yt_snapshot() or self.yt_snap
            self.mode = 'idle'
            await self.mpv.cmd('stop')
        snap, self.yt_snap = self.yt_snap, None   # da se YouTube sada ne vrati sam
        if self.source != 'av1':
            await self.set_source('av1')   # zaustavlja radio/TV, AV releji isključeni
        else:
            await self.stop_slides()
            await self.start_slides()      # slike ili sat, kako je podešeno
        self.yt_snap = snap
        self.mark()
        return {'ok': 'Stand by'}

    # ---------- pravi daljinski (TSOP na GPIO17)
    IR_REPEAT = {'volup', 'voldn', 'fwd', 'back'}   # ove komande se ponavljaju dok je taster držan

    async def push(self, msg):
        """Poruka svim otvorenim stranicama (npr. signal sa daljinskog)."""
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    async def ir_loop(self):
        async for code, rep in hw.ir_codes():
            try:
                await self.ir_hw(code, rep)
            except Exception as e:
                log.warning('IR: %s', e)

    async def ir_hw(self, code, rep):
        m = next((x for x in self.st['ir'] if x['code'] == code), None)
        learning = self.ir_learn_until > time.time()
        now = time.monotonic()
        if rep:   # držan taster: samo jačina/premotavanje, posle pola sekunde, na 0,2 s
            if learning or not m or m['cmd'] not in self.IR_REPEAT or now - self._ir_first < 0.5 or now - self._ir_rep < 0.2:
                return
            self._ir_rep = now
        else:
            self._ir_first = self._ir_rep = now
            await self.push({'irsig': {'code': code, 'key': m and m['key'], 'cmd': m and m['cmd'], 'learn': learning}})
        if m and not learning:
            await self.run_cmd(m['cmd'])

    # ---------- komande (web i daljinski)
    async def run_cmd(self, cmd):
        """Izvršava komandu sa daljinskog (ista imena kao u mapiranju tastera)."""
        fm = self.source == 'fm'
        stm = self.source == 'st'
        vol = self.cur_vol()
        if self.source in AVS and cmd in ('play', 'stop', 'next', 'prev', 'fwd', 'back'):
            return   # na AV ulazu nema plejera (inače bi se pustio YouTube ispod konzole)
        if cmd == 'play':
            await (self.toggle_mute() if fm or stm else self.mpv.cmd('cycle', 'pause'))
        elif cmd == 'stop':   # pauza i povratak na početak; lista ostaje (mpv 'stop' bi je obrisao)
            if fm or stm:
                await self.mpv.cmd('set_property', 'mute', True)
                for sock in HELPER_SOCKS:
                    await mpv_oneshot(sock, 'set_property', 'mute', True)
            elif self.mode == 'yt':
                await self.mpv.cmd('set_property', 'pause', True)
                await self.mpv.cmd('seek', 0, 'absolute')
        elif cmd in ('next', 'prev') and stm:
            await self.stream_step(1 if cmd == 'next' else -1)
        elif cmd in ('next', 'prev') and self.source == 'tv':
            await self.tv_step(1 if cmd == 'next' else -1)
        elif cmd in ('fwd', 'back') and (stm or self.source == 'tv'):
            pass
        elif cmd == 'next':
            await (self.fm_seek(1) if fm else self.mpv.cmd('playlist-next', 'force'))
        elif cmd == 'prev':
            await (self.fm_seek(-1) if fm else self.mpv.cmd('playlist-prev', 'force'))
        elif cmd == 'fwd':
            await (self.fm_tune(self.st['fm']['f'] + 0.1) if fm else self.mpv.cmd('seek', 10, 'relative'))
        elif cmd == 'back':
            await (self.fm_tune(self.st['fm']['f'] - 0.1) if fm else self.mpv.cmd('seek', -10, 'relative'))
        elif cmd == 'volup':
            await self.set_volume(vol + 5)
        elif cmd == 'voldn':
            await self.set_volume(vol - 5)
        elif cmd == 'mute':
            await self.toggle_mute()
        elif cmd == 'random':   # RND taster: bilo koji izvor i sadržaj
            await self.random_pick('all')
        elif cmd == 'standby':
            await self.standby()
        elif cmd in SOURCES:
            await self.set_source(cmd)
        elif re.fullmatch(r'r\d+', cmd or ''):
            i = int(cmd[1:])
            if i < len(self.st['relays']):
                self.relay_set(i, not self.st['relays'][i]['on'])
        self.mark()

    async def handle(self, a, admin):
        """Obrada poruke sa stranice. Vraća dict koji ide nazad samo tom klijentu."""
        c = a.get('c')
        if c not in ('ping', 'check', 'vol', 'seek', 'seek_rel'):   # zapis komandi sa stranice (bez PIN-a i tokena)
            log.info('stranica: %s %s', c, {k: v for k, v in a.items()
                                             if k not in ('c', 'rid', 'tok', 'pin', 'pw') and 'pass' not in k and 'pw' not in k} or '')
        if c in ADMIN_CMDS and not admin and not (c == 'set_pin' and not self.st['pin']):
            return {'err': 'Potreban je Admin režim.'}
        if c == 'play':
            await self.mpv.cmd('cycle', 'pause')
        elif c == 'rc':   # taster kao na daljinskom (zavisi od izvora): mini plejer na dnu stranice
            k = str(a.get('k'))
            if k in ('play', 'next', 'prev', 'volup', 'voldn', 'mute'):
                await self.run_cmd(k)
        elif c == 'next':
            await self.mpv.cmd('playlist-next', 'force')
        elif c == 'prev':
            if (self.mpv.props.get('time-pos') or 0) > 5:
                await self.mpv.cmd('seek', 0, 'absolute')
            else:
                await self.mpv.cmd('playlist-prev', 'force')
        elif c == 'seek':
            await self.mpv.cmd('seek', float(a['pos']), 'absolute')
        elif c == 'seek_rel':
            await self.mpv.cmd('seek', float(a['s']), 'relative')
        elif c == 'vol':
            await self.set_volume(a['v'])
        elif c == 'mute':
            await self.toggle_mute()
        elif c == 'source':
            await self.set_source(a.get('s'))
        elif c == 'add_yt':
            url = str(a.get('url', '')).strip()
            if not re.search(r'(youtube\.com|youtu\.be)/', url):
                return {'err': 'To nije YouTube link.'}
            now = bool(a.get('now'))
            try:
                title = await self.add_youtube(url, now)
            except RuntimeError as e:
                return {'err': 'Video nije dodat: %s' % e}
            return {'ok': ('Pušta se: ' if now else 'Dodato: ') + title}
        elif c == 'ev_set':
            ev = self.st['events'].get(a.get('id'))
            if not ev:
                return {'err': 'Nepoznat događaj.'}
            if 'n' in a and a['id'] not in ALARMS:   # alarmi (požar, voda) imaju stalan naziv i zvuk
                ev['n'] = str(a['n']).strip()[:30] or DEFAULT_EVENTS.get(a['id'], {}).get('n', 'Događaj')
            if 'on' in a:
                ev['on'] = bool(a['on'])
            if 'sec' in a:
                ev['sec'] = max(1, min(120, int(a['sec'])))
            if 'pause' in a:
                ev['pause'] = bool(a['pause'])
            save_state(self.st)
            await self.events_assets()
        elif c == 'ev_test':
            return await self.fire_event(a.get('id'))
        elif c == 'ev_sound_del':
            snd = self.event_sound(str(a.get('id')))
            if snd and a.get('id') not in ALARMS:
                snd.unlink()
        elif c == 'net_set':
            return await self.net_set(a)
        elif c == 'dns_set':
            return await self.dns_set(a)
        elif c == 'alarm_cfg':
            return await self.alarm_cfg_set(a)
        elif c == 'alarm':   # uključi/isključi: dozvoljeno svima u mreži (izbor korisnika)
            return await self.alarm_do(a)
        elif c in ('alarm_monitor', 'alarm_mem_clear', 'alarm_ev_clear'):
            return await self.alarm_extra(c, a)
        elif c == 'ntp_set':
            return await self.ntp_set(a)
        elif c == 'wifi_scan':
            await self.wifi_scan()
        elif c == 'wifi_connect':
            return await self.wifi_connect(a.get('ssid'), a.get('pw'))
        elif c == 'wifi_forget':
            return await self.wifi_forget(a.get('ssid'))
        elif c == 'cast':
            await self.cast_set(bool(a.get('v')))
            return {'ok': 'Cast je uključen' if a.get('v') else 'Cast je isključen'}
        elif c in ('smb', 'smb_usb'):
            return await self.smb_set(c, bool(a.get('v')))
        elif c == 'smb_install':
            return await self.smb_install()
        elif c == 'smb_eject':
            return await self.usb_eject(str(a.get('dev') or ''))
        elif c == 'smb_pw':
            return await self.smb_password(a.get('pw'))
        elif c == 'smb_ls':
            return await self.smb_ls(a)
        elif c == 'smb_dir':
            return await self.smb_dir(a)
        elif c == 'smb_opl':
            return await self.smb_opl(a)
        elif c == 'game_del':
            return await self.game_del(a)
        elif c == 'reboot':
            save_state(self.st)
            asyncio.get_running_loop().call_later(1.5, lambda: subprocess.Popen(['systemctl', 'reboot']))
            return {'ok': 'Teco.Pi se restartuje, vraća se za oko 30 s.'}
        elif c == 'pin_req':
            self.st['pin_required'] = bool(a.get('v'))
            save_state(self.st)
        elif c == 'audio_out':
            await self.set_output(a['id'])
        elif c.startswith('bt_'):
            return await self.bt_cmd(c, a.get('mac'))
        elif c == 'hist_play':
            h = self.st.get('history', [])
            i = int(a['i'])
            if not 0 <= i < len(h):
                return {'err': 'Video nije u istoriji.'}
            try:
                title = await self.add_youtube(h[i]['url'], True)
            except RuntimeError as e:
                return {'err': 'Video nije pušten: %s' % e}
            return {'ok': 'Pušta se: ' + title}
        elif c == 'hist_clear':
            self.st['history'] = []
            save_state(self.st)
        elif c == 'hist_del':
            h = self.st.get('history', [])
            i = int(a['i'])
            if 0 <= i < len(h):
                h.pop(i)
                save_state(self.st)
        elif c == 'q_play':
            await self.mpv.cmd('set_property', 'playlist-pos', int(a['i']))
        elif c == 'q_move':
            i, d = int(a['i']), int(a['d'])
            n = len(self.mpv.props.get('playlist') or [])
            j = i + d
            if 0 <= j < n:
                await self.mpv.cmd('playlist-move', i, j + 1 if d > 0 else j)
        elif c == 'q_del':
            await self.mpv.cmd('playlist-remove', int(a['i']))
        elif c == 'q_clear':   # skloni sve što čeka posle trenutnog videa
            pl = self.mpv.props.get('playlist') or []
            cur = next((i for i, e in enumerate(pl) if e.get('current')), -1)
            for i in range(len(pl) - 1, cur, -1):
                await self.mpv.cmd('playlist-remove', i)
        elif c == 'tv_play':
            if a.get('u'):
                await self.tv_play_url(str(a['u']))
            else:
                await self.tv_step(int(a.get('d', 0)))
        elif c == 'tv_fav':   # omiljeni kanal: prečica na vrhu liste (može svako)
            u, fav = str(a.get('u') or ''), self.st['tv'].setdefault('fav', [])
            if u in fav:
                fav.remove(u)
            elif any(x['u'] == u for x in self.tv_all()):
                fav.append(u)
            save_state(self.st)
        elif c == 'tv_add':
            u = str(a.get('u') or '').strip()
            if not re.match(r'https?://\S+$', u):
                return {'err': 'Link mora počinjati sa http:// ili https://'}
            lst = self.st['tv']['list']
            n = str(a.get('n') or '').strip()[:26] or 'Kanal %d' % (len(lst) + 1)
            lst.append({'n': n, 'u': u[:500]})
            self.st['tv'].setdefault('fav', []).append(u[:500])
            save_state(self.st)
            await self.tv_play_url(u[:500])
        elif c == 'tv_del':   # briše moj kanal (po linku)
            u, t = str(a.get('u') or ''), self.st['tv']
            if any(x['u'] == u for x in t['list']):
                t['list'] = [x for x in t['list'] if x['u'] != u]
                t['fav'] = [x for x in t.get('fav', []) if x != u]
                save_state(self.st)
                if u == t.get('u') and self.source == 'tv':
                    await self.tv_start()
        elif c == 'theme':   # izgled stranice za sve uređaje: auto (kao telefon/računar), svetla, tamna
            v = str(a.get('v'))
            if v in ('auto', 'light', 'dark'):
                self.st['theme'] = v
                save_state(self.st)
        elif c == 'boot':
            v = str(a.get('v'))
            if v == 'last' or v in SOURCES:
                self.st['boot'] = v
                save_state(self.st)
        elif c == 'ir_learn':   # admin snima taster: 15 s daljinski samo javlja kod
            self.ir_learn_until = time.time() + 15 if a.get('on') else 0
        elif c == 'ir_press':
            m = next((x for x in self.st['ir'] if x['code'] == a.get('code')), None)
            if not m:
                return {'ir': {'code': a.get('code'), 'cmd': None}}
            await self.run_cmd(m['cmd'])
            return {'ir': {'code': m['code'], 'key': m['key'], 'cmd': m['cmd']}}
        elif c == 'ir_save':
            m = {'key': str(a['key'])[:20], 'code': str(a['code'])[:40], 'cmd': str(a['cmd'])[:10]}
            self.st['ir'] = [x for x in self.st['ir'] if x['code'] != m['code'] and x['code'] != a.get('old')]
            self.st['ir'].append(m)
            save_state(self.st)
        elif c == 'ir_del':
            self.st['ir'] = [x for x in self.st['ir'] if x['code'] != a.get('code')]
            save_state(self.st)
        elif c == 'relay':
            err = self.relay_set(int(a['i']), bool(a['on']))
            if err:
                self.mark()
                return {'err': err}
        elif c in ('cons', 'name'):
            if a.get('s') in AVS:
                if c == 'cons':
                    self.st['cons'][a['s']] = str(a.get('v', 'other'))[:10]
                else:
                    self.st['names'][a['s']] = str(a.get('v', ''))[:24]
                save_state(self.st)
                await self.show_console(a['s'], show=self.source == a['s'])   # nova slika odmah (ili spremna za kasnije)
                self._ovl_prune_keep()
        elif c == 'fm_tune':
            await self.fm_tune(a['f'])
        elif c == 'fm_seek':
            await self.fm_seek(int(a['d']))
        elif c == 'fm_save':
            f = self.st['fm']['f']
            n = str(a.get('n') or '').strip()[:20] or 'Stanica %d' % (len(self.st['fm']['list']) + 1)
            ex = next((p for p in self.st['fm']['list'] if abs(p['f'] - f) < 0.05), None)
            if ex:
                ex['n'] = n
            else:
                self.st['fm']['list'].append({'n': n, 'f': f})
            save_state(self.st)
        elif c == 'fm_del':
            self.st['fm']['list'] = [p for p in self.st['fm']['list'] if abs(p['f'] - float(a['f'])) >= 0.05]
            save_state(self.st)
        elif c == 'radio_clock':
            v = int(a.get('v') or 0)
            self.st['radio_clock'] = v if v in (0, 15, 30, 45, 60) else 15   # sekundi (0 = nikad)
            save_state(self.st)
        elif c == 'idle':
            self.st['idle'] = 'clock' if a.get('v') == 'clock' else 'slides'
            save_state(self.st)
            await self.refresh_slides()
        elif c == 'slide_sec':
            self.st['slide_sec'] = max(3, min(600, int(a['v'])))
            save_state(self.st)
            if self.mode == 'slides':
                await self.mpv.cmd('set_property', 'image-display-duration', self.st['slide_sec'])
        elif c == 'img_del':
            name = os.path.basename(str(a.get('n', '')))
            try:
                (IMAGES / name).unlink()
            except OSError:
                return {'err': 'Slika nije pronađena.'}
            try:
                (IMAGES / 'orig' / name).unlink()
            except OSError:
                pass
            await self.refresh_slides()
        elif c == 'st_play':
            if 'd' in a:
                await self.stream_step(int(a['d']))
            elif a.get('u'):
                i = next((k for k, x in enumerate(self.stream_all()) if x['u'] == a['u']), None)
                if i is not None:
                    await self.stream_play(i)
            else:
                await self.stream_play(a.get('i', 0))
        elif c == 'yt_cookies_del':
            try:
                YT_COOKIES.unlink()
            except OSError:
                pass
        elif c == 'standby':
            return await self.standby()
        elif c == 'random':   # nasumična stanica, kanal ili izvor (what: st / tv / src)
            return await self.random_pick(a.get('what'))
        elif c == 'st_fav':   # omiljena stanica (može svako)
            u, fav = str(a.get('u') or ''), self.st['stream'].setdefault('fav', [])
            if u in fav:
                fav.remove(u)
            elif any(x['u'] == u for x in SYS_STREAMS):
                fav.append(u)
            save_state(self.st)
            if self.source == 'st':
                await self.show_stream()
        elif c == 'st_add':
            u = str(a.get('u') or '').strip()
            if not re.match(r'https?://\S+$', u):
                return {'err': 'Link mora počinjati sa http:// ili https://'}
            lst = self.st['stream']['list']
            n = str(a.get('n') or '').strip()[:26] or 'Stanica %d' % (len(lst) + 1)
            lst.append({'n': n, 'u': u[:500]})
            save_state(self.st)
            await self.stream_play(len(SYS_STREAMS) + len(lst) - 1)
        elif c == 'st_del':   # i = redni broj u mojoj listi
            lst, i = self.st['stream']['list'], int(a.get('i', -1))
            if 0 <= i < len(lst):
                was_cur = lst.pop(i)['u'] == self.st['stream'].get('u')
                save_state(self.st)
                if was_cur and self.source == 'st':
                    await self.stream_start()
        if c in ('fm_save', 'fm_del') and self.source == 'fm':
            await self.show_fm()
        if c == 'st_del' and self.source == 'st':
            await self.show_stream()
        self.mark()
        return {}

    # ---------- PIN / admin
    def auth(self, pin):
        p = self.st['pin']
        if p and not self.st.get('pin_required', True):   # PIN isključen u podešavanjima
            tok = secrets.token_hex(16)
            self.tokens.add(tok)
            return tok
        if not p or not re.fullmatch(r'\d{4,8}', str(pin)):
            return None
        if secrets.compare_digest(pin_hash(str(pin), p['salt']), p['hash']):
            tok = secrets.token_hex(16)
            self.tokens.add(tok)
            return tok
        return None

    def set_pin(self, pin):
        if not re.fullmatch(r'\d{4,8}', str(pin)):
            return None
        salt = secrets.token_hex(16)
        self.st['pin'] = {'salt': salt, 'hash': pin_hash(str(pin), salt)}
        save_state(self.st)
        self.tokens.clear()
        tok = secrets.token_hex(16)
        self.tokens.add(tok)
        return tok

    # ---------- sistem
    async def inet_loop(self):
        """Internet (ne samo lokalna mreža): TCP veza ka javnim serverima + DNS.
        'ok' = radi, 'dns' = ima veze ali imena ne rade, 'off' = nema interneta. Češće proverava dok nema veze."""
        async def tcp(host, port):
            t0 = time.monotonic()
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 4)
            w.close()
            return round((time.monotonic() - t0) * 1000)
        while True:
            ms = None
            for host, port in (('1.1.1.1', 443), ('8.8.8.8', 53), ('9.9.9.9', 443)):
                try:
                    ms = await tcp(host, port)
                    break
                except (OSError, asyncio.TimeoutError):
                    pass
            st = 'off'
            if ms is not None:
                try:
                    await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo('www.youtube.com', 443), 5)
                    st = 'ok'
                except (OSError, asyncio.TimeoutError):
                    st = 'dns'
            if getattr(self, 'inet', (None,))[0] not in (None, st):
                log.warning('internet: %s', st)
            self.inet = (st, ms)
            await asyncio.sleep(30 if st == 'ok' else 8)

    async def sys_loop(self):
        while True:
            s = {}
            try:
                s['cpu'] = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
            except (OSError, ValueError):
                s['cpu'] = None
            # unutra: DHT22 (sa vlažnošću) ili DS18B20 na 1-Wire; napolju: vrednost poslata preko /api/outside
            temps = []
            devs = [d for fam in ('10', '22', '28', '3b', '42') for d in glob.glob('/sys/bus/w1/devices/%s-*/temperature' % fam)]
            for dev in sorted(devs):
                try:
                    temps.append(round(int(Path(dev).read_text()) / 1000, 1))
                except (OSError, ValueError):
                    temps.append(None)
            # DHT22 preko kernel drajvera (dtoverlay=dht11): temperatura + vlažnost; čitanje ponekad ne uspe,
            # pa se zadnja dobra vrednost drži do 60 s
            dht_t = dht_h = None
            for dev in glob.glob('/sys/bus/iio/devices/iio:device*'):
                try:
                    if Path(dev, 'name').read_text().strip().startswith('dht'):
                        dht_t = round(int(Path(dev, 'in_temp_input').read_text()) / 1000, 1)
                        dht_h = round(int(Path(dev, 'in_humidityrelative_input').read_text()) / 1000)
                        self.dht_last = (dht_t, dht_h, time.monotonic())
                except (OSError, ValueError):
                    pass
            if dht_t is None and self.dht_last and time.monotonic() - self.dht_last[2] < 60:
                dht_t, dht_h = self.dht_last[0], self.dht_last[1]
            s['room'] = dht_t if dht_t is not None else (temps[0] if temps else None)
            ext = getattr(self, 'outside', None)   # (temperatura, vlažnost, vreme) sa ESP-a ili drugog uređaja
            s['out'] = ext[0] if ext and time.time() - ext[2] < 1800 else None   # starije od 30 min se ne prikazuje
            s['out_hum'] = ext[1] if s['out'] is not None else None
            s['hum'] = dht_h
            try:
                p = await asyncio.create_subprocess_exec('vcgencmd', 'get_throttled', stdout=asyncio.subprocess.PIPE)
                out, _ = await p.communicate()
                thr = int(out.decode().strip().split('=')[1], 16)
                s['uv_now'], s['uv_seen'] = bool(thr & 0x1), bool(thr & 0x10000)
            except (OSError, ValueError, IndexError):
                s['uv_now'] = s['uv_seen'] = None
            try:
                s['uptime'] = int(float(Path('/proc/uptime').read_text().split()[0]))
            except (OSError, ValueError):
                s['uptime'] = 0
            du = shutil.disk_usage('/')
            s['disk_free'] = round(du.free / 2**30, 1)
            try:
                mi = dict(l.split(':', 1) for l in Path('/proc/meminfo').read_text().splitlines())
                avail, total = int(mi['MemAvailable'].split()[0]), int(mi['MemTotal'].split()[0])
                s['mem_free'] = avail // 1024
                s['ram'] = round(100 * (total - avail) / total)
            except (OSError, KeyError, ValueError, ZeroDivisionError):
                s['mem_free'] = s['ram'] = None
            # opterećenje procesora: razlika /proc/stat između dva čitanja (5 s), bez dodatnih programa
            try:
                v = [int(x) for x in Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]]
                idle, total = v[3] + v[4], sum(v[:8])
                if self.cpu_prev:
                    dt = total - self.cpu_prev[1]
                    s['load'] = round(100 * (1 - (idle - self.cpu_prev[0]) / dt)) if dt > 0 else None
                else:
                    s['load'] = None
                self.cpu_prev = (idle, total)
            except (OSError, ValueError, IndexError):
                s['load'] = None
            if s['cpu'] is not None:
                self.cpu_hist = (self.cpu_hist + [s['cpu']])[-24:]
            if s['room'] is not None:
                self.room_hist = (self.room_hist + [s['room']])[-24:]
            if s['out'] is not None:
                self.out_hist = (self.out_hist + [s['out']])[-24:]
            s['cpu_hist'], s['room_hist'], s['out_hist'] = self.cpu_hist, self.room_hist, self.out_hist
            s['inet'], s['inet_ms'] = getattr(self, 'inet', (None, None))
            self.sys = s
            self.mark()
            await asyncio.sleep(5)

    # ---------- stanje za stranicu
    def snapshot(self):
        p = self.mpv.props
        cur_url = (self.meta.get(p.get('path') or '') or {}).get('url')
        return {
            'mpv': self.mpv.connected,
            'mode': self.mode,
            'source': self.source,
            'title': p.get('media-title') if self.mode == 'yt' else None,
            'pos': p.get('time-pos') or 0,
            'dur': p.get('duration') or 0,
            'paused': bool(p.get('pause')),
            'vol': self.cur_vol(),
            'mute': self.cur_mute(),
            'queue': self.queue(),
            'history': [{'t': x['t'], 'd': x.get('d', 0), 'now': self.mode == 'yt' and x['url'] == cur_url, 'src': x.get('src', 'link')}
                        for x in self.st.get('history', [])],
            'pending': [{'id': e['id'], 't': e['t'], 'err': e['err']} for e in self.pending],
            'names': self.st['names'],
            'cons': self.st['cons'],
            'fm': {**self.st['fm'], 'on': self.source == 'fm' and (self.fm_proc is not None or self.fm_task is not None), 'err': self.fm_err,
                   'hw': 'rda5807' if self.tuner else 'rtl',
                   **({'rssi': self.tuner.rssi, 'st': self.tuner.stereo, 'ps': self.tuner.ps, 'rt': self.tuner.rt}
                      if self.tuner and self.fm_task else {})},
            'amp': bool(self.amp),
            'tv': {'all': self.tv_all(), 'fav': self.st['tv'].get('fav') or [], 'u': (self.tv_cur() or (0, {}))[1].get('u'), 'err': self.tv_err,
                   'on': self.source == 'tv' and self.mode == 'tv', 'loading': self.tv_loading},
            'boot': self.st.get('boot', 'last'),
            'theme': self.st.get('theme', 'auto'),
            'alarm': self.alarm_snapshot(),   # (i kao lista 'alarms' na stranici: jedan za sada)
            'last_av': self.st.get('last_av', 'av2'),
            'radio_clock': int(self.st.get('radio_clock', 15) or 0),
            'yt_cookies': self.yt_cookie_state(),
            'wifi': self.wifi,
            'stream': {'sys': SYS_STREAMS, 'my': self.st['stream']['list'], 'fav': self.st['stream'].get('fav') or [],
                       'cur': (self.stream_cur() or (0,))[0], 'err': self.st_err,
                       'on': self.source == 'st' and self.st_proc is not None, 'title': self.st_title},
            'relays': [{'n': r['n'], 'pin': r['pin'], 'on': r['on'], 'role': r.get('role')} for r in self.st['relays']],
            'io': [   # stanje uređaja (I/O prozor)
                {'n': 'IR prijemnik', 'd': 'TSOP · GPIO17', 'ok': os.path.exists('/dev/lirc0')},
                {'n': 'FM tjuner', 'd': 'RDA5807M · I2C 0x11', 'ok': bool(self.tuner)},
                {'n': 'Pojačalo', 'd': 'TPA2016D2 · I2C 0x58', 'ok': bool(self.amp)},
                {'n': 'Tasteri', 'd': 'PCF8574 · I2C 0x27', 'ok': bool(getattr(self, 'keys_addr', None))},
                {'n': 'Unutra', 'd': 'DHT22 · GPIO4 ili DS18B20 · 1-Wire', 'ok': self.sys.get('room') is not None},
                {'n': 'Napolju', 'd': 'preko API-ja (/api/outside)', 'ok': self.sys.get('out') is not None},
            ],
            'ir': self.st['ir'],
            'slide_sec': self.st['slide_sec'],
            'idle': self.st.get('idle', 'slides'),
            'audio': self.audio,
            'bt': self.bt,
            'net': self.net,
            'images': self.images(),
            'pin_set': bool(self.st['pin']),
            'pin_required': self.st.get('pin_required', True),
            'events': self.events_snapshot(),
            'ev_now': self.ev_now,
            'cast': {'on': bool(self.st.get('cast')), 'run': bool(getattr(self, 'cast_run', False)),
                     'sender': self.cast_sender},
            'smb': self.smb_snapshot(),
            'sys': self.sys,
        }

    async def broadcast_loop(self):
        last = 0
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), 1)
            except asyncio.TimeoutError:
                pass
            wait = 0.15 - (time.monotonic() - last)  # najviše ~6 slanja u sekundi
            if wait > 0:
                await asyncio.sleep(wait)
            self.dirty.clear()
            last = time.monotonic()
            if not self.clients:
                continue
            msg = json.dumps({'state': self.snapshot()}, ensure_ascii=False)
            for ws in list(self.clients):
                try:
                    await ws.send_str(msg)
                except (ConnectionError, RuntimeError):
                    self.clients.discard(ws)


# ---------------------------------------------------------------- HTTP rute

def make_app():
    teco = Teco()
    app = web.Application(client_max_size=25 * 2**20)

    async def index(request):
        return web.FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        teco.clients.add(ws)
        await ws.send_str(json.dumps({'state': teco.snapshot()}, ensure_ascii=False))
        tasks = set()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    a = json.loads(msg.data)
                except ValueError:
                    continue
                # svaka poruka u svom zadatku, da spora komanda (YouTube) ne blokira ostale
                t = asyncio.create_task(process(ws, a))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        finally:
            teco.clients.discard(ws)
        return ws

    async def process(ws, a):
        rid = a.get('rid')
        c = a.get('c')
        if c == 'auth':
            tok = teco.auth(a.get('pin'))
            if not tok:
                await asyncio.sleep(1)  # uspori pogađanje PIN-a
            reply = {'tok': tok} if tok else {'err': 'Pogrešan PIN.'}
        elif c == 'set_pin':
            allowed = not teco.st['pin'] or a.get('tok') in teco.tokens
            tok = teco.set_pin(a.get('pin')) if allowed else None
            reply = {'tok': tok} if tok else {'err': 'PIN mora imati 4 do 8 cifara.' if allowed else 'Potreban je Admin režim.'}
            teco.mark()
        elif c == 'check':
            reply = {'valid': a.get('tok') in teco.tokens}
        elif c == 'ping':   # stranica proverava da je veza živa
            reply = {'pong': True}
        else:
            try:
                reply = await teco.handle(a, a.get('tok') in teco.tokens)
            except (KeyError, ValueError, TypeError) as e:
                reply = {'err': 'Neispravna komanda: %s' % e}
        if rid is not None and not ws.closed:
            reply['rid'] = rid
            try:
                await ws.send_str(json.dumps(reply, ensure_ascii=False))
            except (ConnectionError, RuntimeError):
                pass

    async def _send_link(url, t=0):
        """Zajedničko za dugme u pregledaču (/send) i iPhone prečicu (/api/play); t = odakle da nastavi."""
        url = (url or '').strip()
        if not re.search(r'(youtube\.com|youtu\.be)/', url):
            return False, 'To nije YouTube link.'
        if teco.source != 'av1':
            await teco.set_source('av1', teco._hist_title(url) or 'YouTube')
        try:
            title = await teco.add_youtube(url, True, t)
        except RuntimeError as e:
            return False, 'Video nije pušten: %s' % e
        return True, title

    async def send_page(request):
        """Stranica koju otvara dugme „Pošalji na Teco.Pi“ iz Firefox-a: link se obrađuje u pozadini,
        a prozor se odmah zatvara (napredak se vidi na glavnoj stranici)."""
        url = request.query.get('url', '')
        ok = bool(re.search(r'(youtube\.com|youtu\.be)/', url))
        if ok:
            try:
                t = max(0.0, float(request.query.get('t') or 0))
            except ValueError:
                t = 0.0
            asyncio.get_running_loop().create_task(_send_link(url, t))
        html = f'''<!doctype html><html lang="sr"><head><meta charset="utf-8"><title>Teco.Pi</title>
<style>body{{margin:0;font:15px system-ui,sans-serif;background:#0b1018;color:#e8eef6;display:grid;place-items:center;min-height:100vh}}</style></head>
<body>{'Poslato na Teco.Pi' if ok else 'To nije YouTube link.'}<script>{'window.close()' if ok else 'setTimeout(()=>window.close(),2500)'}</script></body></html>'''
        return web.Response(text=html, content_type='text/html')

    async def api_play(request):
        """Za iPhone prečicu (Shortcuts) i slične alate: /api/play?url=... ili POST sa telom = link."""
        url = request.query.get('url') or (await request.text() if request.method == 'POST' else '')
        ok, msg = await _send_link(url)
        return web.json_response({'ok': ok, 'msg': msg}, status=200 if ok else 400)

    async def cast_api(request):
        if request.remote not in ('127.0.0.1', '::1'):
            return web.json_response({'err': 'Samo za cast program na Teco.Pi.'}, status=403)
        try:
            a = await request.json()
            return web.json_response(await teco.cast_api(a))
        except (ValueError, TypeError, RuntimeError) as e:
            return web.json_response({'ok': False, 'err': str(e)})

    async def event_sound_upload(request):
        """Zvuk za događaj: /api/event-sound?id=zvono (zamenjuje postojeći zvuk tog događaja)."""
        if request.headers.get('X-Teco-Token') not in teco.tokens:
            return web.json_response({'err': 'Potreban je Admin režim.'}, status=403)
        eid = request.query.get('id', '')
        if eid not in teco.st['events']:
            return web.json_response({'err': 'Nepoznat događaj.'}, status=400)
        if eid in ALARMS:
            return web.json_response({'err': 'Alarm ima ugrađen zvuk.'}, status=400)
        reader = await request.multipart()
        part = await reader.next()
        ext = Path(part.filename or '').suffix.lower()
        if ext not in AUDIO_EXT:
            return web.json_response({'err': 'Podržani su mp3, wav, ogg, m4a, flac.'}, status=400)
        d = EVENTS_DIR / eid
        d.mkdir(parents=True, exist_ok=True)
        old = teco.event_sound(eid)
        if old:
            old.unlink()
        (d / ('zvuk' + ext)).write_bytes(await part.read(decode=False))
        teco.mark()
        return web.json_response({'ok': 'Zvuk je sačuvan'})

    async def upload(request):
        if request.headers.get('X-Teco-Token') not in teco.tokens:
            return web.json_response({'err': 'Potreban je Admin režim.'}, status=403)
        from PIL import Image, ImageOps
        IMAGES.mkdir(parents=True, exist_ok=True)
        reader = await request.multipart()
        saved = []
        async for part in reader:
            if not part.filename:
                continue
            raw = await part.read(decode=False)
            stem = re.sub(r'[^A-Za-z0-9._-]', '_', Path(part.filename).stem)[:40] or 'slika'
            dest = IMAGES / (stem + '.jpg')
            n = 1
            while dest.exists():
                dest = IMAGES / ('%s-%d.jpg' % (stem, n))
                n += 1

            def convert(raw=raw, dest=dest):
                import io
                im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
                orig = IMAGES / 'orig' / dest.name
                orig.parent.mkdir(exist_ok=True)
                o = im.copy()
                o.thumbnail((1920, 1920), Image.LANCZOS)
                o.save(orig, 'JPEG', quality=90)
                slide_image(im).save(dest, 'JPEG', quality=88)
            try:
                await asyncio.get_running_loop().run_in_executor(None, convert)
                saved.append(dest.name)
            except Exception as e:
                log.warning('slika %s nije sačuvana: %s', part.filename, e)
        await teco.refresh_slides()
        teco.mark()
        return web.json_response({'saved': saved})

    async def game_upload(request):
        """Igrica (ISO/ZSO/VCD) direktno u deljeni folder: POST /api/game?share=Teco&name=igra.iso, telo = fajl.
        Piše se u delovima (bez ograničenja od 25 MB) u .part fajl, pa se preimenuje kad stigne ceo."""
        if request.headers.get('X-Teco-Token') not in teco.tokens:
            return web.json_response({'err': 'Potreban je Admin režim.'}, status=403)
        base, sh = teco.share_base(request.query.get('share', ''))
        if not base:
            return web.json_response({'err': 'Deljenje nije uključeno ili disk nije priključen.'}, status=404)
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', Path(request.query.get('name', '')).name).strip(' .')[:150]
        ext = Path(name).suffix.lower()
        size = request.content_length
        if ext not in GAME_EXT:
            return web.json_response({'err': 'Podržani su .iso, .zso (PS2) i .vcd (PS1/POPS).'}, status=400)
        if not size:
            return web.json_response({'err': 'Nepoznata veličina fajla.'}, status=411)
        if sh.get('fs') == 'vfat' and size >= 4 * 2**30:
            return web.json_response({'err': 'FAT32 ne prima fajl od 4 GB i više. Formatiraj disk kao exFAT.'}, status=400)
        if shutil.disk_usage(base).free < size + 32 * 2**20:
            return web.json_response({'err': 'Nema dovoljno mesta na %s.' % sh['n']}, status=507)
        folder = GAME_EXT[ext] or ('CD' if size <= CD_MAX else 'DVD')
        dest = base / folder / name
        if dest.exists():
            return web.json_response({'err': '%s/%s već postoji.' % (folder, name)}, status=409)
        dest.parent.mkdir(exist_ok=True)
        tmp = dest.with_name('.' + name + '.part')
        loop = asyncio.get_running_loop()
        got, ok = 0, False
        f = open(tmp, 'wb')
        try:
            async for chunk in request.content.iter_chunked(1 << 20):
                await loop.run_in_executor(None, f.write, chunk)   # USB zna da zastane: ne blokira mpv i stranicu
                got += len(chunk)
            await loop.run_in_executor(None, f.flush)
            ok = got == size
        finally:
            f.close()
            if ok:
                os.replace(tmp, dest)
            else:
                tmp.unlink(missing_ok=True)   # prekinuto slanje
        if not ok:
            return web.json_response({'err': 'Slanje je prekinuto.'}, status=400)
        log.info('igrica %s (%d MB) -> %s', name, size >> 20, dest)
        await teco.smb_refresh()
        return web.json_response({'ok': 'Sačuvano: %s/%s na %s' % (folder, name, sh['n'])})

    async def on_start(app):
        await teco.events_assets()
        if teco.st.get('cast'):
            await teco.cast_start()
        await teco.smb_refresh(force=True)   # smb.conf i smbd prema podešavanju (i posle instalacije)
        if teco.alarm_cfg().get('enabled'):   # alarm usluga (ako je podešena) — ostaje da radi i kad se server restartuje
            await teco._run('systemctl', '--user', 'start', ALARM_UNIT, timeout=20)
        app['tasks'] = [asyncio.create_task(t) for t in (teco.mpv.run(), teco.sys_loop(), teco.broadcast_loop(), teco.warm_ytdlp(), teco.clock_loop(), teco.slow_loop(), teco.boot_restore(), teco.hw_detect(), teco.ir_loop(), teco.inet_loop(), teco.prerender_consoles(), teco.alarm_loop())]

    async def on_stop(app):
        await teco.fm_stop()
        await teco.stream_stop()   # cast (posebna usluga) ostaje da radi i kad se server restartuje
        for t in app['tasks']:
            t.cancel()

    app.router.add_get('/', index)
    app.router.add_get('/favicon.ico', lambda r: web.FileResponse(STATIC / 'favicon-64.png'))   # pregledači ga traže i bez linka
    app.router.add_get('/ws', ws_handler)
    app.router.add_post('/api/upload', upload)
    app.router.add_post('/api/game', game_upload)
    app.router.add_post('/api/cast', cast_api)
    async def api_event(request):
        # okidanje zvona/interfona; za sada samo sa samog Teco.Pi (kasnije ESP)
        if request.remote not in ('127.0.0.1', '::1'):
            return web.json_response({'err': 'Za sada samo sa Teco.Pi.'}, status=403)
        eid = request.query.get('id', '')
        if eid not in teco.st['events']:
            return web.json_response({'err': 'Nepoznat događaj.'}, status=404)
        return web.json_response(await teco.fire_event(eid) or {'ok': True})

    async def yt_cookies_upload(request):
        """cookies.txt sa YouTube naloga (Netscape format, npr. dodatak „Get cookies.txt LOCALLY”)."""
        if request.headers.get('X-Teco-Token') not in teco.tokens:
            return web.json_response({'err': 'Potreban je Admin režim.'}, status=403)
        reader = await request.multipart()
        part = await reader.next()
        data = (await part.read(decode=False))[:2_000_000].decode('utf-8', 'replace')
        if 'youtube.com' not in data or '\t' not in data:
            return web.json_response({'err': 'To nije cookies.txt sa youtube.com (Netscape format).'}, status=400)
        YT_COOKIES.write_text(data)
        os.chmod(YT_COOKIES, 0o600)
        teco.rcache.clear()
        teco.mark()
        return web.json_response({'ok': 'YouTube kolačići su sačuvani'})

    async def api_outside(request):
        """Spoljna temperatura sa drugog uređaja u mreži (npr. ESP): /api/outside?t=12.5&h=60"""
        import ipaddress
        try:
            if not ipaddress.ip_address(request.remote).is_private:
                return web.json_response({'err': 'Samo iz lokalne mreže.'}, status=403)
            q = dict(request.query)
            if request.method == 'POST' and request.can_read_body:
                try:
                    q.update(await request.json())
                except ValueError:
                    q.update(await request.post())
            t = round(float(q['t']), 1)
            h = round(float(q['h'])) if q.get('h') not in (None, '') else None
            if not -60 <= t <= 70:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            return web.json_response({'err': 'Pošalji t (°C), npr. /api/outside?t=12.5&h=60'}, status=400)
        teco.outside = (t, h, time.time())
        teco.sys['out'], teco.sys['out_hum'] = t, h
        teco.mark()
        return web.json_response({'ok': True, 't': t, 'h': h})

    app.router.add_get('/api/outside', api_outside)
    app.router.add_post('/api/outside', api_outside)
    app.router.add_post('/api/event-sound', event_sound_upload)
    app.router.add_post('/api/yt-cookies', yt_cookies_upload)
    app.router.add_get('/api/event', api_event)
    app.router.add_post('/api/event', api_event)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static('/events', EVENTS_DIR)
    app.router.add_get('/send', send_page)
    app.router.add_get('/api/play', api_play)
    app.router.add_post('/api/play', api_play)
    app.router.add_static('/static', STATIC)
    app.router.add_static('/img', IMAGES)
    app.on_startup.append(on_start)
    async def on_shutdown(app):
        # zatvori veze sa stranicama odmah (inače gašenje čeka i do minut, a stranica se tada ne učitava)
        for ws in list(teco.clients):
            try:
                await ws.close(code=1001, message=b'restart')
            except Exception:
                pass

    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    IMAGES.mkdir(parents=True, exist_ok=True)
    web.run_app(make_app(), port=PORT, print=None, shutdown_timeout=2)
